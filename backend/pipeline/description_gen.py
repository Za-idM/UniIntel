"""
Stage 5: description generation.

Deterministic string templates (INVOICE_DESC, MOBILE_DESC, SHORT_DESC,
RETAIL_DESC) mined from GT -- zero LLM calls, zero invented text, exact
values pulled straight from the reconciled attribute set. LONG_DESC1 and
MARKETING_DESCRIPTION are LLM-generated prose (Groq, JSON-schema forced)
fed the same validated attribute JSON -- never given room to invent a
value that isn't already in the reconciled attributes.

RETAIL_DESC deviates from the original plan (LLM-generated) on GT
evidence: it is exactly SHORT_DESC with the "{brand} {mpn} " prefix
stripped in every sampled GT row. Implemented deterministically instead --
free, exact, and the locked plan explicitly cares about cost-per-SKU
(Section 8). LONG_DESC1 also shows a very regular per-slot structure in
GT (not free prose) but generalizing that would mean extending the leaf
template schema itself (per-slot format-type) -- bigger scope than this
pass, so LONG_DESC1 stays LLM-generated per the plan; worth a follow-up
look if LONG_DESC1 match quality matters more than MARKETING_DESCRIPTION's.
"""
import asyncio
import json
import logging
from functools import lru_cache
from pathlib import Path

from groq import AsyncGroq, PermissionDeniedError, RateLimitError

from config import GROQ_API_KEY, GROQ_DESC_MODEL
from pipeline.llm_client import GROQ_70B_PACER, _make_http_client, is_daily_quota_exhausted, mark_quota_exhausted, quota_is_exhausted
from persistence import llm_cache
from pipeline.normalizer import format_uom

logger = logging.getLogger(__name__)

BOOTSTRAP = Path(__file__).resolve().parent.parent / "data" / "bootstrap"

MAX_RETRIES = 4
BACKOFF_BASE_SECONDS = 2.0

# The only leaf classpath with a GT-mined, hand-verified deterministic
# template (90.9%/95.5%/95.5% exact match on INVOICE/SHORT/RETAIL -- see
# scripts/evaluate_descriptions.py). Every other leaf falls back to
# _generic_noun()/_generic_invoice_desc() below: a category-correct noun
# and best-effort attribute listing, not a GT-exact reproduction --
# mining a per-leaf template for the other 73 classpaths is future scope,
# not done here.
LED_BULBS_CLASSPATH = "Electrical>Lamps & Lightings>Light Bulbs>LED Light Bulbs"

# INVOICE_DESC's optional trailing tokens, tried in this fixed order and
# appended only while the running total stays <=INVOICE_MAX_LEN chars.
# Not an exact-match reproduction of GT's own ordering (would overfit to
# 22 rows) -- "never exceeds the limit, includes the most useful tokens
# available" is the actual contract.
INVOICE_MAX_LEN = 40

# Only "Amber" -> "AMB" is confirmed in GT; other appearances (Warm White,
# Warm Glow, Soft White, Natural Light) never get an INVOICE_DESC
# abbreviation there. Guessing abbreviations for those actively produced
# wrong output (564450 got an invented "WG" suffix GT never has) --
# removed rather than left in as unverified guesses.
APPEARANCE_ABBREV = {
    "amber": "AMB",
}


@lru_cache(maxsize=1)
def _display_names() -> dict[str, dict[str, str]]:
    return json.loads((BOOTSTRAP / "manufacturer_display_names.json").read_text(encoding="utf-8"))


def _brand(manufacturer_name: str | None, style: str) -> str:
    """style is 'mobile' or 'short'. Falls back to the raw manufacturer
    name when it's not one of the (currently 2) GT-mined display names --
    no data to derive a shorter public brand name for anyone else yet."""
    if not manufacturer_name:
        return ""
    names = _display_names().get(manufacturer_name)
    if names:
        return names.get(style, manufacturer_name)
    return manufacturer_name


def _is_filament(attrs: dict[str, str]) -> bool:
    """GT signal: Additional Information explicitly says Glass/Plastic
    construction when present; falls back to shape-code family (T-prefixed
    tube/Edison-style shapes are filament-style) when it doesn't."""
    additional_info = attrs.get("Additional Information", "")
    if "Glass" in additional_info:
        return True
    if "Plastic" in additional_info:
        return False
    shape_code = attrs.get("Bulb Shape Code", "")
    return shape_code.startswith(("T", "ST"))


def _shape_noun(shape_code: str) -> str:
    """B-prefixed non-reflector codes (B11, candelabra/candle shapes) are
    "Candle"; everything else (including BR-prefixed reflector shapes) is
    "Bulb". GT itself is inconsistent on this for at least one SKU
    (573378 is B11 but labeled "Bulb") -- accepted as real-data noise,
    not chased further."""
    if shape_code.startswith("B") and not shape_code.startswith("BR"):
        return "Candle"
    return "Bulb"


def _singularize(word: str) -> str:
    if word.endswith("ies") and len(word) > 3:
        return word[:-3] + "y"
    if word.endswith(("ses", "xes", "ches", "shes")):
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _generic_noun(classpath: str) -> str:
    """Leaf classpath's own name, singularized, for any category without a
    GT-mined noun template (see LED_BULBS_CLASSPATH docstring above) --
    e.g. "Laundry Appliances>Electric Dryers" -> "Electric Dryer"."""
    leaf = classpath.split(">")[-1].strip()
    words = leaf.split(" ")
    if words:
        words[-1] = _singularize(words[-1])
    return " ".join(words)


def _noun(attrs: dict[str, str], classpath: str | None) -> str:
    if classpath == LED_BULBS_CLASSPATH:
        filament = " Filament" if _is_filament(attrs) else ""
        return f"LED{filament} {_shape_noun(attrs.get('Bulb Shape Code', ''))}"
    if classpath:
        return _generic_noun(classpath)
    return "Product"


def _is_range(value: str) -> bool:
    return " to " in value


def _generic_invoice_desc(classpath: str | None, attrs: dict[str, str]) -> str:
    """Fallback for any classpath other than LED Light Bulbs: noun plus as
    many non-empty attribute values as fit within INVOICE_MAX_LEN, ALL
    CAPS. Not a GT-exact per-category template (those aren't mined) --
    just a category-correct string instead of a wrong LED-bulb noun."""
    result = (_generic_noun(classpath) if classpath else "Product").upper()
    for value in attrs.values():
        if not value or _is_range(value):
            continue
        extended = f"{result} {value.upper()}"
        if len(extended) <= INVOICE_MAX_LEN:
            result = extended
    return result


def invoice_desc(mpn: str, attrs: dict[str, str], classpath: str | None = None) -> str:
    """ALL CAPS, compressed UOM, <=40 chars (GT's INVOICE_DESC contract).
    Base string is always included; optional trailing tokens are appended
    in a fixed priority order only while they still fit. Only LED Light
    Bulbs has a GT-mined exact template; other classpaths use
    _generic_invoice_desc()."""
    if classpath != LED_BULBS_CLASSPATH:
        return _generic_invoice_desc(classpath, attrs)

    shape_code = attrs.get("Bulb Shape Code", "")
    noun_word = "CANDLE" if _shape_noun(shape_code) == "Candle" else "BULB"
    fil = " FIL" if _is_filament(attrs) else ""
    lumens = attrs.get("Lumens", "")
    wattage = attrs.get("Wattage", "")
    shape = attrs.get("Bulb Shape", "").upper()

    base = f"{noun_word}{fil} LED"
    if lumens:
        base += f" {lumens}LUMENS"
    if wattage:
        base += f" {wattage}W"
    if shape:
        base += f" {shape}"
    if shape_code:
        base += f" {shape_code}"

    color_temp = attrs.get("Color Temperature", "")
    base_name = attrs.get("Bulb Base", "")
    base_code = attrs.get("Bulb Base Code", "")
    appearance = attrs.get("Light Appearance", "").lower()

    candidates = []
    if color_temp and not _is_range(color_temp):
        candidates.append(f"{color_temp}K")
    if base_name == "Medium":
        candidates.append("MDM")
    if base_code:
        candidates.append(base_code)
    if appearance in APPEARANCE_ABBREV:
        candidates.append(APPEARANCE_ABBREV[appearance])

    result = base
    for token in candidates:
        extended = f"{result} {token}"
        if len(extended) <= INVOICE_MAX_LEN:
            result = extended
    return result


def mobile_desc(mpn: str, manufacturer_name: str | None, attrs: dict[str, str], classpath: str | None = None) -> str:
    """Compressed UOM, comma-joined parts. Mixed case as observed in GT --
    NOT all-caps despite the field's compressed-UOM contract (verified
    against 22 real GT rows; only INVOICE_DESC is actually all-caps)."""
    brand = _brand(manufacturer_name, "mobile")
    noun = _noun(attrs, classpath)
    wattage = attrs.get("Wattage", "")
    # GT is inconsistent on compressing "Type X" -> "TypeX" here (some
    # rows keep the space, some don't, with no clean rule found) -- kept
    # spaced, which matches the GT majority for this field.
    shape = attrs.get("Bulb Shape", "")
    base = attrs.get("Bulb Base", "")
    finish = attrs.get("Bulb Finish", "")
    color_temp = attrs.get("Color Temperature", "")

    parts = [p for p in (brand, noun, mpn) if p]
    if wattage:
        parts.append(format_uom(wattage, "W", "MOBILE_DESC"))
    if shape:
        parts.append(shape)
    if base:
        parts.append(base)
    if finish:
        parts.append(finish)
    if color_temp and not _is_range(color_temp):
        parts.append(format_uom(color_temp, "K", "MOBILE_DESC"))
    return ", ".join(parts)


def _short_desc_body(attrs: dict[str, str], classpath: str | None) -> str:
    """The part of SHORT_DESC after "{brand} {mpn} " -- also IS
    RETAIL_DESC verbatim (GT-confirmed), so this is shared."""
    noun = _noun(attrs, classpath)
    wattage = attrs.get("Wattage", "")
    shape = attrs.get("Bulb Shape", "")
    base = attrs.get("Bulb Base", "")
    finish = attrs.get("Bulb Finish", "")
    color_temp = attrs.get("Color Temperature", "")
    lumens = attrs.get("Lumens", "")
    beam_angle = attrs.get("Beam Angle", "")

    parts = [noun + ","]
    if wattage:
        parts.append(format_uom(wattage, "W", "SHORT_DESC") + ",")
    if shape:
        parts.append(shape + ",")
    if base:
        parts.append(base + ",")
    if finish:
        parts.append(finish + ",")
    if color_temp:
        parts.append(format_uom(color_temp, "K", "SHORT_DESC") + ",")
    if lumens:
        parts.append(f"{lumens} Lumens,")
    if beam_angle:
        parts.append(format_uom(beam_angle, "deg Beam", "SHORT_DESC"))

    body = " ".join(parts).strip()
    return body[:-1] if body.endswith(",") else body


def short_desc(mpn: str, manufacturer_name: str | None, attrs: dict[str, str], classpath: str | None = None) -> str:
    brand = _brand(manufacturer_name, "short")
    body = _short_desc_body(attrs, classpath)
    prefix = " ".join(p for p in (brand, mpn) if p)
    return f"{prefix} {body}".strip()


def retail_desc(attrs: dict[str, str], classpath: str | None = None) -> str:
    """GT-confirmed: SHORT_DESC with the "{brand} {mpn} " prefix stripped.
    See module docstring -- deterministic, not LLM, on real-data evidence."""
    return _short_desc_body(attrs, classpath)


_PROSE_SYSTEM_PROMPT = """You write product descriptions for a B2B distributor catalog from a fixed \
set of validated product attributes. Use ONLY the facts given -- never invent a spec, feature, or \
certification that isn't in the attribute list. Reply with ONLY a JSON object with exactly two keys: \
"long_desc1" (a detailed single-paragraph description weaving in the key specs) and \
"marketing_description" (a short, benefit-focused marketing blurb, 1-3 sentences, no invented claims)."""


def _build_prose_prompt(mpn: str, manufacturer_name: str | None, classpath: str, attrs: dict[str, str]) -> str:
    non_empty = {k: v for k, v in attrs.items() if v}
    return (
        f"Product: {classpath.split('>')[-1]}\n"
        f"Manufacturer: {manufacturer_name or 'unknown'}\n"
        f"Part number: {mpn}\n"
        f"Attributes (JSON): {json.dumps(non_empty)}\n\nJSON:"
    )


async def generate_prose_descriptions(
    mpn: str,
    manufacturer_name: str | None,
    classpath: str,
    attrs: dict[str, str],
    api_key: str | None = None,
    model: str | None = None,
    client: AsyncGroq | None = None,
) -> dict[str, str]:
    """Returns {"long_desc1": ..., "marketing_description": ...} generated
    from the reconciled, already-validated attribute set. Empty dict on
    any failure (rate limit exhausted, bad JSON) -- caller leaves the
    fields empty rather than getting a half-formed value, per "never hide
    uncertainty, don't invent."

    Caching: raw completion cached under namespace='prose' (model-aware
    key). Re-runs of the same (mpn, classpath, attrs) tuple return the
    prior long_desc1+marketing_description for zero tokens -- the entire
    reason a 200-row eval can be re-iterated after a 100K TPD wall hit."""
    non_empty = {k: v for k, v in attrs.items() if v}
    if not non_empty:
        return {}

    model_name = model or GROQ_DESC_MODEL
    skip_live = quota_is_exhausted(model_name)
    prompt = _build_prose_prompt(mpn, manufacturer_name, classpath, attrs)

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
                        {"role": "system", "content": _PROSE_SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0.4,
                    max_tokens=600,
                    response_format={"type": "json_object"},
                )
                return response.choices[0].message.content
            except RateLimitError as exc:
                if is_daily_quota_exhausted(exc):
                    logger.warning(
                        "Groq generate_prose_descriptions() hit daily token quota -- "
                        "skipping retries, prose generation unavailable until quota resets"
                    )
                    mark_quota_exhausted(model_name)
                    return None
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(BACKOFF_BASE_SECONDS * (2 ** attempt))
                    continue
                return None
            except PermissionDeniedError:
                logger.warning("Groq generate_prose_descriptions() got 403 PermissionDenied -- skipping prose generation")
                return None
        return None

    raw = await llm_cache.cached_call(
        namespace="prose",
        model=model_name,
        system_prompt=_PROSE_SYSTEM_PROMPT,
        user_prompt=prompt,
        temperature=0.4,
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

    return {
        k: str(parsed[k]) for k in ("long_desc1", "marketing_description")
        if parsed.get(k)
    }
