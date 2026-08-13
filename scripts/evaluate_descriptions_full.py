"""
Evaluate Stage 5 deterministic description templates against all 200 GT
rows (not just the 22 LED Light Bulb rows evaluate_descriptions.py
covers). Only LED Light Bulbs has a GT-mined exact template; every other
classpath uses description_gen.py's generic fallback, so this is expected
to score much lower than the LED-only eval -- it exists to confirm the
noun-hardcoding fix (every row used to render "LED Bulb" regardless of
category) actually moved the needle, not to hit a new bar.
"""
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from pipeline.description_gen import invoice_desc, mobile_desc, short_desc, retail_desc  # noqa: E402

GT_DELIVERY = ROOT / "data" / "ground_truth" / "gt_delivery_200.csv"


def load_rows() -> list[dict]:
    with open(GT_DELIVERY, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def gt_attrs(row: dict) -> dict[str, str]:
    out = {}
    for i in range(1, 51):
        label = row.get(f"ATTRIBUTE_LABEL {i}", "").strip()
        value = row.get(f"ATTRIBUTE_VALUE {i}", "").strip()
        if label:
            out[label] = value
    return out


def main():
    rows = load_rows()
    scores = {"invoice": 0, "mobile": 0, "short": 0, "retail": 0}
    bulb_mentions_on_non_bulb = 0
    n = len(rows)

    for row in rows:
        mpn = row["Mfg_Part_Num"]
        mfr = row.get("MANUFACTURER_NAME", "").strip()
        classpath = row.get("Classpath", "").strip()
        attrs = gt_attrs(row)

        got_invoice = invoice_desc(mpn, attrs, classpath)
        got_mobile = mobile_desc(mpn, mfr, attrs, classpath)
        got_short = short_desc(mpn, mfr, attrs, classpath)
        got_retail = retail_desc(attrs, classpath)

        if got_invoice == row["INVOICE_DESC"]:
            scores["invoice"] += 1
        if got_mobile == row["MOBILE_DESC"]:
            scores["mobile"] += 1
        if got_short == row["SHORT_DESC"]:
            scores["short"] += 1
        if got_retail == row["RETAIL_DESC"]:
            scores["retail"] += 1

        is_led = "LED Light Bulbs" in classpath
        if not is_led:
            for got in (got_invoice, got_mobile, got_short, got_retail):
                if "bulb" in got.lower() or " led" in got.lower() or got.lower().startswith("led"):
                    bulb_mentions_on_non_bulb += 1
                    break

    print(f"Rows evaluated: {n}")
    print(f"INVOICE_DESC exact match: {scores['invoice']}/{n} ({100*scores['invoice']/n:.1f}%)")
    print(f"MOBILE_DESC  exact match: {scores['mobile']}/{n} ({100*scores['mobile']/n:.1f}%)")
    print(f"SHORT_DESC   exact match: {scores['short']}/{n} ({100*scores['short']/n:.1f}%)")
    print(f"RETAIL_DESC  exact match: {scores['retail']}/{n} ({100*scores['retail']/n:.1f}%)")
    print(f"Non-LED rows wrongly mentioning 'bulb'/'LED': {bulb_mentions_on_non_bulb}")


if __name__ == "__main__":
    main()
