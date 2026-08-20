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

One exception, added after GT verification: "Appliance Dealers Cooperative
(APPDE)" specifically. All 23 GT rows carrying this exact raw code turn out
to be fully resolvable from Part_Desc/MPN alone -- ADC is a co-op *billing*
code, not a manufacturer signal, but the co-op's own SKUs still carry the
real manufacturer's brand marker in the free-text description (21/23 rows)
or a manufacturer-specific MPN prefix (the remaining 2/23, which carry no
text signal at all). See _disambiguate_adc() below -- this is a targeted,
GT-mined secondary-signal lookup for this one raw code, not a general
"try harder on any ambiguous code" mechanism; every other ambiguous raw
string still routes straight to NEEDS_DISAMBIGUATION as before.
"""
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


_ADC_RAW_CODE = "Appliance Dealers Cooperative (APPDE)"

# GT-mined from all 23 rows carrying _ADC_RAW_CODE (gt_input_200.csv /
# gt_delivery_200.csv, verified 2026-08-19). Ordered rules, first match
# wins; each keyword regex requires a leading word boundary so it can't
# false-positive on a substring inside an unrelated word (checked against
# the full 200-row GT: zero non-ADC rows contain any of these tokens at
# all, so there's no cross-contamination risk from routing this table only
# off Part_Desc). "Caf" intentionally has no trailing \b -- the source
# text is "Caf\xe9" (Café) with a mis-decoded accented e that Python's \w
# already treats as a word character, so \bCaf\b never finds a boundary
# after the f; a leading-only boundary is still specific enough (no other
# GT row starts a word with "Caf").
#
# GE and LG additions (2026-08-20, Aug-20-handoff Item 3): GE was
# case-sensitive-only, missing lowercase/mixed-case renders like "Ge
# Dishwasher SS" (confirmed present in the 1000-row scale input, e.g.
# PDT715SYVFS); made case-insensitive. LG had no rule at all despite 6
# ADC-raw rows in the 1000-row set literally reading "LG Dishwasher",
# "LG Fridge", etc. (LDPH5554D, WKE100HWA, MSER2090S, LSEL6333ZE,
# LT18S2100W, LT18S2100SS). Checked all 84 ADC-raw rows in the 1000-row
# set for a false-positive risk (e.g. "Lg"/"LG" as a glove-size
# abbreviation, the trap that sank 3 of Finding B's original 18-row
# estimate) -- every LG/GE token in an ADC-raw row's Part_Desc is a real
# LG/GE appliance mention; size-abbreviation rows never carry the ADC raw
# code (they're a different Part_Manuf entirely), so this table -- which
# only ever runs for the exact ADC raw code -- can't reach that trap.
_ADC_KEYWORD_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bSQ\b"), "Alliance Laundry Systems LLC"),
    (re.compile(r"\bSpeed Queen\b", re.IGNORECASE), "Alliance Laundry Systems LLC"),
    (re.compile(r"\bGE\b", re.IGNORECASE), "Haier"),
    (re.compile(r"\bCaf"), "Haier"),
    (re.compile(r"\bBeko\b", re.IGNORECASE), "Beko"),
    (re.compile(r"\bLG\b", re.IGNORECASE), "LG Electronics"),
]

# MPN-prefix fallback, tried only when no keyword rule matched. Two of
# these (WDTS, PDSH) carry NO independent text signal in Part_Desc at all
# ("Dishwasher SS - Display Only") -- this is a single-row-mined exact
# prefix pin, the same confidence tier as enricher.py's SATCO_PDF_MIRRORS
# hardcoded URLs: it will only ever fire again on this literal prefix, not
# a learned pattern. XOU and MVWP are also GT-mined but are genuine
# manufacturer-specific SKU-line prefixes (XOU = XO Ventilation's own
# product-line code, already used elsewhere in this codebase's confirmed
# xoappliance.com URL pattern; MVWP = Whirlpool/Maytag's vertical-washer
# line prefix), not a coincidental short generic code.
_ADC_MPN_PREFIX_RULES: list[tuple[str, str]] = [
    ("XOU", "XO Ventilation"),
    ("MVWP", "Whirlpool Corporation"),
    ("WDTS", "Whirlpool Corporation"),
    ("PDSH", "Rheem Manufacturing"),
]

# Manufacturer-name-in-description fallback (GT-mined, 2026-08-19): tokens
# that are common/generic across manufacturer names and must not by
# themselves trigger a description match (e.g. "Manufacturing" appearing in
# "A-J Manufacturing Company" would otherwise false-positive on any row
# whose Part_Desc happens to contain the word "manufacturing").
_GENERIC_NAME_TOKENS = frozenset({
    "corporation", "company", "inc", "incorporated", "llc", "corp",
    "manufacturing", "industries", "holding", "holdings", "products",
    "systems", "group", "co", "ltd", "limited", "international", "usa",
})

# Non-mojibake trailing punctuation/symbols GT's brand columns tack onto
# trademark names (a garbled (R)/(TM) glyph, decoded as a single stray
# byte -- see brand_manufacturer_pairs.json's raw keys, e.g. "Schumacher�").
_TRAILING_SYMBOL_RE = re.compile(r"[^\w\s&.-]+$")


def _disambiguate_adc(part_desc: str | None, mpn: str | None) -> str | None:
    """Secondary-signal resolution for the ADC co-op code specifically --
    see the module docstring. Returns a canonical manufacturer name, or
    None when neither the Part_Desc keyword table nor the MPN-prefix table
    matches (caller falls through to the normal NEEDS_DISAMBIGUATION
    path -- this never forces a guess on a genuinely unrecognized row)."""
    if part_desc:
        for pattern, manufacturer in _ADC_KEYWORD_RULES:
            if pattern.search(part_desc):
                return manufacturer
    if mpn:
        mpn_upper = mpn.strip().upper()
        for prefix, manufacturer in _ADC_MPN_PREFIX_RULES:
            if mpn_upper.startswith(prefix):
                return manufacturer
    return None


def _resolve_from_brand(brand: str | None, brand_map: dict[str, str]) -> str | None:
    """Fallback for rows with a blank/placeholder Part_Manuf but a real
    brand column (E1_Brand/Unilog_Brand/DIB_Brand) -- e.g. GT row SL1672:
    Part_Manuf="-", DIB_Brand="Schumacher" -> "Schumacher Electric
    Corporation". brand_map is keyed on GT's raw brand strings (which carry
    a garbled trademark-symbol suffix); matched here after stripping that
    suffix so a clean "Schumacher" still hits "Schumacher�"."""
    if not brand:
        return None
    key = _TRAILING_SYMBOL_RE.sub("", brand.strip()).strip().lower()
    if not key:
        return None
    return brand_map.get(key)


def _resolve_from_description(part_desc: str | None, known_manufacturers: list[str]) -> str | None:
    """Fallback for rows with no usable Part_Manuf/brand signal at all but
    whose Part_Desc free text literally names the manufacturer -- e.g. GT
    row 1501767: Part_Desc contains "Huber" -> "J.M. Huber Corporation".
    Only fires on an UNAMBIGUOUS single match against a distinctive
    (non-generic) whole-word token from a known canonical manufacturer
    name, so it can't mis-fire on generic corporate-suffix words shared
    across many manufacturers."""
    if not part_desc:
        return None
    matches: set[str] = set()
    for name in known_manufacturers:
        for token in re.findall(r"[A-Za-z]+", name):
            if len(token) < 4 or token.lower() in _GENERIC_NAME_TOKENS:
                continue
            if re.search(rf"\b{re.escape(token)}\b", part_desc, re.IGNORECASE):
                matches.add(name)
                break
    if len(matches) == 1:
        return next(iter(matches))
    return None


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
    brand_pairs = json.loads((BOOTSTRAP / "brand_manufacturer_pairs.json").read_text(encoding="utf-8"))
    brand_map = {
        _TRAILING_SYMBOL_RE.sub("", k.strip()).strip().lower(): v
        for k, v in brand_pairs.items()
    }
    return raw_map, ambiguous, domain_map, brand_map


def resolve_manufacturer(
    part_manuf_raw: str | None,
    part_desc: str | None = None,
    mpn: str | None = None,
    brand: str | None = None,
) -> ResolutionResult:
    """part_desc/mpn are optional secondary-disambiguation signals, used
    only for the ADC co-op code (see _disambiguate_adc). brand is an
    optional fallback signal (E1_Brand/Unilog_Brand/DIB_Brand), tried only
    when part_manuf_raw itself is blank -- see _resolve_from_brand and
    _resolve_from_description. Every other raw string's resolution is
    unaffected by whether these are passed."""
    raw_map, ambiguous, domain_map, brand_map = _load()

    if not part_manuf_raw:
        brand_resolved = _resolve_from_brand(brand, brand_map)
        if brand_resolved:
            return ResolutionResult(
                status="RESOLVED",
                manufacturer_name=brand_resolved,
                domain=domain_map.get(brand_resolved),
                matched_raw=f"[brand fallback: {brand!r}]",
                match_score=100.0,
            )
        desc_resolved = _resolve_from_description(part_desc, list(domain_map.keys()))
        if desc_resolved:
            return ResolutionResult(
                status="RESOLVED",
                manufacturer_name=desc_resolved,
                domain=domain_map.get(desc_resolved),
                matched_raw="[description fallback]",
                match_score=100.0,
            )
        return ResolutionResult(status="UNRESOLVED")

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
        if raw == _ADC_RAW_CODE:
            disambiguated = _disambiguate_adc(part_desc, mpn)
            if disambiguated:
                return ResolutionResult(
                    status="RESOLVED",
                    manufacturer_name=disambiguated,
                    domain=domain_map.get(disambiguated),
                    matched_raw=f"{raw} [ADC secondary-signal match]",
                    match_score=100.0,
                )
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
