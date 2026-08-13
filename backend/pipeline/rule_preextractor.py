"""
L2b rule pre-extraction: regex priors pulled straight from Part_Desc before
any LLM involvement. Cheap signal for Stage 4 reconciliation to prefer over
weaker inference.
"""
import re

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
