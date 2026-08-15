"""
Groq client wired behind the classifier.LLMClassifierClient Protocol.
Only constructed if config.llm_configured() is True -- callers must check
before using this, and fall back to the deterministic baselines
(classifier.rule_based_classify) otherwise. Never hide that fallback
happened; the doc's principle is "never hide uncertainty."
"""
import asyncio
import logging

import httpx
from groq import AsyncGroq, PermissionDeniedError, RateLimitError

from config import GROQ_API_KEY, GROQ_CLASSIFY_MODEL
from persistence import llm_cache
from leaf_templates.registry import known_classpaths
from pipeline.classifier import rule_based_classify

logger = logging.getLogger(__name__)

# api.groq.com resolves an IPv6 address first; on a network with broken/
# flaky IPv6 routing, httpx's default happy-eyeballs-less connect will hang
# on the IPv6 attempt until timeout instead of falling back to IPv4 quickly
# (observed live: calls hung until the network's IPv6 path recovered on its
# own). local_address="0.0.0.0" forces httpx to bind/connect via IPv4,
# scoped to this Groq http_client only -- not a global socket monkeypatch,
# so it doesn't affect enricher.py's manufacturer-site fetches.
_GROQ_TRANSPORT = httpx.AsyncHTTPTransport(local_address="0.0.0.0")


def _make_http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=_GROQ_TRANSPORT)


class GroqModelPacer:
    """Serializes + paces calls to a single Groq model across the whole
    process, shared by every asyncio task regardless of the per-row
    CONCURRENCY semaphore in orchestrator.py.

    Root cause this fixes: extractor.py's extract_attributes()/
    fallback_extract_attributes() and description_gen.py's
    generate_prose_descriptions() all call the same GROQ_EXTRACT_MODEL/
    GROQ_DESC_MODEL (llama-3.3-70b-versatile), which free-tier caps at
    12000 TPM -- confirmed live via `with_raw_response` headers
    (x-ratelimit-limit-tokens: 12000). With CONCURRENCY=4 row-workers each
    firing 1-3 large (1-3k token) prompts against that one model with no
    coordination between them, a 200-row batch blew through the whole
    per-minute budget in the first few seconds; Groq then returned 429s
    with Retry-After up to ~700s, an order of magnitude past this module's
    own MAX_RETRIES=4 exponential backoff (~30s total), so every affected
    call gave up and returned {} -- silently, not via a visible error,
    which is exactly why the row-level per-row eval looked like a broad
    extraction failure rather than a rate-limit problem.

    Instance is a leaky-bucket-of-one: `wait()` blocks until at least
    `min_interval_seconds` has elapsed since the last call this pacer
    allowed, across all concurrent callers. Not a token-accurate limiter
    (doesn't inspect actual prompt size), just a conservative fixed
    cadence tuned to stay under budget with headroom for the classify
    model's own (separate-budget, unaffected) traffic."""

    def __init__(self, min_interval_seconds: float):
        self._lock = asyncio.Lock()
        self._min_interval = min_interval_seconds
        self._last_call_at: float | None = None

    async def wait(self) -> None:
        async with self._lock:
            loop = asyncio.get_event_loop()
            now = loop.time()
            if self._last_call_at is not None:
                elapsed = now - self._last_call_at
                remaining = self._min_interval - elapsed
                if remaining > 0:
                    await asyncio.sleep(remaining)
            self._last_call_at = loop.time()


# NOT confirmed to fix anything by itself -- every 429 actually inspected
# during debugging (both the raw error body and the response's own
# x-ratelimit-remaining-tokens/-requests headers) turned out to be the
# tokens-PER-DAY cap (see is_daily_quota_exhausted below), captured with
# the per-minute buckets still nearly full (e.g. remaining-tokens: 12000/
# 12000). No genuine TPM/RPM-exhaustion 429 was ever observed, so this
# pacer was originally tuned to 6s on an unvalidated assumption that it
# was needed -- that made a 200-row eval ~6x slower than before for no
# measured benefit. The real fix for the actual (daily-quota) failure mode
# is is_daily_quota_exhausted's fail-fast, not pacing.
#
# 2026-08-15: lowered to 0.1s after the 8B extraction swap. With extract
# on llama-3.1-8b-instant (separate quota pool from 70B, much higher TPM)
# and CONCURRENCY=4, the 1.0s pacer was the binding throughput limit:
# ~1 row/sec serial -> 200 rows took >25min and a 50-row eval took ~10min.
# 0.1s retains cheap insurance against 4 large concurrent prompts firing
# in the same instant (the original reason a non-zero value was kept) but
# removes the pacer as the bottleneck, so a full 200-row eval completes
# in a few minutes -- a prerequisite for iterating on accuracy at all.
# If genuine per-minute 429s appear under this setting (none observed in
# the 50-row run that produced 44.5% attribute accuracy), raise to 0.2-0.3
# rather than restoring 1.0.
GROQ_70B_PACER = GroqModelPacer(min_interval_seconds=0.2)
# NOTE: the variable name carries "70B" for historical reasons -- it paces
# every Groq call site (extract 8B, classify 8B via a future pacer if
# added, plasma prose 70B); do not be misled into thinking it only gates
# 70B model calls. Renaming would require coordinated edits to extractor.py
# and description_gen.py and is not worth the churn right now.


def is_daily_quota_exhausted(exc: RateLimitError) -> bool:
    """True if this 429 is Groq's tokens-per-day (TPD) limit, not the
    per-minute (TPM) one -- confirmed live on this free-tier account/key:
    `llama-3.3-70b-versatile` caps at 100000 TPD, separate from and much
    smaller than what a 200-row batch's extraction+fallback+prose calls
    need (each ~500-2500 prompt tokens, up to 3 calls/row). A per-minute
    429 clears itself in under a minute and is worth this module's normal
    exponential-backoff retry; a daily 429 will not clear for minutes-to-
    hours (the response's own retry-after ran ~9-11 minutes when this was
    observed), so retrying it with the same ~30s backoff budget every
    other 429 gets is pure waste -- multiplied across every remaining row
    in a batch, that waste is exactly what produced a near-zero attribute-
    accuracy score without ever raising a visible error. Callers should
    fail fast (skip retries, return {}/empty) on this case instead."""
    return "per day" in str(exc) or "TPD" in str(exc)


# Process-global "sticky" quota-exhaustion tracker, per model name. Once a
# model's TPD cap has been observed to fail ONCE in this process, every
# subsequent call for that model fails fast at zero cost (no pacer wait,
# no API hit) -- because the daily quota does NOT reset mid-process.
# This is the difference between a 200-row eval completing in ~3 minutes
# vs hanging for 30+ minutes on 200 unused prose calls each waiting their
# 0.2s pacer slot then calling the API to re-confirm what we already know.
# Reset by restarting the process (a new day, a fresh quota window).
_quota_exhausted_models: dict[str, bool] = {}


def mark_quota_exhausted(model: str) -> None:
    """Record that `model` is in TPD-exhausted state for the rest of this
    process. Idempotent. Callers should call this the moment a
    RateLimitError with is_daily_quota_exhausted=True is observed, then
    `quota_is_exhausted(model)` before any subsequent attempt to call that
    model -- if exhausted, skip the call and return empty/fallback directly."""
    if not _quota_exhausted_models.get(model):
        logger.warning(
            "Groq model %s marked TPD-exhausted for the rest of this process -- "
            "all subsequent calls will fail fast without pacing or API hits",
            model,
        )
    _quota_exhausted_models[model] = True


def quota_is_exhausted(model: str) -> bool:
    """True if `mark_quota_exhausted(model)` has been called in this process.
    Callers should check this BEFORE pacing / calling the Groq API, and
    fail fast (return {} / raise for fallback / etc.) when True."""
    return _quota_exhausted_models.get(model, False)

_CLASSIFY_SYSTEM_PROMPT = """You classify a distributor product row into exactly one leaf Classpath \
from a fixed taxonomy. Reply with ONLY the exact Classpath string, nothing else. \
If none fit well, reply with UNKNOWN."""

MAX_RETRIES = 4
BACKOFF_BASE_SECONDS = 2.0
# Sending all 74 leaf classpaths on every call burns through the free-tier
# TPM budget fast (hit a real 429 running a 50-row eval at concurrency 4:
# "Limit 6000, Used 5812, Requested 1512"). The rule-based baseline
# (classifier.rule_based_classify) is nearly free and already gets the
# right answer in its top candidates most of the time (87.3% LOO top-1) --
# use it to shortlist instead of sending the full taxonomy every call.
CANDIDATE_SHORTLIST_SIZE = 8


def _shortlist_classpaths(part_desc: str, manufacturer_name: str | None) -> list[str]:
    all_classpaths = known_classpaths()
    baseline = rule_based_classify(part_desc, manufacturer_name)
    shortlist = [c for c in (baseline.classpath, baseline.runner_up) if c]
    remaining = [c for c in all_classpaths if c not in shortlist]
    shortlist.extend(remaining[: max(0, CANDIDATE_SHORTLIST_SIZE - len(shortlist))])
    return shortlist or all_classpaths[:CANDIDATE_SHORTLIST_SIZE]


class GroqClassifierClient:
    def __init__(self, api_key: str | None = None, model: str | None = None):
        self.client = AsyncGroq(api_key=api_key or GROQ_API_KEY, http_client=_make_http_client())
        self.model = model or GROQ_CLASSIFY_MODEL

    async def classify(self, part_desc: str, manufacturer_name: str | None) -> str:
        all_classpaths = known_classpaths()
        # Skip the API call entirely if this model's TPD quota is already
        # known exhausted -- raise immediately so orchestrator._classify's
        # try/except falls back to rule_based_classify (the same path a live
        # 429-TPD takes), avoiding serial pacer wait + round-trip on every
        # remaining row in the batch.
        skip_live = quota_is_exhausted(self.model)
        candidates = _shortlist_classpaths(part_desc, manufacturer_name)
        prompt = (
            f"Allowed Classpaths (choose exactly one):\n" + "\n".join(candidates) +
            f"\n\nProduct description: {part_desc}\n"
            f"Manufacturer: {manufacturer_name or 'unknown'}\n\nClasspath:"
        )

        async def _live() -> str | None:
            if skip_live:
                return None
            for attempt in range(MAX_RETRIES + 1):
                try:
                    response = await self.client.chat.completions.create(
                        model=self.model,
                        messages=[
                            {"role": "system", "content": _CLASSIFY_SYSTEM_PROMPT},
                            {"role": "user", "content": prompt},
                        ],
                        temperature=0,
                        max_tokens=100,
                    )
                    return response.choices[0].message.content
                except RateLimitError as exc:
                    if is_daily_quota_exhausted(exc):
                        logger.warning(
                            "Groq classify() hit daily token quota -- skipping retries, "
                            "falling back to rule-based classify"
                        )
                        mark_quota_exhausted(self.model)
                        return None
                    if attempt < MAX_RETRIES:
                        await asyncio.sleep(BACKOFF_BASE_SECONDS * (2 ** attempt))
                        continue
                    logger.warning("Groq classify() exhausted retries on 429 -- falling back to rule-based classify")
                    return None
                except PermissionDeniedError:
                    logger.warning("Groq classify() got 403 PermissionDenied -- falling back to rule-based classify")
                    return None
            return None

        raw = await llm_cache.cached_call(
            namespace="classify",
            model=self.model,
            system_prompt=_CLASSIFY_SYSTEM_PROMPT,
            user_prompt=prompt,
            temperature=0,
            max_tokens=100,
            response_format=None,
            live_fn=_live,
        )
        if raw is None:
            # cache miss + live path failed (quota, 403, retries exhausted) --
            # raise so orchestrator._classify's try/except falls back to
            # rule_based_classify (never silently hide the fallback).
            raise RateLimitError(
                message="classify live-call returned None (quota/403/retry-exhaustion)",
                response=httpx.Response(status_code=429),
                body=None,
            )
        result = raw.strip()
        return result if result in all_classpaths else "UNKNOWN"
