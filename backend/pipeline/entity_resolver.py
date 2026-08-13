"""
Manufacturer resolution.

Finding from real GT (see data/ground_truth): Part_Manuf is frequently a
DISTRIBUTOR/CO-OP CODE, not the manufacturer, and is often textually
unrelated to the canonical manufacturer name -- e.g.
  "Phillips Lighting (5831)" -> "Signify Holding"      (19/22 LED bulb rows)
  "Palmer Donavin Mfg Company (PALDO)" -> "Digger Specialties Inc"
Fuzzy-matching Part_Manuf text against a manufacturer name list (the
architecture doc's original plan) would fail on exactly these majority
cases. Resolution here is LOOKUP-FIRST against a mined
raw-string -> canonical map; rapidfuzz is used only to catch typo/format
variants of a raw string already seen in GT, not to discover canonical
names from unrelated text.

Some raw codes are genuinely ambiguous (e.g. "Appliance Dealers Cooperative"
covers 6+ manufacturers) -- these must NOT be auto-resolved; they route to
NEEDS_DISAMBIGUATION and need a secondary signal (brand, product page) to
resolve, which is out of scope for this deterministic stage.
"""

'''hello'''
import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from rapidfuzz import fuzz, process

BOOTSTRAP = Path(__file__).resolve().parent.parent / "data" / "bootstrap"

ACCEPT_THRESHOLD = 85
REVIEW_THRESHOLD = 70

_CODE_SUFFIX_RE = re.compile(r"\s*\([^)]*\)\s*$")


def _strip_code_suffix(raw: str) -> str:
    """Drop a trailing '(CODE)' distributor-id suffix so fuzzy matching scores
    the actual name, not shared boilerplate like 'Co ('. WRatio on the
    unstripped string gave "Totally Unknown Distributor Co (999999)" an
    85.5 match against "Hager Hinge Co (4189)" -- a false positive driven
    entirely by the parenthetical/"Co" pattern, not the name."""
    return _CODE_SUFFIX_RE.sub("", raw).strip()


@dataclass
class ResolutionResult:
    status: str  # RESOLVED, NEEDS_REVIEW, NEEDS_DISAMBIGUATION, UNRESOLVED
    manufacturer_name: str | None = None
    domain: str | None = None
    matched_raw: str | None = None
    match_score: float | None = None
    candidates: list[str] | None = None  # for NEEDS_DISAMBIGUATION


@lru_cache(maxsize=1)
def _load():
    raw_map = json.loads((BOOTSTRAP / "raw_manufacturer_map.json").read_text(encoding="utf-8"))
    ambiguous = json.loads((BOOTSTRAP / "raw_manufacturer_ambiguous.json").read_text(encoding="utf-8"))
    domain_map = json.loads((BOOTSTRAP / "manufacturer_domain_map.json").read_text(encoding="utf-8"))
    return raw_map, ambiguous, domain_map


def resolve_manufacturer(part_manuf_raw: str | None) -> ResolutionResult:
    if not part_manuf_raw:
        return ResolutionResult(status="UNRESOLVED")

    raw_map, ambiguous, domain_map = _load()
    raw = part_manuf_raw.strip()

    # exact match
    if raw in raw_map:
        canonical = raw_map[raw]
        return ResolutionResult(
            status="RESOLVED",
            manufacturer_name=canonical,
            domain=domain_map.get(canonical),
            matched_raw=raw,
            match_score=100.0,
        )

    if raw in ambiguous:
        return ResolutionResult(status="NEEDS_DISAMBIGUATION", candidates=ambiguous[raw])

    # fuzzy match against known raw strings (typo/format variants only),
    # scored on the code-stripped name to avoid boilerplate-driven false
    # positives (see _strip_code_suffix docstring)
    stripped_query = _strip_code_suffix(raw)
    stripped_to_raw = {_strip_code_suffix(r): r for r in raw_map.keys()}
    match = process.extractOne(stripped_query, stripped_to_raw.keys(), scorer=fuzz.token_sort_ratio)
    if match is None:
        return ResolutionResult(status="UNRESOLVED")

    matched_stripped, score, _ = match
    matched_raw = stripped_to_raw[matched_stripped]
    canonical = raw_map[matched_raw]
    if score >= ACCEPT_THRESHOLD:
        status = "RESOLVED"
    elif score >= REVIEW_THRESHOLD:
        status = "NEEDS_REVIEW"
    else:
        return ResolutionResult(status="UNRESOLVED")

    return ResolutionResult(
        status=status,
        manufacturer_name=canonical,
        domain=domain_map.get(canonical),
        matched_raw=matched_raw,
        match_score=score,
    )
