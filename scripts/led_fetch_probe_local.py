"""LED 22-row fetch + extraction probe, designed to be run LOCALLY
(on the user's own machine, NOT from the blocked dev sandbox).

WHY: the dev sandbox hits a persistent Vercel edge 429 on satco.com
because Vercel bot-detection fingers the sandbox's source IP. The user's
local machine got through (Satco WEB EVIDENCE attributes shown for
62-1850 / 64-110 in the running app), supporting the IP-reputation
diagnosis in commit 1a31db4. This script gets the real fetch-success
number + the attribute-extraction accuracy delta from the unblocked
environment -- the missing data the submission actually ships with.

USAGE (from uniintel/):
    python scripts/led_fetch_probe_local.py

Run via process_job() -- the orchestrator's real code path that uses the
shared httpx.AsyncClient built with BROWSER_HEADERS (after orchestrator.py
follow-up fix to use full headers, not UA-only). Same path the running app
uses, so the numbers reflect what the running app is producing.

OUTPUT (printed to stdout, also saved to data/output/led_probe_local.txt):
  * per-row table: MPN | GT mfr | GT mfr-domain | enrich status | extracted attr count
  * totals: how many FETCHED out of 22, by manufacturer
  * attribute accuracy: extracted values vs GT ATTRIBUTE_VALUE 1..27
  * description accuracy: invoice/mobile/short/retail/long/marketing
  * before/after vs the dev-sandbox run (0/22 fetched, 0% descriptions)

If this script reports >0/22 FETCHED, the IP-reputation diagnosis is
confirmed and the deployed Railway host (fresh IP) should also work.
"""
import asyncio
import csv
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from persistence.db import init_db  # noqa: E402
from persistence.llm_cache import reset_hit_miss, hit_miss_summary  # noqa: E402
from pipeline.orchestrator import process_job  # noqa: E402

GT = ROOT / "data" / "ground_truth" / "gt_delivery_200.csv"
LED = "Electrical>Lamps & Lightings>Light Bulbs>LED Light Bulbs"
OUT_PATH = ROOT / "data" / "output" / "led_probe_local.txt"


def gt_attrs(row):
    out = {}
    for i in range(1, 51):
        lab = (row.get(f"ATTRIBUTE_LABEL {i}", "") or "").strip()
        val = (row.get(f"ATTRIBUTE_VALUE {i}", "") or "").strip()
        if lab and val:
            out[lab] = val
    return out


async def main():
    init_db()
    reset_hit_miss()

    gt_rows = [r for r in csv.DictReader(open(GT, encoding="utf-8-sig"))
               if (r.get("Classpath", "") or "").strip() == LED]
    print(f"LED GT rows: {len(gt_rows)}")

    input_cols = ["Mfg_Part_Num", "Part_Desc", "E1_Brand", "Unilog_Brand",
                  "DIB_Brand", "Part_Manuf"]
    input_rows = [{k: r.get(k, "") for k in input_cols} for r in gt_rows]

    products = await process_job(input_rows, job_id="led-probe-local")
    by_mpn = {p.mfg_part_num: p for p in products}

    lines = []
    def out(s=""):
        print(s)
        lines.append(s)

    out("| MPN       | GT mfg (resolved)         | GT mfr-domain                  | enrich status    | HTTP | ev chars | extracted attrs |")
    out("|-----------|---------------------------|--------------------------------|------------------|------|----------|-----------------|")

    fetched = 0
    failed = 0
    no_url = 0
    blocked = 0
    by_manufacturer = {}
    by_mfr_domain = {}

    # run enrichment phase already done by process_job; we read the product
    # attributes + descriptions to score accuracy. We need the enrichment
    # status -- which EnrichedProduct doesn't carry directly (it has mfr_url
    # and ref_urls, but not the EnrichmentResult.status string). Derive:
    #   mfr_url present => at least attempted + recorded a url (FETCHED or
    #     FETCH_FAILED-with-source-recorded)
    #   mfr_url absent => NO_URL (unknown domain) or FETCH_BLOCKED (pre-request skip)
    # We re-run enrich() on each row to get the explicit status for the
    # report -- NOT for the pipeline output (process_job has already run it).
    # This is one extra network call per row but keeps the report truthful.
    from pipeline.enricher import enrich  # noqa: E402
    from pipeline.entity_resolver import resolve_manufacturer  # noqa: E402

    for gt_row in gt_rows:
        mpn = gt_row["Mfg_Part_Num"]
        gt_mfr = (gt_row.get("MANUFACTURER_NAME", "") or "").strip()
        gt_mfr_url = (gt_row.get("MFR URL", "") or "").strip()
        gt_domain = gt_mfr_url.split("/")[2] if gt_mfr_url.startswith("http") else "(none)"
        resolved = resolve_manufacturer(gt_row.get("Part_Manuf", ""))
        resolved_name = resolved.manufacturer_name or "(unresolved)"
        # explicit re-probe for status (truthful reporting)
        status_obj = await enrich(resolved_name or gt_mfr, mpn)
        status = status_obj.status
        http_st = status_obj.http_status if status_obj.http_status is not None else ""
        ev_chars = len(status_obj.evidence_text or "")
        product = by_mpn.get(mpn)
        extracted_attr_count = 0
        if product:
            extracted_attr_count = sum(1 for a in product.attributes if a.value)
        out(f"| {mpn:<10}| {resolved_name[:25]:<25}| {gt_domain[:30]:<30}| {status:<16}| {str(http_st):<4}| {ev_chars:<8}| {extracted_attr_count:<15}|")
        # tabulators
        by_manufacturer.setdefault(resolved_name, {"FETCHED":0,"FETCH_FAILED":0,"NO_URL":0,"BLOCKED_SOURCE":0,"FETCH_BLOCKED":0})
        by_manufacturer[resolved_name][status] = by_manufacturer[resolved_name].get(status, 0) + 1
        by_mfr_domain.setdefault(gt_domain, {"FETCHED":0,"FETCH_FAILED":0,"NO_URL":0,"BLOCKED_SOURCE":0,"FETCH_BLOCKED":0})
        by_mfr_domain[gt_domain][status] = by_mfr_domain[gt_domain].get(status, 0) + 1
        if status == "FETCHED": fetched += 1
        elif status == "FETCH_FAILED": failed += 1
        elif status == "NO_URL": no_url += 1
        elif status == "BLOCKED_SOURCE": blocked += 1

    out()
    out(f"=== Totals (n={len(gt_rows)}) ===")
    out(f"  FETCHED       : {fetched} ({100*fetched/len(gt_rows):.1f}%)")
    out(f"  FETCH_FAILED  : {failed}")
    out(f"  NO_URL        : {no_url}")
    out(f"  BLOCKED_SOURCE: {blocked}")
    out()
    out("=== by resolved manufacturer ===")
    for mfr, s in by_manufacturer.items():
        out(f"  {mfr:<30} {s}")
    out()
    out("=== by GT URL domain ===")
    for d, s in by_mfr_domain.items():
        out(f"  {d:<30} {s}")

    # Accuracy: attribute exact-match vs GT across the 22 LED rows.
    attr_correct = 0
    attr_total = 0
    per_row_attr = []
    for gt_row in gt_rows:
        mpn = gt_row["Mfg_Part_Num"]
        product = by_mpn.get(mpn)
        if product is None:
            continue
        ga = gt_attrs(gt_row)
        sa = {a.label: a.value for a in product.attributes if a.value}
        row_correct = sum(1 for k, v in ga.items() if sa.get(k) == v)
        attr_correct += row_correct
        attr_total += len(ga)
        per_row_attr.append((mpn, status_obj.status if mpn == gt_rows[-1]["Mfg_Part_Num"] else None,
                              row_correct, len(ga)))
    out()
    out(f"=== attribute accuracy (22 LED rows, vs GT non-empty slots) ===")
    out(f"  {attr_correct}/{attr_total} ({100*attr_correct/attr_total:.2f}%)")

    # Description accuracy
    out()
    out("=== description exact-match (22 LED rows vs GT) ===")
    for col, field in [("INVOICE_DESC", "invoice_desc"),
                        ("MOBILE_DESC", "mobile_desc"),
                        ("SHORT_DESC", "short_desc"),
                        ("LONG_DESC1", "long_desc1"),
                        ("RETAIL_DESC", "retail_desc"),
                        ("MARKETING_DESCRIPTION", "marketing_description")]:
        ok = sum(1 for gt_row in gt_rows
                 if by_mpn.get(gt_row["Mfg_Part_Num"]) and
                 (getattr(by_mpn[gt_row["Mfg_Part_Num"]].descriptions, field) or "").strip()
                 == (gt_row.get(col, "") or "").strip())
        out(f"  {col:<22} {ok}/22 ({100*ok/22:.1f}%)")

    hm = hit_miss_summary()
    out()
    out(f"=== LLM cache (this run) ===")
    out(f"  hits={hm.get('hits',0)}  misses={hm.get('misses',0)}")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text("\n".join(lines), encoding="utf-8")
    print()
    print(f"Report saved to: {OUT_PATH}")


if __name__ == "__main__":
    # default logging level = WARNING so individual row stats don't drown out the report
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(main())
