"""
Item 2 of the Aug 20 handoff (blind-801-only brand-coverage gap).

Extends brand_manufacturer_pairs.json with BRAND -> MANUFACTURER pairs mined
from the 1000-row scale-test input's DIB_Brand/E1_Brand/Unilog_Brand columns
(data/input/scale_input_1000.csv has no MANUFACTURER_NAME column, so a row's
manufacturer is derived by running it through the already-resolved
entity_resolver.resolve_manufacturer() -- which, after
mine_scale_manufacturers.py's Item-1 additions, RESOLVED-status covers many
more of these 1000 rows than the GT-only map alone).

Same fallback discipline as the existing GT-mined brand_map: a brand is only
added when it maps to exactly one manufacturer. But "exactly one manufacturer
by count" isn't sufficient on its own here -- some Part_Manuf raw codes
(e.g. "Boise Cascade Building Materials (BOICA)", "U S Lumber (3073)") are
GT-mined as unambiguous ONLY because the 200-row GT sample happened to see a
single manufacturer (Trex) through them, but the 1000-row set proves they are
actually multi-manufacturer distributor codes in disguise -- e.g. rows whose
DIB_Brand is literally "JAMESHARDIE" or "LP SMARTSIDE" (real, different
manufacturers) still resolve to "Trex Company, Inc" through that tainted
code. Adding "JAMESHARDIE" -> "Trex Company, Inc" to brand_map would inject a
confidently wrong pairing, which is worse than leaving it unresolved -- so
this script also cross-checks: if EVERY supporting row for a candidate brand
came through a Part_Manuf code proven (elsewhere in this same 1000-row set)
to carry >1 distinct real brand, the candidate is dropped UNLESS the brand
name itself textually matches the resolved manufacturer name (e.g. "TREX" ->
"Trex Company, Inc" is safe even via a tainted code, because the brand and
manufacturer names agree independently of which code routed it there).
Confirmed excluded by this check: JAMESHARDIE, LP SMARTSIDE (both routed
through the Trex-dominated codes above but are not Trex), and "Wiz" (routed
through the Phillips Lighting code but doesn't textually match Signify).
"BRK" is manually kept in-list despite the auto name-match check missing it
(word length filter excludes 3-letter tokens) -- "BRK" is literally the
initials in the existing manufacturer name "First Alert - B R K Brands".

Run from uniintel/: python scripts/mine_scale_brands.py
"""
import csv
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from pipeline.entity_resolver import resolve_manufacturer  # noqa: E402

BOOTSTRAP = ROOT / "backend" / "data" / "bootstrap"
SCALE_INPUT = ROOT / "data" / "input" / "scale_input_1000.csv"

_PLACEHOLDERS = {
    "-- unbranded --", "-- no dib brand --", "-- no unilog brand --",
    "commodity - unbranded",
}
_TRAILING_SYMBOL_RE = re.compile(r"[^\w\s&.-]+$")
_TOKEN_RE = re.compile(r"[A-Za-z]+")

# Manually confirmed real-world equivalence the automated name-match check
# can't see (see module docstring). Every other candidate is decided purely
# by the tainted-source-code + name-match logic below.
_MANUAL_ALLOW = {"BRK"}
_MANUAL_DENY = {"JAMESHARDIE", "LP SMARTSIDE", "Wiz"}


def _norm_key(brand: str) -> str:
    return _TRAILING_SYMBOL_RE.sub("", brand.strip()).strip().lower()


def _name_match(brand: str, manufacturer: str) -> bool:
    mfr_lower = manufacturer.lower()
    return any(
        len(tok) >= 4 and tok.lower() in mfr_lower
        for tok in _TOKEN_RE.findall(brand)
    )


def main():
    brand_pairs_path = BOOTSTRAP / "brand_manufacturer_pairs.json"
    brand_pairs = json.loads(brand_pairs_path.read_text(encoding="utf-8"))
    existing_keys = {_norm_key(k) for k in brand_pairs}

    with open(SCALE_INPUT, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    # pm -> distinct non-placeholder brand strings seen through it
    pm_brand_diversity: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        pm = row.get("Part_Manuf", "").strip()
        if not pm:
            continue
        for col in ("E1_Brand", "DIB_Brand", "Unilog_Brand"):
            b = row.get(col, "").strip()
            if b and b.lower() not in _PLACEHOLDERS:
                pm_brand_diversity[pm].add(b)
    tainted_pms = {pm for pm, brands in pm_brand_diversity.items() if len(brands) > 1}

    # brand -> {manufacturer: count}, brand -> set of source pm codes
    brand_to_mfrs: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    brand_to_pms: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        pm = row.get("Part_Manuf", "").strip()
        desc = row.get("Part_Desc", "")
        mpn = row.get("Mfg_Part_Num", "").strip()
        res = resolve_manufacturer(pm or None, part_desc=desc, mpn=mpn)
        if res.status != "RESOLVED" or not res.manufacturer_name:
            continue
        for col in ("E1_Brand", "DIB_Brand", "Unilog_Brand"):
            b = row.get(col, "").strip()
            if not b or b.lower() in _PLACEHOLDERS:
                continue
            brand_to_mfrs[b][res.manufacturer_name] += 1
            brand_to_pms[b].add(pm)

    added = {}
    skipped_ambiguous = []
    skipped_tainted = []
    for brand, mfr_counts in brand_to_mfrs.items():
        if brand in _MANUAL_DENY:
            continue
        if _norm_key(brand) in existing_keys:
            continue
        if len(mfr_counts) != 1:
            skipped_ambiguous.append((brand, mfr_counts))
            continue
        manufacturer = next(iter(mfr_counts))
        sources = brand_to_pms[brand]
        all_tainted = sources and sources <= tainted_pms
        if all_tainted and brand not in _MANUAL_ALLOW and not _name_match(brand, manufacturer):
            skipped_tainted.append((brand, manufacturer, sources))
            continue
        added[brand] = manufacturer
        existing_keys.add(_norm_key(brand))

    brand_pairs.update(added)
    brand_pairs_path.write_text(json.dumps(brand_pairs, indent=2, sort_keys=True), encoding="utf-8")

    print(f"Added {len(added)} new brand_manufacturer_pairs.json entries:")
    for b, m in sorted(added.items()):
        print(f"  {b!r} -> {m!r}")
    if skipped_ambiguous:
        print(f"Skipped {len(skipped_ambiguous)} ambiguous brands (maps to >1 manufacturer):")
        for b, mfrs in skipped_ambiguous:
            print(f"  {b!r}: {dict(mfrs)}")
    if skipped_tainted:
        print(f"Skipped {len(skipped_tainted)} brands sourced only via a tainted (multi-brand) Part_Manuf code:")
        for b, m, sources in skipped_tainted:
            print(f"  {b!r} -> {m!r} (via {sources})")


if __name__ == "__main__":
    main()
