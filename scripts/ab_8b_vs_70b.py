"""
Phase F: 8B vs 70B side-by-side A/B measurement for the LOV-constrained
fallback extraction path.

Strategy (per the audit's Q5 verdict): the extraction task is constrained-
selection (pick from allowed values or omit), structurally similar to
classify which already runs at 92.5% on 8B. So 8B could be a viable swap
that escapes the shared 100K TPD org-level quota wall the 70B model keeps
hitting -- but 8B's reliability with `response_format={"type":"json_object"}`
is UNVALIDATED in this codebase (only 70B uses it; classify uses plain text).
This script gives a real before/after number instead of guessing.

To keep the comparison fair and quota-cheap:
  - Only the extraction path is compared (descriptions/prose skipped).
    Both the model swap (GROQ_EXTRACT_MODEL) and prose-generation
    (GROQ_DESC_MODEL) are separable concerns; they will be tested in
    isolation, never confounded.
  - Only the fallback path is exercised: web fetch is stubbed so every
    row routes to fallback_extract_attributes, isolating the LLM variable.
  - rule_priors are NOT passed to reconcile() -- this is purely an
    extractor-side measurement, not a pipeline-level one.
  - Per-row provenance (source_url None + evidence_text from Part_Desc)
    is stamped so validators and the confidence formula behave like live
    production rows without any web evidence.

Cost: ~20-30 rows x 2 models x 1 fallback call = 40-60 LLM calls -- well
under any reasonable daily-quota reserve, trivial vs 100K TPD. Run twice
if you want variance bars.

Usage:
    python scripts/ab_8b_vs_70b.py                # default 20 rows
    python scripts/ab_8b_vs_70b.py --limit 30
    python scripts/ab_8b_vs_70b.py --model 8b     # only one arm (single model)

Quota must be available for the chosen model arms -- check the live eval
dashboard / `python -c "import os; print(os.getenv('GROQ_API_KEY'))"` first.
"""
import argparse
import asyncio
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from evaluation.metrics import evaluate_product, summarize   # noqa: E402
from leaf_templates.registry import get_template               # noqa: E402
from pipeline.entity_resolver import resolve_manufacturer     # noqa: E402
from pipeline.extractor import fallback_extract_attributes, reconcile  # noqa: E402
from schemas.product import AttributeValue                    # noqa: E402

GT_DELIVERY = ROOT / "data" / "ground_truth" / "gt_delivery_200.csv"
RULE_PRIORS: list = []  # empty by design -- isolate the LLM variable

MODELS_FULL = ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"]
MODEL_ALIASES = {"70b": "llama-3.3-70b-versatile", "8b": "llama-3.1-8b-instant"}


def load_gt(limit: int) -> list[dict]:
    with open(GT_DELIVERY, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    return rows[:limit] if limit else rows


async def run_arm(rows: list[dict], model: str) -> list[dict]:
    results = []
    for row in rows:
        mpn = (row.get("Mfg_Part_Num") or "").strip()
        part_desc = (row.get("Part_Desc") or "").strip()
        part_manuf = row.get("Part_Manuf", "")
        classpath = (row.get("Classpath") or "").strip()
        if not classpath or not part_desc:
            # No template to fill / nothing to read; skip but keep output countable.
            results.append({
                "mfg_part_num": mpn, "classpath": classpath,
                "manufacturer_name": None, "attributes": [],
                "extraction_call": "skipped",
            })
            continue
        mfr = resolve_manufacturer(part_manuf).manufacturer_name
        try:
            extracted = await fallback_extract_attributes(
                part_desc, classpath, mfr, model=model,
            )
        except Exception as exc:
            results.append({
                "mfg_part_num": mpn, "classpath": classpath,
                "manufacturer_name": mfr, "attributes": [],
                "extraction_call": f"error: {type(exc).__name__}: {exc}",
            })
            continue
        reconciled = reconcile(classpath, extracted, RULE_PRIORS)
        attrs = [
            AttributeValue(
                slot=s["slot"], label=s["label"], value=s.get("value") or None,
                uom=s.get("uom") or None, origin=s.get("origin"),
                # Stamp provenance so downstream validators / confidence behave
                # identically to live desc_fallback rows.
                source_url=None,
                evidence_text=f'LOV-constrained extraction from: "{part_desc}"',
            )
            for s in reconciled
        ]
        results.append({
            "mfg_part_num": mpn, "classpath": classpath,
            "manufacturer_name": mfr,
            "attributes": [a.model_dump() for a in attrs],
            "extraction_call": "ok",
        })
    return results


def score_against_gt(results: list[dict], gt_rows: list[dict]) -> tuple[dict, list[dict]]:
    gt_by_mpn = {r["Mfg_Part_Num"]: r for r in gt_rows}
    scored = []
    for r in results:
        gt_row = gt_by_mpn.get(r["mfg_part_num"], {})
        if not gt_row:
            continue
        scored.append(evaluate_product(r, gt_row))
    summary = summarize(scored) if scored else {}
    return summary, scored


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--model", choices=["70b", "8b", "both"], default="both",
                    help="which A/B arm(s) to run")
    ap.add_argument("--out", type=str, default=None,
                    help="optional path to dump a JSON of per-row scores per arm")
    args = ap.parse_args()

    gt_rows = load_gt(args.limit)
    print(f"Loaded {len(gt_rows)} GT rows; running arms: {args.model}\n")

    arms = MODELS_FULL if args.model == "both" else [MODEL_ALIASES[args.model]]
    out_dump: dict[str, list] = {}

    for model in arms:
        print("=" * 70)
        print(f"Arm: {model}")
        results = await run_arm(gt_rows, model)
        summary, scored = score_against_gt(results, gt_rows)

        calls_ok = sum(1 for r in results if r.get("extraction_call") == "ok")
        calls_err = sum(1 for r in results if r.get("extraction_call", "").startswith("error"))
        calls_skip = sum(1 for r in results if r.get("extraction_call") == "skipped")
        print(f"  extraction calls: ok={calls_ok}  errors={calls_err}  skipped={calls_skip}")

        if summary:
            print(f"  rows_evaluated: {summary['rows_evaluated']}")
            ca = summary["classpath_accuracy"]; ms = summary["manufacturer_accuracy"]
            aa = summary["attribute_accuracy"]
            print(f"  classpath accuracy:    {ca['correct']}/{ca['total']} ({ca['pct']}%)")
            print(f"  manufacturer accuracy: {ms['correct']}/{ms['total']} ({ms['pct']}%)")
            print(f"  attribute accuracy:    {aa['correct']}/{aa['total']} ({aa['pct']}%)")
            print(f"  description_match_rates (skipped in this A/B -- prose is out of scope)")
        else:
            print("  (no rows scored against GT)")

        if args.out:
            out_dump[model] = scored

    if args.out:
        Path(args.out).write_text(json.dumps(out_dump, indent=2), encoding="utf-8")
        print(f"\nPer-row scores written to {args.out}")

    print("\nDecision rule: swap config.py:37 GROQ_EXTRACT_MODEL to 8B IF")
    print("  - attribute accuracy delta < ~5 percentage points vs 70B, AND")
    print("  - zero JSON-mode breakage observed (no 'error: 'extraction_call' from JSON parsing).")
    print("If 8B accuracy drops hard or breaks JSON object mode, keep 70B and build")
    print("an LLM-response cache keyed by (mpn, classpath, prompt-hash) to escape via re-run-freeness instead.")


if __name__ == "__main__":
    asyncio.run(main())
