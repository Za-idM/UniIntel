"""
Run the orchestrator (Stage 1 classify -> Stage 3 enrich -> Stage 4
extract/reconcile) end-to-end over all 22 LED Light Bulb ground-truth rows
and report aggregate accuracy against gt_delivery_200.csv.

gt_delivery_200.csv carries every input column too (PART_NUMBER, Dept,
Class, Fine, SKU, Mfg_Part_Num, Part_Desc, E1_Brand, Unilog_Brand,
DIB_Brand, Part_Manuf) alongside the labeled output columns, so the 22 LED
rows are used directly as both input and ground truth -- no separate join
against gt_input_200.csv needed.

Field accuracy only counts GT slots that are non-empty (matches the
"never invent values" framing: an empty GT slot the pipeline also leaves
empty isn't a meaningful correctness signal either way).
"""
import asyncio
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from pipeline.orchestrator import process_job  # noqa: E402
from persistence.db import init_db  # noqa: E402

GT_DELIVERY = ROOT / "data" / "ground_truth" / "gt_delivery_200.csv"
LED_CLASSPATH = "Electrical>Lamps & Lightings>Light Bulbs>LED Light Bulbs"

# See evaluate_orchestrator_full.py for why these 5 answer-leak cribs
# (PART_NUMBER, Dept, Class, Fine, SKU - MY_PART_NUMBER) are dropped.
INPUT_COLS = [
    "Mfg_Part_Num", "Part_Desc", "E1_Brand", "Unilog_Brand", "DIB_Brand", "Part_Manuf",
]


def load_led_rows() -> list[dict]:
    with open(GT_DELIVERY, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    return [r for r in rows if r.get("Classpath", "").strip() == LED_CLASSPATH]


def gt_attribute_values(gt_row: dict) -> dict[str, str]:
    """{label: value} for the 27 ATTRIBUTE_LABEL/VALUE pairs, non-empty only."""
    out = {}
    for i in range(1, 51):
        label = gt_row.get(f"ATTRIBUTE_LABEL {i}", "").strip()
        value = gt_row.get(f"ATTRIBUTE_VALUE {i}", "").strip()
        if label and value:
            out[label] = value
    return out


def main():
    # See evaluate_orchestrator_full.py: standalone eval has no FastAPI
    # lifespan to run init_db(), so do it here (idempotent).
    init_db()

    gt_rows = load_led_rows()
    assert len(gt_rows) == 22, f"expected 22 LED Light Bulb GT rows, found {len(gt_rows)}"

    input_rows = [{k: r[k] for k in INPUT_COLS} for r in gt_rows]
    products = asyncio.run(process_job(input_rows, job_id="gt-eval"))

    by_mpn = {p.mfg_part_num: p for p in products}

    classpath_correct = 0
    manufacturer_correct = 0
    field_correct = 0
    field_total = 0
    enrichment_fetched = 0
    per_row = []

    for gt_row in gt_rows:
        mpn = gt_row["Mfg_Part_Num"]
        product = by_mpn.get(mpn)
        if product is None:
            per_row.append((mpn, "MISSING FROM OUTPUT", 0, 0))
            continue

        classpath_ok = product.classpath == LED_CLASSPATH
        classpath_correct += classpath_ok

        manufacturer_ok = product.manufacturer_name == gt_row.get("MANUFACTURER_NAME", "").strip()
        manufacturer_correct += manufacturer_ok

        gt_attrs = gt_attribute_values(gt_row)
        produced_attrs = {a.label: a.value for a in product.attributes if a.value}

        row_correct = sum(1 for label, val in gt_attrs.items() if produced_attrs.get(label) == val)
        row_total = len(gt_attrs)
        field_correct += row_correct
        field_total += row_total

        per_row.append((mpn, f"classpath={'OK' if classpath_ok else 'MISS'} mfr={'OK' if manufacturer_ok else 'MISS'}",
                         row_correct, row_total))

    print(f"Rows evaluated: {len(gt_rows)}")
    print(f"Classpath accuracy: {classpath_correct}/{len(gt_rows)} ({100 * classpath_correct / len(gt_rows):.1f}%)")
    print(f"Manufacturer accuracy: {manufacturer_correct}/{len(gt_rows)} ({100 * manufacturer_correct / len(gt_rows):.1f}%)")
    if field_total:
        print(f"Attribute field accuracy (non-empty GT slots only): {field_correct}/{field_total} ({100 * field_correct / field_total:.1f}%)")
    else:
        print("Attribute field accuracy: no non-empty GT slots matched (no extraction succeeded)")

    print()
    print("Per-row detail (mpn, classpath/mfr match, fields correct/total):")
    for mpn, status, correct, total in per_row:
        print(f"  {mpn:12s} {status:35s} {correct}/{total}")


if __name__ == "__main__":
    main()
