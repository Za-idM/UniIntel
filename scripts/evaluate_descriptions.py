"""
Evaluate Stage 5 description generation (pipeline/description_gen.py)
against all 22 LED Light Bulb GT rows.

Deterministic fields (INVOICE_DESC, MOBILE_DESC, SHORT_DESC, RETAIL_DESC)
are scored by exact string match against GT. LONG_DESC1 and
MARKETING_DESCRIPTION are LLM-generated prose -- not expected to
exact-match GT's own human-written text, so they're spot-printed for
manual sanity-checking rather than scored.
"""
import asyncio
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from config import llm_configured  # noqa: E402
from pipeline.description_gen import (  # noqa: E402
    invoice_desc, mobile_desc, short_desc, retail_desc, generate_prose_descriptions,
)

GT_DELIVERY = ROOT / "data" / "ground_truth" / "gt_delivery_200.csv"
LED_CLASSPATH = "Electrical>Lamps & Lightings>Light Bulbs>LED Light Bulbs"


def load_led_rows() -> list[dict]:
    with open(GT_DELIVERY, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    return [r for r in rows if r.get("Classpath", "").strip() == LED_CLASSPATH]


def gt_attrs(row: dict) -> dict[str, str]:
    out = {}
    for i in range(1, 51):
        label = row.get(f"ATTRIBUTE_LABEL {i}", "").strip()
        value = row.get(f"ATTRIBUTE_VALUE {i}", "").strip()
        if label:
            out[label] = value
    return out


def main():
    rows = load_led_rows()
    assert len(rows) == 22, f"expected 22 LED rows, found {len(rows)}"

    scores = {"invoice": 0, "mobile": 0, "short": 0, "retail": 0}
    mismatches = {"invoice": [], "mobile": [], "short": [], "retail": []}
    invoice_over_40 = 0

    for row in rows:
        mpn = row["Mfg_Part_Num"]
        mfr = row.get("MANUFACTURER_NAME", "").strip()
        attrs = gt_attrs(row)

        got_invoice = invoice_desc(mpn, attrs, LED_CLASSPATH)
        got_mobile = mobile_desc(mpn, mfr, attrs, LED_CLASSPATH)
        got_short = short_desc(mpn, mfr, attrs, LED_CLASSPATH)
        got_retail = retail_desc(attrs, LED_CLASSPATH)

        if len(got_invoice) > 40:
            invoice_over_40 += 1

        checks = [
            ("invoice", got_invoice, row["INVOICE_DESC"]),
            ("mobile", got_mobile, row["MOBILE_DESC"]),
            ("short", got_short, row["SHORT_DESC"]),
            ("retail", got_retail, row["RETAIL_DESC"]),
        ]
        for field, got, expected in checks:
            if got == expected:
                scores[field] += 1
            else:
                mismatches[field].append((mpn, got, expected))

    n = len(rows)
    print(f"Rows evaluated: {n}")
    print(f"INVOICE_DESC exact match: {scores['invoice']}/{n} ({100*scores['invoice']/n:.1f}%)  [over 40 chars: {invoice_over_40}]")
    print(f"MOBILE_DESC  exact match: {scores['mobile']}/{n} ({100*scores['mobile']/n:.1f}%)")
    print(f"SHORT_DESC   exact match: {scores['short']}/{n} ({100*scores['short']/n:.1f}%)")
    print(f"RETAIL_DESC  exact match: {scores['retail']}/{n} ({100*scores['retail']/n:.1f}%)")

    for field in ("invoice", "mobile", "short", "retail"):
        if mismatches[field]:
            print(f"\n--- {field.upper()} mismatches ({len(mismatches[field])}) ---")
            for mpn, got, expected in mismatches[field]:
                print(f"  {mpn}")
                print(f"    got:      {got!r}")
                print(f"    expected: {expected!r}")

    if llm_configured():
        print("\n=== LLM prose spot-check (first 3 LED rows) ===")
        for row in rows[:3]:
            mpn = row["Mfg_Part_Num"]
            mfr = row.get("MANUFACTURER_NAME", "").strip()
            attrs = gt_attrs(row)
            prose = asyncio.run(generate_prose_descriptions(mpn, mfr, LED_CLASSPATH, attrs))
            print(f"\n{mpn}:")
            print(f"  long_desc1 (generated): {prose.get('long_desc1', '<empty>')}")
            print(f"  long_desc1 (GT):        {row['LONG_DESC1']}")
            print(f"  marketing (generated):  {prose.get('marketing_description', '<empty>')}")
            print(f"  marketing (GT):         {row['MARKETING_DESCRIPTION']}")
    else:
        print("\n(GROQ_API_KEY not configured -- skipping LLM prose spot-check)")


if __name__ == "__main__":
    main()
