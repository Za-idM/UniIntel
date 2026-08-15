"""
Run the orchestrator (Stage 1 classify -> Stage 3 enrich -> Stage 4
extract/reconcile -> Stage 5 descriptions) end-to-end over all 200 ground
-truth rows (not just the 22 LED Light Bulb rows evaluate_orchestrator.py
covers) and report aggregate manufacturer/attribute/description accuracy
against gt_delivery_200.csv.

This is the "existing evaluation script" extended to the full 200-row set
referenced when diagnosing the ~7.7% attribute-accuracy / 83.5%
manufacturer-accuracy numbers -- evaluate_orchestrator.py alone only ever
covered the 22-row LED slice, so it can't reproduce or verify a fix against
those numbers.

Pass --debug to enable orchestrator.py's per-row DEBUG logging (classpath,
enrichment.status, which extraction path ran, and what keys it returned).
"""
import argparse
import asyncio
import csv
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from pipeline.orchestrator import process_job  # noqa: E402
from persistence.db import init_db  # noqa: E402
from persistence.llm_cache import hit_miss_summary, reset_hit_miss, stats  # noqa: E402

GT_DELIVERY = ROOT / "data" / "ground_truth" / "gt_delivery_200.csv"

# Only the 6 input columns a real Unilog feed provides per the 1000-row
# scale_input spec. The 5 columns dropped here (PART_NUMBER, Dept, Class,
# Fine, SKU - MY_PART_NUMBER) are GT-side answer-leak cribs -- they would
# short-circuit Stage 1 classification by handing the eval script the
# classpath hierarchy (Dept/Class/Fine) on the same row it's supposed to
# predict. Keep them OUT of the orchestrator's input slice.
INPUT_COLS = [
    "Mfg_Part_Num", "Part_Desc", "E1_Brand", "Unilog_Brand", "DIB_Brand", "Part_Manuf",
]


def load_rows(limit: int | None = None) -> list[dict]:
    with open(GT_DELIVERY, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    return rows[:limit] if limit else rows


def gt_attribute_values(gt_row: dict) -> dict[str, str]:
    out = {}
    for i in range(1, 51):
        label = gt_row.get(f"ATTRIBUTE_LABEL {i}", "").strip()
        value = gt_row.get(f"ATTRIBUTE_VALUE {i}", "").strip()
        if label and value:
            out[label] = value
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--debug", action="store_true", help="enable orchestrator per-row DEBUG logging")
    parser.add_argument("--limit", type=int, default=None, help="only evaluate the first N rows")
    args = parser.parse_args()

    if args.debug:
        logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(name)s: %(message)s")
    else:
        logging.basicConfig(level=logging.WARNING)

    reset_hit_miss()

    # The orchestrator + llm_cache both assume the SQLite schema (jobs/
    # job_stages/products/attributes/corrections/audit_logs/llm_cache) is
    # already initialized -- main.py does this via FastAPI lifespan, but a
    # standalone eval run has no server. init_db() is idempotent (CREATE
    # TABLE IF NOT EXISTS), so calling it here is free on a warm DB.
    init_db()

    gt_rows = load_rows(args.limit)
    input_rows = [{k: r.get(k, "") for k in INPUT_COLS} for r in gt_rows]
    products = asyncio.run(process_job(input_rows, job_id="gt-eval-full"))

    by_mpn: dict[str, list] = {}
    for p in products:
        by_mpn.setdefault(p.mfg_part_num, []).append(p)

    classpath_correct = 0
    manufacturer_correct = 0
    field_correct = 0
    field_total = 0
    row_errors = 0
    desc_scores = {"invoice": 0, "mobile": 0, "short": 0, "retail": 0}
    n = len(gt_rows)

    for gt_row in gt_rows:
        mpn = gt_row["Mfg_Part_Num"]
        bucket = by_mpn.get(mpn) or []
        product = bucket.pop(0) if bucket else None
        if product is None:
            row_errors += 1
            continue
        if product.row_error:
            row_errors += 1

        classpath_correct += product.classpath == gt_row.get("Classpath", "").strip()
        manufacturer_correct += product.manufacturer_name == gt_row.get("MANUFACTURER_NAME", "").strip()

        gt_attrs = gt_attribute_values(gt_row)
        produced_attrs = {a.label: a.value for a in product.attributes if a.value}
        row_correct = sum(1 for label, val in gt_attrs.items() if produced_attrs.get(label) == val)
        field_correct += row_correct
        field_total += len(gt_attrs)

        d = product.descriptions
        if d.invoice_desc == gt_row.get("INVOICE_DESC"):
            desc_scores["invoice"] += 1
        if d.mobile_desc == gt_row.get("MOBILE_DESC"):
            desc_scores["mobile"] += 1
        if d.short_desc == gt_row.get("SHORT_DESC"):
            desc_scores["short"] += 1
        if d.retail_desc == gt_row.get("RETAIL_DESC"):
            desc_scores["retail"] += 1

    print(f"Rows evaluated: {n}")
    print(f"Row errors (exceptions / missing from output): {row_errors}")
    print(f"Classpath accuracy: {classpath_correct}/{n} ({100 * classpath_correct / n:.1f}%)")
    print(f"Manufacturer accuracy: {manufacturer_correct}/{n} ({100 * manufacturer_correct / n:.1f}%)")
    if field_total:
        print(f"Attribute field accuracy (non-empty GT slots only): {field_correct}/{field_total} "
              f"({100 * field_correct / field_total:.1f}%)")
    else:
        print("Attribute field accuracy: no non-empty GT slots to compare")
    for key, label in [("invoice", "INVOICE_DESC"), ("mobile", "MOBILE_DESC"),
                        ("short", "SHORT_DESC"), ("retail", "RETAIL_DESC")]:
        print(f"{label} exact match: {desc_scores[key]}/{n} ({100 * desc_scores[key] / n:.1f}%)")

    hm = hit_miss_summary()
    hits, misses = hm.get("hits", 0), hm.get("misses", 0)
    total_calls = hits + misses
    ratio = (100 * hits / total_calls) if total_calls else 0.0
    print()
    print(f"LLM cache hits/misses: {hits}/{misses} "
          f"(hit ratio: {ratio:.1f}% of {total_calls} calls)")
    print(f"LLM cache table (per namespace): {stats()}")


if __name__ == "__main__":
    main()
