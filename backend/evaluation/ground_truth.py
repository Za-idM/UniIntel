"""GT loading for evaluation: the 200-row delivery set, indexed by MPN."""
import csv
from functools import lru_cache
from pathlib import Path

GT_DELIVERY = Path(__file__).resolve().parent.parent / "data" / "ground_truth" / "gt_delivery_200.csv"


@lru_cache(maxsize=1)
def _load_rows() -> list[dict]:
    with open(GT_DELIVERY, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


@lru_cache(maxsize=1)
def by_mpn() -> dict[str, dict]:
    return {r["Mfg_Part_Num"]: r for r in _load_rows() if r.get("Mfg_Part_Num")}


def gt_attributes(gt_row: dict) -> dict[str, str]:
    """{label: value} for the row's ATTRIBUTE_LABEL/VALUE N pairs,
    non-empty only (matches the "never invent" framing: an empty GT slot
    the pipeline also leaves empty isn't a meaningful correctness signal
    either way)."""
    out = {}
    for i in range(1, 51):
        label = gt_row.get(f"ATTRIBUTE_LABEL {i}", "").strip()
        value = gt_row.get(f"ATTRIBUTE_VALUE {i}", "").strip()
        if label and value:
            out[label] = value
    return out
