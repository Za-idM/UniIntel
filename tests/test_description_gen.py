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
from pipeline.led_philips_templates import led_marketing_and_features  # noqa: E402
from pipeline.rule_preextractor import extract_led_shape_code  # noqa: E402

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


def test_led_philips_marketing_and_features_matches_at_least_baseline():
    """Baseline measured 2026-08-18: 12/19 Philips LED rows (63.2%) get an
    exact MARKETING_DESCRIPTION + ITEM_FEATURES match from the GT-mined
    (Bulb Shape Code, Color Temperature) template lookup -- up from 0/19
    when this field shipped hard-coded empty. The 7 remaining misses are
    rows whose Part_Desc never states a shape code at all (e.g. "571497
    150W Led Med 27k") -- genuinely unrecoverable from input text without
    a real fetch, so they correctly stay empty rather than guessing."""
    rows = [r for r in load_led_rows() if r.get("MANUFACTURER_NAME", "").strip() == "Signify Holding"]
    assert len(rows) == 19

    def gt_features(row):
        return [row[f"ITEM_FEATURES_{i}"].strip() for i in range(1, 21) if row.get(f"ITEM_FEATURES_{i}", "").strip()]

    matches = 0
    for row in rows:
        attrs = gt_attrs(row)
        mkt, feats = led_marketing_and_features("Signify Holding", attrs)
        if mkt == row["MARKETING_DESCRIPTION"].strip() and feats == gt_features(row):
            matches += 1
    assert matches >= 12, f"Philips LED marketing/features match rate regressed: {matches}/19"


def test_led_philips_template_empty_for_unseen_shape_ct_combo():
    """Safety requirement: a Philips LED row whose (shape, color temp)
    combination was never seen in GT must return empty, not the nearest
    mined template -- this is what makes the lookup safe to run on a
    judge's own uploaded dataset, which will contain SKUs outside the 19
    GT rows. "A19" at 5000K is a synthetic combo not in the mined table
    (GT only has A19 at the 2700K bucket)."""
    mkt, feats = led_marketing_and_features(
        "Signify Holding", {"Bulb Shape Code": "A19", "Color Temperature": "5000"}
    )
    assert mkt is None
    assert feats == []


def test_led_philips_template_empty_without_shape_code():
    """Same safety requirement, the common real-world case: Part_Desc
    never states a shape at all (e.g. "571497 150W Led Med 27k"), so
    Bulb Shape Code stays empty and the lookup must not guess."""
    mkt, feats = led_marketing_and_features(
        "Signify Holding", {"Bulb Shape Code": "", "Color Temperature": "2700"}
    )
    assert mkt is None
    assert feats == []


def test_led_philips_template_scoped_to_signify_only():
    """A non-Signify manufacturer must never get Philips-branded
    marketing copy, even if its shape/color-temp happens to match a
    mined key -- this text is Philips catalog boilerplate, not generic."""
    mkt, feats = led_marketing_and_features(
        "Satco Products, Inc", {"Bulb Shape Code": "A19", "Color Temperature": "2700"}
    )
    assert mkt is None
    assert feats == []


def test_extract_led_shape_code_handles_satco_style_st_prefix():
    """Philips Part_Desc uses the literal "ST19" token (Satco-style tube
    naming) for what GT's own canonical shape code calls "T19" -- the
    extractor returns the LOV value actually present in the text
    ("ST19"); the ST19->T19 alias is applied by the caller
    (pipeline/led_philips_templates.py), not here."""
    assert extract_led_shape_code("574004 75W Led ST19 27k 2pk") == "ST19"
    assert extract_led_shape_code("576496 45W Led R20 Med 27k") == "R20"
    assert extract_led_shape_code("571497 150W Led Med 27k") is None


if __name__ == "__main__":
    test_led_gt_row_count_unchanged()
    test_invoice_desc_never_exceeds_40_chars()
    test_invoice_desc_matches_at_least_baseline()
    test_short_desc_matches_at_least_baseline()
    test_retail_desc_matches_at_least_baseline()
    test_mobile_desc_matches_at_least_baseline()
    test_noun_is_category_specific_not_hardcoded_led()
    test_led_philips_marketing_and_features_matches_at_least_baseline()
    test_led_philips_template_empty_for_unseen_shape_ct_combo()
    test_led_philips_template_empty_without_shape_code()
    test_led_philips_template_scoped_to_signify_only()
    test_extract_led_shape_code_handles_satco_style_st_prefix()
    print("All description_gen regression checks passed.")
