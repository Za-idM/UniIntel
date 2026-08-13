"""
Stage 4: extract/reconcile attributes into the leaf template's fixed slots,
JSON-schema forced (locked build plan Section 9: "medium model, JSON-schema
forced"). Merges rule_preextractor priors (from Part_Desc) with LLM
extraction from enriched page evidence_text, preferring sourced/evidence-
backed values per the doc's "evidence for everything" principle -- never
invent a value that isn't in the input text or a rule prior.
"""
import asyncio
import json
import logging

from groq import AsyncGroq, PermissionDeniedError, RateLimitError

from config import GROQ_API_KEY, GROQ_EXTRACT_MODEL
from leaf_templates.registry import AttributeSlot, get_template
from pipeline.llm_client import _make_http_client

logger = logging.getLogger(__name__)

MAX_RETRIES = 4
BACKOFF_BASE_SECONDS = 2.0

_EXTRACT_SYSTEM_PROMPT = """You extract product attributes from manufacturer page text into a fixed \
set of labeled slots. Reply with ONLY a JSON object mapping each given label to its extracted value \
(a short string) as found in the source text. If a slot's value is not present in the text, OMIT that \
key entirely -- do not guess, invent, or fill in a plausible-sounding value. Do not include UOM in the \
value string; extract the bare value only."""


def _build_prompt(evidence_text: str, slots: list[AttributeSlot]) -> str:
    labels = [s.label for s in slots]
    return (
        f"Slots to fill (use these exact labels as JSON keys):\n" + "\n".join(labels) +
        f"\n\nSource text:\n{evidence_text[:6000]}\n\nJSON:"
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
    real IPv4-forced Groq client (see llm_client.py's IPv6-hang note)."""
    slots = get_template(classpath)
    if not slots or not evidence_text:
        return {}

    client = client or AsyncGroq(api_key=api_key or GROQ_API_KEY, http_client=_make_http_client())
    prompt = _build_prompt(evidence_text, slots)

    for attempt in range(MAX_RETRIES + 1):
        try:
            response = await client.chat.completions.create(
                model=model or GROQ_EXTRACT_MODEL,
                messages=[
                    {"role": "system", "content": _EXTRACT_SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                temperature=0,
                max_tokens=1500,
                response_format={"type": "json_object"},
            )
            break
        except RateLimitError:
            if attempt < MAX_RETRIES:
                await asyncio.sleep(BACKOFF_BASE_SECONDS * (2 ** attempt))
                continue
            return {}
        except PermissionDeniedError:
            # 403 -- not retryable like a 429. Log and skip extraction for
            # this row (caller's reconcile() already handles an empty dict
            # by falling back to rule_priors / leaving slots empty).
            logger.warning("Groq extract_attributes() got 403 PermissionDenied -- skipping LLM extraction")
            return {}

    raw = response.choices[0].message.content
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}

    valid_labels = {s.label for s in slots}
    # constrain to known slot labels -- never accept an invented key
    return {k: str(v) for k, v in parsed.items() if k in valid_labels and v not in (None, "")}


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
    fetch required. Each prior is claimed by at most one slot, given to the
    first matching slot in template order, so an ambiguous uom shared by
    multiple slots (LED's Wattage and Incandescent Wattage Equivalent both
    hint "W") resolves to the more directly-named one rather than being
    applied twice."""
    slots = get_template(classpath)
    prior_uom_by_value = {p["value"]: p["uom"] for p in rule_priors}
    priors_by_uom: dict[str, list[str]] = {}
    for p in rule_priors:
        priors_by_uom.setdefault(p["uom"], []).append(p["value"])

    result = []
    for slot in slots:
        value = llm_extracted.get(slot.label, "")
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
