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
        candidates = _shortlist_classpaths(part_desc, manufacturer_name)
        prompt = (
            f"Allowed Classpaths (choose exactly one):\n" + "\n".join(candidates) +
            f"\n\nProduct description: {part_desc}\n"
            f"Manufacturer: {manufacturer_name or 'unknown'}\n\nClasspath:"
        )

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
                break
            except RateLimitError:
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(BACKOFF_BASE_SECONDS * (2 ** attempt))
                    continue
                # Returning a sentinel here (the old behavior) meant
                # orchestrator._classify's try/except never saw a raised
                # exception and so never fell back to rule_based_classify
                # -- "UNKNOWN" got used as the row's real classpath, which
                # cascades into empty templates/attributes/descriptions for
                # every affected row. Re-raise instead so that catch-all
                # fallback (the one this module's own docstring promises)
                # actually engages.
                logger.warning("Groq classify() exhausted retries on 429 -- falling back to rule-based classify")
                raise
            except PermissionDeniedError:
                # 403 -- key/org lacks access, or (seen live) the network
                # is blocking api.groq.com entirely upstream of Groq. Not
                # something a retry fixes. Same reasoning as the 429 case
                # above: re-raise so orchestrator._classify falls back to
                # rule_based_classify instead of silently using "UNKNOWN"
                # as if it were a real answer.
                logger.warning("Groq classify() got 403 PermissionDenied -- falling back to rule-based classify")
                raise

        result = response.choices[0].message.content.strip()
        return result if result in all_classpaths else "UNKNOWN"
