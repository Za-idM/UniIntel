"""
L2b rule pre-extraction: regex priors pulled straight from Part_Desc before
any LLM involvement. Cheap signal for Stage 4 reconciliation to prefer over
weaker inference.
"""
import json
import re
from functools import lru_cache
from pathlib import Path

_BOOTSTRAP = Path(__file__).resolve().parent.parent / "data" / "bootstrap"

# Ordered so longer/more specific unit tokens are tried before short ones
# that could be substrings (e.g. "in" inside "min").
_UNIT_PATTERN = re.compile(
    r"(?P<value>\d+(?:\.\d+)?)\s*(?P<uom>W|V|K|A|hr|deg|lm|in\b|mm|cm|ft)\b",
    re.IGNORECASE,
)

UOM_CANONICAL = {
    "w": "W", "v": "V", "k": "K", "a": "A", "hr": "hr", "deg": "deg",
    "lm": "Lumens", "in": "in", "mm": "mm", "cm": "cm", "ft": "ft",
}


def extract_uom_priors(text: str) -> list[dict[str, str]]:
    """Pulls (value, uom) pairs like "8W" -> {"value": "8", "uom": "W"} out
    of a raw description string. Order-preserving, duplicates kept (a
    description can legitimately mention the same unit twice)."""
    if not text:
        return []
    priors = []
    for match in _UNIT_PATTERN.finditer(text):
        uom_raw = match.group("uom").lower()
        canonical_uom = UOM_CANONICAL.get(uom_raw, match.group("uom"))
        value = match.group("value")
        if canonical_uom == "K" and "." not in value and int(value) < 100:
            # lighting-industry shorthand: "27k" means 2700K, not literal
            # 27K -- real color temperatures are always >= 1000K. Doc
            # example: "S21354 8W Led T9 Med 27k" -> Color Temperature 2700 K.
            value = str(int(value) * 100)
        priors.append({"value": value, "uom": canonical_uom})
    return priors


# Material/shape keywords that map directly to a template slot label when
# spotted in free text. Seeded narrow -- extend as more leaves are covered.
MATERIAL_KEYWORDS = {
    "stainless steel": "Material", "ss": "Material", "brass": "Material",
    "aluminum": "Material", "plastic": "Material", "steel": "Material",
}

SHAPE_KEYWORDS = {
    "t9": "Bulb Shape Code", "a19": "Bulb Shape Code", "br30": "Bulb Shape Code",
    "tube": "Bulb Shape", "round": "Bulb Shape",
}


LED_BULBS_CLASSPATH = "Electrical>Lamps & Lightings>Light Bulbs>LED Light Bulbs"


@lru_cache(maxsize=1)
def _led_shape_code_lov() -> tuple[str, ...]:
    """Bulb Shape Code's LOV for the LED Light Bulbs leaf, GT-mined into
    lov_by_classpath.json. Sorted longest-first so a word-boundary scan
    doesn't false-positive on a shorter code being a prefix of a longer
    one still present in the text (e.g. "A19" isn't currently a prefix of
    anything else in this LOV, but ordering defensively costs nothing)."""
    path = _BOOTSTRAP / "lov_by_classpath.json"
    if not path.exists():
        return ()
    data = json.loads(path.read_text(encoding="utf-8"))
    codes = data.get(LED_BULBS_CLASSPATH, {}).get("Bulb Shape Code", [])
    return tuple(sorted(codes, key=len, reverse=True))


def extract_led_shape_code(text: str) -> str | None:
    """Word-boundary, case-insensitive match of an LED Bulb Shape Code
    LOV value (A19, BR30, T19, ...) directly out of Part_Desc -- e.g.
    "576496 45W Led R20 Med 27k" -> "R20". Returns None when no known
    code appears in the text; this is LOV-constrained lookup, not
    inference, so an unrecognized shape token (or none at all) correctly
    yields nothing rather than a guess.

    "ST19" is itself a distinct LOV value (Satco's own naming for a
    tube-shape SKU) so it's matched and returned as-is here; GT shows
    Philips SKUs use the same literal "ST19" token in Part_Desc despite
    their own canonical shape code being "T19" -- that alias is handled
    by the caller (pipeline/led_philips_templates.py), not here, since
    this function's contract is "return the LOV value actually present
    in the text", not a manufacturer-specific remap."""
    if not text:
        return None
    for code in _led_shape_code_lov():
        if re.search(rf"\b{re.escape(code)}\b", text, re.IGNORECASE):
            return code
    return None


def extract_keyword_priors(text: str) -> dict[str, str]:
    """Case-insensitive keyword spot -> {slot_label: matched_value}."""
    if not text:
        return {}
    lowered = text.lower()
    hits = {}
    for keyword, label in {**MATERIAL_KEYWORDS, **SHAPE_KEYWORDS}.items():
        if re.search(rf"\b{re.escape(keyword)}\b", lowered):
            hits[label] = keyword
    return hits
