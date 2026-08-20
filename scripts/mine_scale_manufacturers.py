"""
Item 1 of the Aug 20 handoff (blind-801-only manufacturer-resolution gap).

Adds self-mapping raw_manufacturer_map.json entries for Part_Manuf strings
seen in the 1000-row scale-test input (data/input/scale_input_1000.csv) that
are NOT covered by the 200-row-GT-mined map, but whose code-stripped text
literally already IS a real manufacturer name (not a distributor/co-op code
routing to someone else -- see entity_resolver.py's module docstring for why
that distinction matters; Part_Manuf is often a distributor code, and this
script must not blindly assume otherwise).

This is NOT a general auto-miner: `build_gt_seeds.py` mines purely from GT,
which is safe because MANUFACTURER_NAME is a labeled column there. The
1000-row input has no such label, so "does this stripped string reflect a
correct manufacturer" needed a human read of each candidate's Part_Desc
rows before inclusion (methodology below), not just a blind self-map.

Methodology used to build INCLUDE_RAW below (manually reviewed, 2026-08-20):
for every unresolved code-stripped Part_Manuf string with >=1 occurrence in
the 1000-row set, its Part_Desc rows were read for a DIFFERENT, clearly
distinct, well-known brand name appearing in the free text -- the same
"Part_Manuf is a distributor, not the manufacturer" trap CLAUDE.md documents
for Phillips/Signify. Confirmed real examples of this trap found in the
1000-row set (EXCLUDED, not in INCLUDE_RAW):
  - "Parksite (6151)" (55 rows) -- every row is AZEK-brand PVC decking
    ("Vintage Azek PVC Decking"); Parksite is a building-products
    distributor, same pattern as the existing map's "U S Lumber (3073)" /
    "Boise Cascade Building Materials (BOICA)" both -> "Trex Company, Inc".
  - "Jam Industrial Supply LLC (JAMIN)" -- rows are 3M-brand Cubitron II
    sanding discs.
  - "National Nail Corp (7439)" -- one row is a Paslode-brand nailer.
  - "Fenton Bros Electric Inc (FENBR)" -- rows are Lutron-brand dimmers.
  - "Radians (7363)" / "Metalmark Industrial Inc (METIN)" -- rows reference
    Dewalt-branded product lines (licensed PPE / compatible mounts);
    ambiguous whether Radians/Metalmark or Dewalt is the true manufacturer
    of record, excluded on that ambiguity alone.
  - "Woodstock Intl (3658)" -- rows are Grizzly-brand table saws; Woodstock
    International is Grizzly's parent entity but this wasn't independently
    confirmable from the data alone, excluded per "don't guess".
  - "V & V Appliance Parts Inc (VVAPP)" / "Marshalltown Trowel (5155)" --
    reseller-shaped name ("...Parts Inc") or mixed unrelated product
    categories across its rows with no confirming brand mention; excluded.
Every string in INCLUDE_RAW below was checked against this same test and
had no conflicting-brand signal in any of its Part_Desc rows.

Run from uniintel/: python scripts/mine_scale_manufacturers.py
"""
import csv
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BOOTSTRAP = ROOT / "backend" / "data" / "bootstrap"
SCALE_INPUT = ROOT / "data" / "input" / "scale_input_1000.csv"
GT_INPUT = ROOT / "data" / "ground_truth" / "gt_input_200.csv"

_CODE_SUFFIX_RE = re.compile(r"\s*\([^)]*\)\s*$")


def _strip_code_suffix(raw: str) -> str:
    return _CODE_SUFFIX_RE.sub("", raw).strip()


# Manually reviewed (see module docstring) -- exact raw Part_Manuf strings
# (with their distributor-code suffix, matching raw_manufacturer_map.json's
# existing key convention) that are confirmed to self-map: the stripped text
# IS the correct manufacturer name, with no conflicting-brand signal found
# in any of that string's Part_Desc rows in the 1000-row set.
INCLUDE_RAW = [
    "Premier Metals (PREME)",
    "Vessel Tools USA Inc (VESTO)",
    "Prime Wire & Cable (3562)",
    "Bow Products (BOWPR)",
    "Saw Stop LLC (SAWST)",
    "Rees Cast Stone Company (REECA)",
    "Westwood Lumber Sales (WESLU)",
    "Woodpeckers Inc (WOODP)",
    "Robt Bosch Tool Corp (6564)",
    "Square D Con Prod Dv (6825)",
    "Whiteside Machine & Repair Co (WHIMA)",
    "ProVia (PRODO)",
    "Certainteed Gypsum (2765)",
    "Feit Electric (3468)",
    "ACG Brands (1154)",
    "Prebena (PREBE)",
    "First Alert - B R K Brands (2754)",
    "King Canada Inc (KINCA)",
    "3 M Co (5293)",
    "Emseal Joint Systems Ltd (EMSJO)",
    "MillerTech Energy Solutions (MILTE)",
    "Thomas & Betts (7405)",
    "Cooper Wiring Devices (3560)",
    "Keystone (5702)",
    "Maxsa Innovations (MAXIN)",
    "Streamlight (7277)",
    "Sabre (9195)",
    "Amana Tool Corp (AMATO)",
    "Irwin Industrial Tools (5863)",
    "J&G Machinery (JGMAC)",
    "Kreg Tool Company (KRETO)",
    "Festool USA (FESTO)",
    "Hunter Fan Co (4381)",
    "Tech Gear 5.7 Inc (TECGE)",
]


def load_scale_rows():
    with open(SCALE_INPUT, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def load_gt_mpns():
    with open(GT_INPUT, encoding="utf-8-sig") as f:
        return {r.get("Mfg_Part_Num", "").strip() for r in csv.DictReader(f)}


def main():
    raw_map_path = BOOTSTRAP / "raw_manufacturer_map.json"
    raw_map = json.loads(raw_map_path.read_text(encoding="utf-8"))
    ambiguous = json.loads((BOOTSTRAP / "raw_manufacturer_ambiguous.json").read_text(encoding="utf-8"))

    rows = load_scale_rows()
    gt_mpns = load_gt_mpns()

    added = {}
    for raw in INCLUDE_RAW:
        if raw in raw_map:
            print(f"SKIP (already in raw_manufacturer_map.json): {raw!r}")
            continue
        if raw in ambiguous:
            raise SystemExit(f"REFUSING: {raw!r} is in raw_manufacturer_ambiguous.json -- do not self-map an ambiguous code")
        added[raw] = _strip_code_suffix(raw)

    affected_rows = 0
    gt_overlap_rows = 0
    for row in rows:
        pm = row.get("Part_Manuf", "").strip()
        if pm in added:
            affected_rows += 1
            if row.get("Mfg_Part_Num", "").strip() in gt_mpns:
                gt_overlap_rows += 1

    if gt_overlap_rows:
        raise SystemExit(
            f"REFUSING: {gt_overlap_rows} affected 1000-row rows overlap the 200-row GT set "
            f"by Mfg_Part_Num -- this must stay a blind-801-only change."
        )

    raw_map.update(added)
    raw_map_path.write_text(json.dumps(raw_map, indent=2, sort_keys=True), encoding="utf-8")

    print(f"Added {len(added)} new self-mapped raw_manufacturer_map.json entries.")
    print(f"1000-row rows newly resolvable: {affected_rows}")
    print(f"GT overlap (must be 0): {gt_overlap_rows}")


if __name__ == "__main__":
    main()
