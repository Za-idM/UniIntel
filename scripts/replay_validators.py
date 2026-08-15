"""
Replay harness: re-runs V1-V6 validators + the calibrated confidence formula
over already-persisted EnrichedProduct rows in data/uniintel.db, WITHOUT making
any new LLM calls. Used to:

  - sanity-check Phase C wiring (validators + confidence) before/after edits
    to validators.py / confidence.py, for free (no Groq quota burned)
  - confirm the V6 origin-gate actually stops the 262/262 false-fail storm
    desc_fallback rows were producing under the old "value AND no source_url"
    strict rule (audit finding: 262/262 llm_extract desc_fallback attrs had
    source_url=None because orchestrator.py:229-232 attaches no source_url
    to that path by design)
  - confirm V2 LOV auto-repair is zero on the current persisted snapshot
    (expected: CLOSE=0.0% across 3088 evaluated GT slots, per the audit -- if
    non-zero, the LOV widening (Phase D) actually opened up repairable
    near-misses and the formula is now exercising)
  - verify confidence bands cluster as expected: GT-correct rows should
    land VERIFIED/REVIEW, GT-wrong rows near LOW. If they don't, the
    thresholds VERIFIED_THRESHOLD=90 / REVIEW_THRESHOLD=70 in
    confidence.py need tuning.

Usage:
    python scripts/replay_validators.py                       # default job=cc9d36ab
    python scripts/replay_validators.py --job_id JOB --limit 50
Read-only against the DB -- writes nothing.
"""
import argparse
import csv
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from schemas.product import AttributeValue, EnrichedProduct  # noqa: E402
from validation.validators import run_validators, v6_source_url  # noqa: E402
from validation.confidence import confidence_for_product  # noqa: E402

DEFAULT_JOB = "cc9d36ab-dbc1-4551-ab42-5d7ffbc26558"  # newest full 200-row eval
DB_PATH = ROOT / "data" / "uniintel.db"
GT_DELIVERY = ROOT / "data" / "ground_truth" / "gt_delivery_200.csv"


def load_gt_by_mpn() -> dict[str, dict]:
    with open(GT_DELIVERY, encoding="utf-8-sig") as f:
        return {r["Mfg_Part_Num"]: r for r in csv.DictReader(f) if r.get("Mfg_Part_Num")}


def load_products(job_id: str, limit: int | None = None) -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    if limit:
        cur.execute(
            "SELECT data_json FROM products WHERE job_id=? ORDER BY ROWID LIMIT ?",
            (job_id, limit),
        )
    else:
        cur.execute("SELECT data_json FROM products WHERE job_id=? ORDER BY ROWID", (job_id,))
    rows = [json.loads(r[0]) for r in cur.fetchall()]
    conn.close()
    return rows


def gt_attribute_values(gt_row: dict) -> dict[str, str]:
    out = {}
    for i in range(1, 51):
        label = gt_row.get(f"ATTRIBUTE_LABEL {i}", "").strip()
        value = gt_row.get(f"ATTRIBUTE_VALUE {i}", "").strip()
        if label and value:
            out[label] = value
    return out


# -------- OLD (pre-gate) V6, kept here only for before/after reporting --------

def _v6_strict(attributes: list[AttributeValue]) -> tuple[bool, list[str]]:
    """The original strict V6: any value with no source_url fails. Kept here
    to demonstrate WHY we gated -- the post-gate v6_source_url in
    validators.py is the live one."""
    warnings = [
        f"V6: '{attr.label}' has value '{attr.value}' with no source_url"
        for attr in attributes
        if attr.value and not attr.source_url
    ]
    return (len(warnings) == 0, warnings)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--job_id", default=DEFAULT_JOB)
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    gt_by_mpn = load_gt_by_mpn()
    products = load_products(args.job_id, args.limit)
    print(f"Loaded {len(products)} persisted products from job {args.job_id[:8]}")
    print(f"GT MPN coverage: {sum(1 for p in products if p.get('mfg_part_num') in gt_by_mpn)}/{len(products)}")
    print()

    # Aggregate counters
    v_dist = Counter()
    v2_total_repairs = 0
    v6_strict_pass = 0
    v6_gated_pass = 0
    v6_strict_attr_fails = 0
    v6_gated_attr_hard_fails = 0
    v6_gated_attr_soft_warnings = 0  # text-only provenance (rule_prior / desc_fallback)
    band_dist = Counter()
    confidence_inputs_avg = {"source_strength": 0.0, "extraction_conf": 0.0,
                              "lov_match": 0.0, "rule_validation": 0.0}

    # GT-correct pairings (to sanity-check band clustering)
    band_by_gt_correctness = {"correct_attribute": Counter(), "wrong_attribute": Counter()}

    for pdata in products:
        # Materialize EnrichedProduct for confidence assembly, but feed
        # run_validators the AttributeValue list directly (mutated in-place
        # by V2 repairs -- on a copy so we don't bias downstream rows).
        product = EnrichedProduct.model_validate(pdata)
        attrs = [
            AttributeValue(
                slot=a["slot"], label=a["label"], value=a.get("value"),
                uom=a.get("uom"), source_url=a.get("source_url"),
                evidence_text=a.get("evidence_text"), origin=a.get("origin"),
            )
            for a in pdata.get("attributes", [])
        ]

        desc_fields = pdata.get("descriptions") or {}

        # Pre-gate V6 strict count (report only)
        v6s_pass, v6s_warn = _v6_strict(attrs)
        if v6s_pass:
            v6_strict_pass += 1
        v6_strict_attr_fails += len(v6s_warn)

        # Live V1-V6 + V2 in-place repair on `attrs`
        validation = run_validators(
            product.mfg_part_num, product.part_desc, product.classpath,
            product.brand_name, product.manufacturer_name, attrs, desc_fields,
        )
        v_dist["v1_pass" if validation.v1_required else "v1_fail"] += 1
        v_dist["v2_pass" if validation.v2_lov else "v2_fail"] += 1
        v_dist["v3_pass" if validation.v3_uom_inline else "v3_fail"] += 1
        v_dist["v4_pass" if validation.v4_casing_inline else "v4_fail"] += 1
        v_dist["v5_pass" if validation.v5_brand_mfr else "v5_fail"] += 1
        v_dist["v6_pass" if validation.v6_source_url else "v6_fail"] += 1
        v_dist["needs_review" if validation.needs_human_review else "ok"] += 1

        # V2 repairs
        v2_repairs = [w for w in validation.warnings if w.startswith("V2: repaired")]
        v2_total_repairs += len(v2_repairs)

        # V6 gated post-repair (recompute since attrs may have changed)
        v6g_pass, v6g_warn = v6_source_url(attrs)
        if v6g_pass:
            v6_gated_pass += 1
        v6_gated_attr_hard_fails += sum(1 for w in v6g_warn if "no source_url AND no evidence_text" in w)
        v6_gated_attr_soft_warnings += sum(1 for w in v6g_warn if "text-only" in w)

        # Rebuild product with repaired attrs for confidence assembly
        product.attributes = attrs

        score, band, inputs = confidence_for_product(product, validation)
        band_dist[band] += 1
        confidence_inputs_avg["source_strength"] += inputs.source_strength
        confidence_inputs_avg["extraction_conf"] += inputs.extraction_conf
        confidence_inputs_avg["lov_match"] += inputs.lov_match
        confidence_inputs_avg["rule_validation"] += inputs.rule_validation

        # GT correctness per attribute slot
        gt = gt_by_mpn.get(product.mfg_part_num)
        if gt and product.classpath:
            gt_attrs = gt_attribute_values(gt)
            produced = {a.label: a.value for a in attrs if a.value}
            for label, gt_val in gt_attrs.items():
                if produced.get(label) == gt_val:
                    band_by_gt_correctness["correct_attribute"][band] += 1
                else:
                    band_by_gt_correctness["wrong_attribute"][band] += 1

    n = len(products)
    print("=" * 70)
    print("V1-V6 distribution (post-gate, live validators):")
    for k in sorted(v_dist):
        print(f"  {k:14s} {v_dist[k]:>5d}  ({100*v_dist[k]/n:.1f}%)")
    print()
    print(f"V2 LOV auto-repairs applied: {v2_total_repairs}")
    print(f"  (expected 0 on current pre-LOV-widening snapshot; non-zero after Phase D)")
    print()
    print("V6 strict (OLD, pre-gate) -- the reason the gate exists:")
    print(f"  rows passing strict V6: {v6_strict_pass}/{n} ({100*v6_strict_pass/n:.1f}%)")
    print(f"  attribute-level strict-V6 warnings: {v6_strict_attr_fails}")
    print("V6 gated (NEW, post-origin-gate):")
    print(f"  rows passing gated V6: {v6_gated_pass}/{n} ({100*v6_gated_pass/n:.1f}%)")
    print(f"  attribute-level HARD fails (no source_url AND no evidence_text): {v6_gated_attr_hard_fails}")
    print(f"  attribute-level SOFT warnings (text-only provenance, not a fail): {v6_gated_attr_soft_warnings}")
    print()
    print("Confidence band distribution:")
    for b in ["VERIFIED", "REVIEW", "LOW"]:
        print(f"  {b:8s} {band_dist[b]:>5d}  ({100*band_dist[b]/n:.1f}%)")
    print()
    print("Avg confidence inputs (0-1 each):")
    for k, v in confidence_inputs_avg.items():
        print(f"  {k:20s} {v/n:.3f}")
    print()
    print("Band vs GT correctness (sanity: correct attrs should cluster VERIFIED/REVIEW):")
    for label in ["correct_attribute", "wrong_attribute"]:
        c = band_by_gt_correctness[label]
        total = sum(c.values()) or 1
        parts = " ".join(f"{b}={c[b]} ({100*c[b]/total:.0f}%)" for b in ["VERIFIED", "REVIEW", "LOW"] if c.get(b))
        print(f"  {label:18s} n={sum(c.values()):>4d}  {parts}")


if __name__ == "__main__":
    main()
