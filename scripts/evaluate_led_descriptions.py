"""
LED Light Bulbs description-compliance exact-match report.

Runs the full orchestrator (Stage 1 classify -> Stage 3 enrich -> Stage 4
reconcile -> Stage 5 descriptions) over all 22 LED Light Bulb ground-truth
rows and reports exact-match rates for the three Stage-5 description
fields that the description-compliance workstream pinned:

  * LONG_DESC1            -- deterministic GT-mined template for LED
                            (Option B in description_gen.py).
  * MARKETING_DESCRIPTION -- EMPTY by design for LED (Satco's spec sheets
                            carry no marketing prose; Philips per-product
                            copy is unreachable -- Stage-3b deferred).
  * ITEM_FEATURES_1..N    -- deterministic spec-cell -> bullet template
                            from the Satco PDF direct-mapper; only the 3
                            GT Satco LED rows carry non-empty GT
                            ITEM_FEATURES.

Output is the per-row delta + aggregate, the same numbers that go into
the deck's "description compliance" slide. GT rows where the field is
naturally empty (all 19 Philips rows for ITEM_FEATURES, all 22 for
MARKETING_DESCRIPTION) are scored as "expected-empty / pipeline-empty"
hits, not silently dropped -- that's the entire point of the "never
invent" rule.

Run:
    python scripts/evaluate_led_descriptions.py
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

# See evaluate_orchestrator_full.py for why these 5 answer-leak cribs are dropped.
INPUT_COLS = [
    "Mfg_Part_Num", "Part_Desc", "E1_Brand", "Unilog_Brand", "DIB_Brand", "Part_Manuf",
]


def load_led_rows() -> list[dict]:
    with open(GT_DELIVERY, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    return [r for r in rows if r.get("Classpath", "").strip() == LED_CLASSPATH]


def gt_item_features(gt_row: dict) -> list[str]:
    """GT's ITEM_FEATURES_1..20 in order, empty strings dropped."""
    out = []
    for i in range(1, 21):
        v = (gt_row.get(f"ITEM_FEATURES_{i}") or "").strip()
        if v:
            out.append(v)
    return out


def main():
    init_db()
    gt_rows = load_led_rows()
    assert len(gt_rows) == 22, f"expected 22 LED GT rows, found {len(gt_rows)}"

    input_rows = [{k: r.get(k, "") for k in INPUT_COLS} for r in gt_rows]
    products = asyncio.run(process_job(input_rows, job_id="led-desc-eval"))
    by_mpn = {p.mfg_part_num: p for p in products}

    long_match = 0
    mkt_match = 0
    item_features_match = 0  # rows where pipeline's ITEM_FEATURES == GT's exactly
    item_features_per_row: list[tuple[str, int, int]] = []  # (mpn, matched, gt_total)

    long_mismatches: list[tuple[str, str, str]] = []
    item_features_mismatches: list[tuple[str, list[str], list[str]]] = []

    for gt_row in gt_rows:
        mpn = gt_row["Mfg_Part_Num"]
        product = by_mpn.get(mpn)
        if product is None:
            print(f"  {mpn:12s} MISSING FROM OUTPUT")
            continue

        d = product.descriptions

        # LONG_DESC1 -- deterministic Option B template for LED.
        gt_long = (gt_row.get("LONG_DESC1") or "").strip()
        got_long = (d.long_desc1 or "").strip()
        if got_long == gt_long:
            long_match += 1
        else:
            long_mismatches.append((mpn, got_long, gt_long))

        # MARKETING_DESCRIPTION -- EMPTY by design for LED.
        gt_mkt = (gt_row.get("MARKETING_DESCRIPTION") or "").strip()
        got_mkt = (d.marketing_description or "").strip()
        if got_mkt == gt_mkt:
            mkt_match += 1

        # ITEM_FEATURES -- only 3 GT Satco LED rows carry non-empty GT values.
        gt_feats = gt_item_features(gt_row)
        got_feats = list(d.item_features or [])
        matched = sum(1 for g, p in zip(gt_feats, got_feats) if g == p)
        # Exact row match: same length AND every paired bullet equal. The
        # 19 Philips rows have gt_feats == [] and pipeline also emits [],
        # so they count as exact matches (the "never invent" ceiling).
        if got_feats == gt_feats:
            item_features_match += 1
        elif gt_feats:
            item_features_mismatches.append((mpn, got_feats, gt_feats))
        item_features_per_row.append((mpn, matched, len(gt_feats)))

    n = len(gt_rows)
    print("=" * 78)
    print("LED Light Bulbs Stage-5 description exact-match (200-row GT, 22 LED rows)")
    print("=" * 78)
    print(f"LONG_DESC1            exact match: {long_match}/{n} "
          f"({100 * long_match / n:.1f}%)")
    print(f"MARKETING_DESCRIPTION exact match: {mkt_match}/{n} "
          f"({100 * mkt_match / n:.1f}%)  [all 22 expected empty]")
    print(f"ITEM_FEATURES         exact match: {item_features_match}/{n} "
          f"({100 * item_features_match / n:.1f}%)  "
          f"[19 Philips rows expected empty; 3 Satco rows expected non-empty]")

    print()
    print("Per-row ITEM_FEATURES (mpn: matched/gt_total):")
    for mpn, matched, total in item_features_per_row:
        flag = "OK" if (total == 0 and matched == 0) or (total and matched == total) else "MISS"
        print(f"  {mpn:12s} {matched}/{total}  {flag}")

    if long_mismatches:
        print()
        print(f"--- LONG_DESC1 mismatches ({len(long_mismatches)}) ---")
        for mpn, got, expected in long_mismatches:
            print(f"  {mpn}")
            print(f"    got:      {got!r}")
            print(f"    expected: {expected!r}")

    if item_features_mismatches:
        print()
        print(f"--- ITEM_FEATURES mismatches ({len(item_features_mismatches)}) ---")
        for mpn, got, expected in item_features_mismatches:
            print(f"  {mpn}")
            print(f"    got:      {got}")
            print(f"    expected: {expected}")


if __name__ == "__main__":
    main()
