"""
GT regression gate (locked build plan): run before every demo, block on any
field-accuracy regression against the 200-row ground truth.

Currently exercises what Step 1 actually built -- the leaf template registry
mined from GT -- since the extraction pipeline (Steps 3-7) doesn't exist yet.
Extend this as each pipeline stage lands.
"""
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from leaf_templates.registry import get_template  # noqa: E402
from pipeline.cleaner import clean_row  # noqa: E402
from pipeline.entity_resolver import resolve_manufacturer  # noqa: E402
from pipeline.normalizer import decimal_to_fraction  # noqa: E402
from pipeline.rule_preextractor import extract_uom_priors  # noqa: E402
from pipeline.classifier import rule_based_classify  # noqa: E402
from pipeline.llm_client import _shortlist_classpaths  # noqa: E402

GT_DELIVERY = ROOT / "data" / "ground_truth" / "gt_delivery_200.csv"
GT_INPUT = ROOT / "data" / "ground_truth" / "gt_input_200.csv"


def load_gt_rows():
    with open(GT_DELIVERY, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def test_gt_file_has_200_rows():
    rows = load_gt_rows()
    assert len(rows) == 200, f"expected 200 GT rows, found {len(rows)}"


def test_led_light_bulbs_is_largest_leaf():
    rows = load_gt_rows()
    from collections import Counter

    counts = Counter(r["Classpath"] for r in rows)
    top_classpath, top_count = counts.most_common(1)[0]
    assert top_classpath == "Electrical>Lamps & Lightings>Light Bulbs>LED Light Bulbs"
    assert top_count == 22


def test_led_leaf_template_has_27_slots():
    template = get_template("Electrical>Lamps & Lightings>Light Bulbs>LED Light Bulbs")
    assert len(template) == 27


def test_led_leaf_template_slot_order_matches_gt_reference():
    template = get_template("Electrical>Lamps & Lightings>Light Bulbs>LED Light Bulbs")
    labels = [s.label for s in template]
    assert labels[0] == "Series"
    assert labels[1] == "Wattage"
    assert labels[-1] == "Additional Information"


def test_every_leaf_template_slot_order_is_consistent_across_gt_rows():
    """Verifies the architecture doc's claim of '0 inconsistencies at leaf level':
    for every leaf Classpath, a given slot index always carries the same label
    across every GT row that has that leaf."""
    rows = load_gt_rows()
    seen = {}  # (classpath, slot) -> label
    violations = []
    for row in rows:
        classpath = row.get("Classpath", "").strip()
        if not classpath:
            continue
        for i in range(1, 51):
            label = row.get(f"ATTRIBUTE_LABEL {i}", "").strip()
            if not label:
                continue
            key = (classpath, i)
            if key in seen and seen[key] != label:
                violations.append((classpath, i, seen[key], label))
            seen[key] = label
    assert not violations, f"slot-order inconsistencies found: {violations}"


def test_entity_resolver_handles_canonical_led_example():
    result = resolve_manufacturer("Satco Prod Inc (5573)")
    assert result.status == "RESOLVED"
    assert result.manufacturer_name == "Satco Products, Inc"
    assert result.domain == "satco.com"


def test_entity_resolver_flags_ambiguous_distributor_codes():
    """'Appliance Dealers Cooperative' is a co-op distributor code that maps
    to 6+ different real manufacturers in GT -- must NOT be silently
    auto-resolved to one of them."""
    result = resolve_manufacturer("Appliance Dealers Cooperative (APPDE)")
    assert result.status == "NEEDS_DISAMBIGUATION"
    assert len(result.candidates) > 1


def test_entity_resolver_rejects_unrelated_garbage_fuzzy_match():
    """Regression for a real false positive: WRatio scored unrelated text
    containing 'Co (' boilerplate at 85.5 against 'Hager Hinge Co (4189)'.
    Fixed by stripping the (code) suffix and switching to token_sort_ratio."""
    result = resolve_manufacturer("Totally Unknown Distributor Co (999999)")
    assert result.status == "UNRESOLVED"


def test_entity_resolver_part_manuf_is_often_textually_unrelated_to_manufacturer():
    """Documents why fuzzy-matching Part_Manuf text against manufacturer
    names (the architecture doc's original plan) would fail: the majority
    LED-bulb case is 'Phillips Lighting (5831)' -> 'Signify Holding', which
    share no text. Resolution must be lookup-first."""
    result = resolve_manufacturer("Phillips Lighting (5831)")
    assert result.status == "RESOLVED"
    assert result.manufacturer_name == "Signify Holding"


def test_rule_preextractor_expands_color_temp_shorthand():
    """'27k' in a lighting description means 2700K, not literal 27K -- the
    exact S21354 example from the architecture doc."""
    priors = extract_uom_priors("S21354 8W Led T9 Med 27k")
    values = {p["uom"]: p["value"] for p in priors}
    assert values["W"] == "8"
    assert values["K"] == "2700"


def test_normalizer_fraction_uses_power_of_two_denominators_only():
    """Regression for a real bug: Fraction.limit_denominator(64) picked
    '1.18' -> '1-9/50', not a valid imperial fraction (denominators must be
    powers of two). Must fall back to the decimal instead of fabricating a
    fraction."""
    assert decimal_to_fraction("1.18") == "1.18"
    assert decimal_to_fraction("50.25") == "50-1/4"
    assert decimal_to_fraction("1.1875") == "1-3/16"


def test_classifier_gets_the_canonical_demo_example_right():
    """Regression for a real bug: length-normalized keyword-overlap scoring
    let 'Fluorescent Light Bulbs' (only 2 mined keywords, from a tiny GT
    sample) outscore 'LED Light Bulbs' (8 keywords, correct answer) on this
    exact S21354 example -- a single 'led' match hit 1/2 for the thin class
    vs. 2/8 for the rich one. Fixed by IDF-weighted-sum scoring instead of
    overlap/len(keyword_list)."""
    result = rule_based_classify("S21354 8W Led T9 Med 27k", "Satco Products, Inc")
    assert result.classpath == "Electrical>Lamps & Lightings>Light Bulbs>LED Light Bulbs"


def test_llm_shortlist_includes_correct_classpath():
    """Guards the cost-saving shortlist (backend/pipeline/llm_client.py):
    sending all 74 classpaths on every LLM call burned through the free-tier
    TPM budget in a live eval (429 after ~35 requests at concurrency 4). The
    rule-based baseline shortlists candidates instead -- must never drop the
    right answer for the canonical demo example."""
    shortlist = _shortlist_classpaths("S21354 8W Led T9 Med 27k", "Satco Products, Inc")
    assert "Electrical>Lamps & Lightings>Light Bulbs>LED Light Bulbs" in shortlist
    assert len(shortlist) <= 8


if __name__ == "__main__":
    test_gt_file_has_200_rows()
    test_led_light_bulbs_is_largest_leaf()
    test_led_leaf_template_has_27_slots()
    test_led_leaf_template_slot_order_matches_gt_reference()
    test_every_leaf_template_slot_order_is_consistent_across_gt_rows()
    test_entity_resolver_handles_canonical_led_example()
    test_entity_resolver_flags_ambiguous_distributor_codes()
    test_entity_resolver_rejects_unrelated_garbage_fuzzy_match()
    test_entity_resolver_part_manuf_is_often_textually_unrelated_to_manufacturer()
    test_rule_preextractor_expands_color_temp_shorthand()
    test_normalizer_fraction_uses_power_of_two_denominators_only()
    test_classifier_gets_the_canonical_demo_example_right()
    test_llm_shortlist_includes_correct_classpath()
    print("All GT regression checks passed.")
