"""
Stage 4: extract/reconcile attributes into the leaf template's fixed slots,
JSON-schema forced (locked build plan Section 9: "medium model, JSON-schema
forced"). Merges rule_preextractor priors (from Part_Desc) with LLM
extraction from enriched page evidence_text, preferring sourced/evidence-
backed values per the doc's "evidence for everything" principle -- never
invent a value that isn't in the input text or a rule prior.

Fallback path: when web evidence is unavailable (fetch failed, blocked,
no URL), a LOV-constrained extraction from Part_Desc + classpath +
manufacturer still runs. The LLM sees the allowed values for each
attribute and can only select from them or omit -- it cannot invent.
This lifts attribute accuracy from ~0% to a usable baseline on rows
where web evidence isn't reachable.
"""
import asyncio
import json
import logging
from functools import lru_cache
from pathlib import Path

from groq import (
    AsyncGroq,
    BadRequestError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
)

from config import GROQ_API_KEY, GROQ_EXTRACT_MODEL
from leaf_templates.registry import AttributeSlot, get_template
from persistence import llm_cache
from pipeline.llm_client import GROQ_70B_PACER, _make_http_client, is_daily_quota_exhausted, mark_quota_exhausted, quota_is_exhausted

logger = logging.getLogger(__name__)

MAX_RETRIES = 4
BACKOFF_BASE_SECONDS = 2.0

BOOTSTRAP = Path(__file__).resolve().parent.parent / "data" / "bootstrap"

_EXTRACT_SYSTEM_PROMPT = """You extract product attributes from manufacturer page text into a fixed \
set of labeled slots. Reply with ONLY a JSON object mapping each given label to its extracted value \
(a short string) as found in the source text. If a slot's value is not present in the text, OMIT that \
key entirely -- do not guess, invent, or fill in a plausible-sounding value. Do not include UOM in the \
value string; extract the bare value only. Begin the reply with { and end with }. Emit nothing else."""

_FALLBACK_SYSTEM_PROMPT = """You extract product attributes from a short, abbreviated product \
description. You are given the product category (classpath), manufacturer, and for each attribute \
the list of allowed values from the approved vocabulary. Reply with ONLY a JSON object mapping each \
label to its extracted value. RULES:
1. For attributes with an ALLOWED VALUES list, only emit a value from that list -- never invent or rephrase. If none of the allowed values appear in the description, OMIT the key.
2. For attributes marked (free-form), extract the bare value VERBATIM from the description as it appears (no rephrasing, no unit, no inventing). OMIT the key if the description contains no token matching that attribute's meaning.
3. Only fill a slot if the description gives clear evidence for that value -- never guess.
4. Do not include UOM in the value string; extract the bare value only.
5. Begin the reply with { and end with }. Emit nothing else."""


@lru_cache(maxsize=1)
def _load_lov() -> dict[str, dict[str, list[str]]]:
    """Load {classpath: {attribute_label: [allowed_values]}} from bootstrap."""
    lov_path = BOOTSTRAP / "lov_by_classpath.json"
    if lov_path.exists():
        return json.loads(lov_path.read_text(encoding="utf-8"))
    return {}


def _build_prompt(evidence_text: str, slots: list[AttributeSlot]) -> str:
    labels = [s.label for s in slots]
    return (
        f"Slots to fill (use these exact labels as JSON keys):\n" + "\n".join(labels) +
        f"\n\nSource text:\n{evidence_text[:6000]}\n\nJSON:"
    )


def _build_fallback_prompt(
    part_desc: str,
    classpath: str,
    manufacturer_name: str | None,
    slots: list[AttributeSlot],
) -> str:
    """Build a LOV-constrained prompt from Part_Desc when no web evidence."""
    lov = _load_lov()
    classpath_lov = lov.get(classpath, {})

    slot_lines = []
    for s in slots:
        allowed = classpath_lov.get(s.label, [])
        if allowed:
            # Capped at 12 (not 30) -- the 30-value cap was still large
            # enough, multiplied across a 15-20 slot template, to push a
            # single fallback prompt past 2000+ tokens, which combined with
            # per-row CONCURRENCY=4 blew through the shared 12000 TPM Groq
            # budget in seconds (see GROQ_70B_PACER's docstring). Smaller
            # sample keeps prompts lean; GROQ_70B_PACER handles the
            # cross-request pacing this alone can't.
            sample = allowed[:12]
            extras = f" (and {len(allowed) - 12} more)" if len(allowed) > 12 else ""
            slot_lines.append(f"- {s.label}: ALLOWED VALUES: {', '.join(sample)}{extras}")
        else:
            slot_lines.append(f"- {s.label}: (free-form, extract from description if present)")

    return (
        f"Product category: {classpath}\n"
        f"Manufacturer: {manufacturer_name or 'unknown'}\n"
        f"Raw description: {part_desc}\n\n"
        f"Attribute slots and their allowed values:\n" + "\n".join(slot_lines) +
        f"\n\nJSON:"
    )


async def extract_attributes(
    evidence_text: str,
    classpath: str,
    api_key: str | None = None,
    model: str | None = None,
    client: AsyncGroq | None = None,
) -> dict[str, str]:
    """Returns {label: extracted_value} for whichever slots were found in
    evidence_text. Missing slots are simply absent from the dict -- caller
    (reconciliation) is responsible for leaving those template slots empty
    rather than treating absence as an error.

    `client` is injectable for testing (mocked completions) without a live
    API call; production callers should omit it and let this construct a
    real IPv4-forced Groq client (see llm_client.py's IPv6-hang note).

    Caching: the raw completion is keyed by (namespace='extract_evidence',
    sha256(system+user+temp+max_tokens+response_format+model)) in
    persistence.llm_cache. Re-runs of the SAME prompt + model return from
    cache with zero token spend, zero pacer wait -- the difference between
    evaluating 200 rows in 30 seconds (cache-hot) and 12 minutes
    (cache-cold) + the daily-quota wall. The parsed dict is NOT cached
    (post-processing has its own filter that could change without invalidating
    cache); we cache the raw completion string and re-parse on every hit."""
    slots = get_template(classpath)
    if not slots or not evidence_text:
        return {}

    model_name = model or GROQ_EXTRACT_MODEL
    # Skip the API call entirely if today's TPD quota for this model is
    # already known exhausted AND we have no cached reply for this prompt.
    # The cache lookup below still runs (a prior cached call could pre-date
    # the quota wall); only the live path is skipped. This is the whole
    # point of the cache -- after a TPD wall, cached rows complete free.
    skip_live = quota_is_exhausted(model_name)
    prompt = _build_prompt(evidence_text, slots)

    async def _live() -> str | None:
        # The actual completion + pacing + retry/quota handling. Skipped
        # entirely when quota is marked exhausted (caller returns None and
        # cached_call falls through to a cache-only check).
        if skip_live:
            return None
        nonlocal client
        client = client or AsyncGroq(api_key=api_key or GROQ_API_KEY, http_client=_make_http_client())
        for attempt in range(MAX_RETRIES + 1):
            await GROQ_70B_PACER.wait()
            try:
                response = await client.chat.completions.create(
                    model=model_name,
                    messages=[
                        {"role": "system", "content": _EXTRACT_SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0,
                    max_tokens=600,
                    response_format={"type": "json_object"},
                )
                return response.choices[0].message.content
            except RateLimitError as exc:
                if is_daily_quota_exhausted(exc):
                    logger.warning(
                        "Groq extract_attributes() hit daily token quota -- "
                        "skipping retries, extraction unavailable until quota resets"
                    )
                    mark_quota_exhausted(model_name)
                    return None
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(BACKOFF_BASE_SECONDS * (2 ** attempt))
                    continue
                return None
            except (PermissionDeniedError, NotFoundError, BadRequestError) as exc:
                # Same rationale as description_gen.py Fix 1: NotFoundError
                # (HTTP 404) fires when Groq decommissions the configured
                # model (seen live 2026-08-17 for llama-3.1-8b-instant).
                # Without this catch the 404 propagates up to
                # orchestrator.process_row's broad `except Exception: llm_extracted={}`,
                # which DOES keep the row alive -- but it raised a full
                # Python traceback to the logs on EVERY un-cached row, AND
                # never called mark_quota_exhausted, so every subsequent
                # row replicated the same ~0.4s dead-API round-trip with
                # no fast-path. On a 1000-row judge-uploaded dataset with
                # zero cache hits (the dynamic-upload requirement UniHack
                # organizers confirmed live 2026-08-17), that's ~7 minutes
                # of pure wasted round-trips plus 1000 stacktraces in the
                # logs. Killing it here: catch -> mark_quota_exhausted ->
                # log once -> return None -> {}/clean reconcile. After
                # the first failed row, every subsequent row sees
                # quota_is_exhausted()=True on the model_name and short-
                # circuits `skip_live` without pacing or HTTP.
                logger.warning(
                    "Groq extract_attributes() model %r unavailable "
                    "(%s: %s) -- skipping Stage 4 web-evidence LLM "
                    "extraction for this run; reconciling from rule priors",
                    model_name,
                    type(exc).__name__,
                    exc,
                )
                mark_quota_exhausted(model_name)
                return None
        return None

    raw = await llm_cache.cached_call(
        namespace="extract_evidence",
        model=model_name,
        system_prompt=_EXTRACT_SYSTEM_PROMPT,
        user_prompt=prompt,
        temperature=0,
        max_tokens=600,
        response_format="json_object",
        live_fn=_live,
    )
    if raw is None:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}

    valid_labels = {s.label for s in slots}
    # "not found" is the 8B extractor's stock absence phrase -- observed
    # 26x across the 1000-row submission+229 cached completions (scan of
    # 2026-08-15 via scripts/scan_garbage). Case-insensitive: the existing
    # `str(v).strip().lower() not in _BAD` check below catches any casing
    # of it. KEEP THIS SET IN SYNC with the identical one inside
    # fallback_extract_attributes() below -- the two filters drift silently
    # otherwise (only the function containing the line you're editing knows
    # the other exists).
    _BAD = {None, "", "none", "null", "n/a", "na", "unknown",
            "unspecified", "-", "not found"}
    return {
        k: str(v) for k, v in parsed.items()
        if k in valid_labels and v not in _BAD and str(v).strip().lower() not in _BAD
    }


async def fallback_extract_attributes(
    part_desc: str,
    classpath: str,
    manufacturer_name: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
    client: AsyncGroq | None = None,
) -> dict[str, str]:
    """LOV-constrained extraction from Part_Desc alone -- used when web
    fetch fails. Same retry/error logic as extract_attributes(). Values
    are constrained to the LOV-allowed set where available, so output
    is guaranteed to pass V2 LOV validation.

    Caching: raw completion cached under namespace='extract_fallback'.
    Re-runs of the same prompt+model are free; this is the main lever
    for iterating accuracy without burning the daily quota wall."""
    slots = get_template(classpath)
    if not slots or not part_desc:
        return {}

    model_name = model or GROQ_EXTRACT_MODEL
    # Skip the LLM live path when quota is exhausted, but still consult
    # the cache -- a prior cached call from before the wall hit is the
    # entire reason the cache exists.
    skip_live = quota_is_exhausted(model_name)
    prompt = _build_fallback_prompt(part_desc, classpath, manufacturer_name, slots)

    async def _live() -> str | None:
        if skip_live:
            return None
        nonlocal client
        client = client or AsyncGroq(api_key=api_key or GROQ_API_KEY, http_client=_make_http_client())
        for attempt in range(MAX_RETRIES + 1):
            await GROQ_70B_PACER.wait()
            try:
                response = await client.chat.completions.create(
                    model=model_name,
                    messages=[
                        {"role": "system", "content": _FALLBACK_SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0,
                    max_tokens=600,
                    response_format={"type": "json_object"},
                )
                return response.choices[0].message.content
            except RateLimitError as exc:
                if is_daily_quota_exhausted(exc):
                    logger.warning(
                        "Groq fallback_extract_attributes() hit daily token quota -- "
                        "skipping retries, extraction unavailable until quota resets"
                    )
                    mark_quota_exhausted(model_name)
                    return None
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(BACKOFF_BASE_SECONDS * (2 ** attempt))
                    continue
                return None
            except (PermissionDeniedError, NotFoundError, BadRequestError) as exc:
                # Mirrors the same handler added to extract_attributes()
                # above -- NotFoundError (decommissioned GROQ_EXTRACT_MODEL
                # llama-3.1-8b-instant live on 2026-08-17) propagates
                # through llm_cache.cached_call's await live_fn() and would
                # otherwise hit the orchestrator's broad `except Exception:`
                # per row, costing ~0.4s of dead-API round-trip + a
                # stacktrace in the logs every single time. The stage-3
                # fetch failure path is the MOST-FIRED stage-4 path
                # (`enrichment.status != "FETCHED"` is the majority case
                # per CLAUDE.md: 19/22 Philips rows NO_URL + 3/22 Satco
                # FETCH_FAILED), so this branch dominates dynamic-upload
                # latency on rows where Stage 3 didn't reach a manufacturer
                # page. Catching here + mark_quota_exhausted means later
                # rows in the same job fail-fast without HTTP.
                logger.warning(
                    "Groq fallback_extract_attributes() model %r unavailable "
                    "(%s: %s) -- skipping Stage 4 desc-fallback LLM "
                    "extraction for this run; reconciling from rule priors",
                    model_name,
                    type(exc).__name__,
                    exc,
                )
                mark_quota_exhausted(model_name)
                return None
        return None

    raw = await llm_cache.cached_call(
        namespace="extract_fallback",
        model=model_name,
        system_prompt=_FALLBACK_SYSTEM_PROMPT,
        user_prompt=prompt,
        temperature=0,
        max_tokens=600,
        response_format="json_object",
        live_fn=_live,
    )
    if raw is None:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}

    valid_labels = {s.label for s in slots}
    # KEEP IN SYNC with extract_attributes()'s _BAD set above. "not found"
    # is the 8B extractor's stock absence phrase -- see the comment there
    # for the observation scan that justified adding it.
    _BAD = {None, "", "none", "null", "n/a", "na", "unknown",
            "unspecified", "-", "not found"}
    return {
        k: str(v) for k, v in parsed.items()
        if k in valid_labels and v not in _BAD and str(v).strip().lower() not in _BAD
    }


def reconcile(
    classpath: str,
    llm_extracted: dict[str, str],
    rule_priors: list[dict[str, str]],
) -> list[dict]:
    """Merge rule_preextractor priors + LLM extraction into the ordered
    template, emitting every slot (label always present, value/uom empty
    if unknown) per the doc's slot-ordering rule (Section 2).

    rule_priors also serve as a VALUE fallback, not just a UOM override:
    when the LLM found nothing for a uom-tagged slot (e.g. because Stage 3
    enrichment never fetched a page), a regex-extracted (value, uom) pair
    from Part_Desc fills it directly -- e.g. "8W" -> Wattage=8 with no
    fetch required. Each prior is claimed by at most one slot, given to
    the first matching slot in template order, so an ambiguous uom shared by
    multiple slots (LED's Wattage and Incandescent Wattage Equivalent both
    hint "W") resolves to the more directly-named one rather than being
    applied twice.

    LLM extraction sometimes keeps the UOM in the value (e.g. "125V" /
    "60Hz" / "1 hp" for Leviton GFCI rows) despite prompt instructions
    not to. When the slot has a registered uom_hint, a trailing suffix
    matching that hint is stripped here so the value lands numeric and
    matches GT's representation. The UOM lives in the slot's `uom`
    field, not the value. See `_strip_attribute_uom` for the conservative
    guards (remainder must be numeric, no mid-word stripping). This is a
    Stage-4 post-processor -- V3 (description UOM) handles a different
    output contract downstream."""
    slots = get_template(classpath)
    prior_uom_by_value = {p["value"]: p["uom"] for p in rule_priors}
    priors_by_uom: dict[str, list[str]] = {}
    for p in rule_priors:
        priors_by_uom.setdefault(p["uom"], []).append(p["value"])

    result = []
    for slot in slots:
        value = llm_extracted.get(slot.label, "")
        # Strip trailing UOM suffix the LLM may have inappropriately kept
        # (e.g. "125V" -> "125" for a V-hinted slot). Conservative: only
        # fires when the remainder is numeric (after stripping) so a
        # genuine text value like "Var" can't be mis-stripped. No-op for
        # values that don't end with the slot's UOM (the LED direct-map
        # path already sanitises upstream so this is dead-code there).
        if value and slot.uom_hint:
            value = _strip_attribute_uom(value, slot.uom_hint)
        uom = slot.uom_hint if value else ""
        origin = "llm_extract" if value else None
        if value in prior_uom_by_value:
            # a rule-extracted prior matches the LLM value -- prefer its
            # cheaply-verified UOM instead of the mined default hint. Value
            # still came from the LLM, so origin stays llm_extract.
            uom = prior_uom_by_value[value]
        elif not value and slot.uom_hint and priors_by_uom.get(slot.uom_hint):
            value = priors_by_uom[slot.uom_hint].pop(0)
            uom = slot.uom_hint
            origin = "rule_prior"
        result.append({"slot": slot.slot, "label": slot.label, "value": value, "uom": uom, "origin": origin})
    return result


import re as _re


def _strip_attribute_uom(value: str, uom_hint: str) -> str:
    """Strip a trailing UOM suffix the LLM may have kept inside the value
    (e.g. "125V" / "60Hz" / "1 hp" -> "125" / "60" / "1"), keyed on the
    slot's registered uom_hint. The UOM belongs in the slot's `uom`
    field, not the value itself.

    Conservative guards so we never damage a value we're not sure about:
      * Remainder must still look numeric / range / fraction-y (digits,
        dots, dashes, slashes, commas, spaces). A textual remainder
        like "Maximum Continuous" -> strip would yield a non-numeric
        remainder and we leave the value untouched.
      * The UOM must match the *suffix* of the value (case-insensitive,
        optional preceding whitespace). It can't match mid-word -- a
        value of "Var" with uom_hint "V" wouldn't have its "ar" stolen.
      * When uom_hint is itself multi-word ("deg C", "lb. ft."), the
        regex repeats each token once with optional whitespace, so a
        realistic space-separated suffix ("125 deg C") and a compact one
        ("125°C") need the tokeniser -- the degenerate case is that the
        structurally-splitting match fails and we leave the value as-is.

    Mirrors what satco_pdf._strip_uom does for the PDF direct-map path;
    this is left intentionally narrower because the LLM is more chaotic
    than a controlled PDF text layer and we'd rather under-strip than
    silently corrupt a value."""
    if not value or not uom_hint:
        return value
    v = value.strip()
    if not v:
        return value

    # Build a regex that matches <-numeric-remainder>(<whitespace>*<uom_hint>)$
    # re.escape handles chars like "$" or "/" in uom_hint; the \s* absorbs
    # the optional space between value and UOM.
    hint = _re.escape(uom_hint.strip())
    pattern = _re.compile(
        r"^(.+?)\s*" + hint + r"\s*$",
        _re.IGNORECASE,
    )
    m = pattern.match(v)
    if not m:
        return value
    remainder = m.group(1).strip()
    if not remainder:
        return value
    # Numeric / range / fraction guard. Allows digits, dots, dashes,
    # commas, slashes, whitespace, AND the " to " range separator (so
    # "120 to 347" passes after a "V" suffix strip). Substituting " to "
    # with a slash before the charset check keeps the guard cheap and
    # avoids admitting other words. Counter-examples that fail this
    # guard correctly: "Integral LED" (no digit start), "Maximum
    # Continuous" (words other than "to"), "10mp" (no digit-only prefix).
    check = remainder.replace(" to ", "/")
    if not _re.match(r"^[\d.\-/,\s]+$", check):
        return value
    return remainder
