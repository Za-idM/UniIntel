"""
Regression gate for Stage 5 deterministic description templates
(pipeline/description_gen.py). Run scripts/evaluate_descriptions.py for
the full per-row report; this file pins the measured baseline so a future
change can't silently regress match rate against the 22 LED GT rows.
"""
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from pipeline.description_gen import invoice_desc, mobile_desc, short_desc, retail_desc  # noqa: E402

GT_DELIVERY = ROOT / "data" / "ground_truth" / "gt_delivery_200.csv"
LED_CLASSPATH = "Electrical>Lamps & Lightings>Light Bulbs>LED Light Bulbs"


def load_led_rows():
    with open(GT_DELIVERY, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    return [r for r in rows if r.get("Classpath", "").strip() == LED_CLASSPATH]


def gt_attrs(row):
    out = {}
    for i in range(1, 51):
        label = row.get(f"ATTRIBUTE_LABEL {i}", "").strip()
        value = row.get(f"ATTRIBUTE_VALUE {i}", "").strip()
        if label:
            out[label] = value
    return out


def test_led_gt_row_count_unchanged():
    assert len(load_led_rows()) == 22


def test_invoice_desc_never_exceeds_40_chars():
    for row in load_led_rows():
        got = invoice_desc(row["Mfg_Part_Num"], gt_attrs(row), LED_CLASSPATH)
        assert len(got) <= 40, f"{row['Mfg_Part_Num']}: {got!r} is {len(got)} chars"


def test_invoice_desc_matches_at_least_baseline():
    """Baseline measured 2026-08-12: 20/22 (90.9%). The 2 known misses are
    a GT-only appearance abbreviation (573971's 'AMB') and a GT-internal
    inconsistency on B11's product noun (573378 is B11 but GT labels it
    'Bulb' not 'Candle', unlike the otherwise-identical 574392) -- both
    accepted as real-data noise, not chased further."""
    rows = load_led_rows()
    matches = sum(1 for r in rows if invoice_desc(r["Mfg_Part_Num"], gt_attrs(r), LED_CLASSPATH) == r["INVOICE_DESC"])
    assert matches >= 20, f"INVOICE_DESC match rate regressed: {matches}/22"


def test_short_desc_matches_at_least_baseline():
    """Baseline measured 2026-08-12: 21/22 (95.5%). The 1 known miss
    (573378) is the same B11 Bulb/Candle GT inconsistency noted above."""
    rows = load_led_rows()
    matches = sum(
        1 for r in rows
        if short_desc(r["Mfg_Part_Num"], r.get("MANUFACTURER_NAME", "").strip(), gt_attrs(r), LED_CLASSPATH) == r["SHORT_DESC"]
    )
    assert matches >= 21, f"SHORT_DESC match rate regressed: {matches}/22"


def test_retail_desc_matches_at_least_baseline():
    """Baseline measured 2026-08-12: 21/22 (95.5%) -- RETAIL_DESC is
    SHORT_DESC with the "{brand} {mpn} " prefix stripped, confirmed
    identical across every sampled GT row; same 1 known B11 miss."""
    rows = load_led_rows()
    matches = sum(1 for r in rows if retail_desc(gt_attrs(r), LED_CLASSPATH) == r["RETAIL_DESC"])
    assert matches >= 21, f"RETAIL_DESC match rate regressed: {matches}/22"


def test_mobile_desc_matches_at_least_baseline():
    """Baseline measured 2026-08-12: 5/22 (22.7%) -- MOBILE_DESC's
    Finish/ColorTemp inclusion is genuinely inconsistent in GT itself
    (e.g. 574004 and 564385 have identical Finish/ColorTemp shape but one
    includes Finish in MOBILE_DESC and the other doesn't); this baseline
    reflects a best-effort heuristic, not a bug to keep chasing."""
    rows = load_led_rows()
    matches = sum(
        1 for r in rows
        if mobile_desc(r["Mfg_Part_Num"], r.get("MANUFACTURER_NAME", "").strip(), gt_attrs(r), LED_CLASSPATH) == r["MOBILE_DESC"]
    )
    assert matches >= 5, f"MOBILE_DESC match rate regressed: {matches}/22"


def test_noun_is_category_specific_not_hardcoded_led():
    """Regression pin for the cross-category bug: every generated
    INVOICE/MOBILE/SHORT/RETAIL description said 'BULB LED' regardless of
    the row's actual classpath, because the noun-building functions never
    received classpath at all. A dryer must not say 'bulb' anywhere."""
    dryer_classpath = "Appliances & Consumer Electronics>Laundry Appliances>Electric Dryers"
    attrs = {"Capacity": "7 cu-ft", "Color": "Matte Black"}
    for desc in (
        invoice_desc("DR7004BE", attrs, dryer_classpath),
        mobile_desc("DR7004BE", "Speed Queen", attrs, dryer_classpath),
        short_desc("DR7004BE", "Speed Queen", attrs, dryer_classpath),
        retail_desc(attrs, dryer_classpath),
    ):
        assert "bulb" not in desc.lower(), f"dryer description wrongly mentions 'bulb': {desc!r}"
        assert "led" not in desc.lower(), f"dryer description wrongly mentions 'LED': {desc!r}"
    assert "dryer" in invoice_desc("DR7004BE", attrs, dryer_classpath).lower()


if __name__ == "__main__":
    test_led_gt_row_count_unchanged()
    test_invoice_desc_never_exceeds_40_chars()
    test_invoice_desc_matches_at_least_baseline()
    test_short_desc_matches_at_least_baseline()
    test_retail_desc_matches_at_least_baseline()
    test_mobile_desc_matches_at_least_baseline()
    test_noun_is_category_specific_not_hardcoded_led()
    print("All description_gen regression checks passed.")
