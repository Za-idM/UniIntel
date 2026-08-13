"""
Initializes the SQLite schema and loads the GT-mined bootstrap artifacts
(data/bootstrap/*.json, produced by build_gt_seeds.py) as the reference
indexes used by backend/lookup/*.

Full reference pack (LOV ~161K rows, manufacturer list ~27K, UOM standards,
decimal/fraction table) is NOT yet available (Locked Decision #7) -- this
script only wires up the GT-bootstrapped subset. Re-run once the full pack
arrives; lookups degrade gracefully (fuzzy-match miss -> flag) without it.

Run from the uniintel/ directory: python scripts/load_reference_data.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from persistence.db import init_db  # noqa: E402

BOOTSTRAP = ROOT / "data" / "bootstrap"
REQUIRED_ARTIFACTS = [
    "leaf_templates.json",
    "manufacturer_domain_map.json",
    "brand_manufacturer_pairs.json",
    "lov_by_classpath.json",
]


def main():
    missing = [f for f in REQUIRED_ARTIFACTS if not (BOOTSTRAP / f).exists()]
    if missing:
        print(f"Missing bootstrap artifacts: {missing}")
        print("Run scripts/build_gt_seeds.py first.")
        sys.exit(1)

    init_db()
    print("SQLite schema initialized.")

    for name in REQUIRED_ARTIFACTS:
        data = json.loads((BOOTSTRAP / name).read_text(encoding="utf-8"))
        print(f"  {name}: {len(data)} entries (loaded OK, served directly from JSON by backend/lookup/*)")

    print("\nReference pack status: BOOTSTRAP-ONLY (GT-seeded).")
    print("Full LOV (~161K)/manufacturer (~27K)/UOM/fraction reference pack not yet received.")
    print("Email support+unihack@hack2skill.com; 1000-row scale run will degrade gracefully until then.")


if __name__ == "__main__":
    main()
