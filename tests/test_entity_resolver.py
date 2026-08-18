"""
Regression gate for entity_resolver's ADC (Appliance Dealers Cooperative)
secondary-signal disambiguation. GT verification (2026-08-19) found all 23
GT rows carrying this exact ambiguous raw code are resolvable from
Part_Desc/MPN alone; prior behaviour routed all of them to
NEEDS_DISAMBIGUATION with no attempt at a secondary signal.
"""
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from pipeline.entity_resolver import resolve_manufacturer  # noqa: E402

GT_INPUT = ROOT / "data" / "ground_truth" / "gt_input_200.csv"
GT_DELIVERY = ROOT / "data" / "ground_truth" / "gt_delivery_200.csv"
ADC_RAW = "Appliance Dealers Cooperative (APPDE)"


def load_adc_rows():
    with open(GT_INPUT, encoding="utf-8-sig") as f:
        inp = list(csv.DictReader(f))
    with open(GT_DELIVERY, encoding="utf-8-sig") as f:
        deliv = {r["Mfg_Part_Num"]: r for r in csv.DictReader(f)}
    rows = []
    for r in inp:
        if (r.get("Part_Manuf") or "").strip() == ADC_RAW:
            rows.append((r, deliv.get(r["Mfg_Part_Num"], {})))
    return rows


def test_adc_gt_row_count_unchanged():
    assert len(load_adc_rows()) == 23


def test_adc_resolves_all_23_gt_rows_to_correct_manufacturer():
    rows = load_adc_rows()
    mismatches = []
    for inp_row, gt_row in rows:
        result = resolve_manufacturer(
            ADC_RAW, part_desc=inp_row["Part_Desc"], mpn=inp_row["Mfg_Part_Num"]
        )
        expected = gt_row.get("MANUFACTURER_NAME", "").strip()
        if result.status != "RESOLVED" or result.manufacturer_name != expected:
            mismatches.append((inp_row["Mfg_Part_Num"], result.status, result.manufacturer_name, expected))
    assert not mismatches, f"ADC resolution mismatches: {mismatches}"


def test_adc_prefix_only_rows_resolve_with_no_text_signal():
    """The 2 rows with zero keyword signal in Part_Desc -- resolvable only
    via the single-row-mined exact MPN-prefix table (WDTS/PDSH), the same
    confidence tier as enricher.py's SATCO_PDF_MIRRORS. Pinned separately
    from the full-23 sweep above so a future refactor can't silently drop
    just these two without a dedicated test failing."""
    wdts = resolve_manufacturer(ADC_RAW, part_desc="WDTS7024RZ Dishwasher SS - Display Only", mpn="WDTS7024RZ")
    assert wdts.status == "RESOLVED"
    assert wdts.manufacturer_name == "Whirlpool Corporation"

    pdsh = resolve_manufacturer(ADC_RAW, part_desc="PDSH4816AF Dishwasher SS - Display Only", mpn="PDSH4816AF")
    assert pdsh.status == "RESOLVED"
    assert pdsh.manufacturer_name == "Rheem Manufacturing"


def test_adc_word_boundary_rejects_substring_false_positives():
    """Safety requirement: "GE"/"SQ" must not match as a substring inside
    an unrelated ALL-CAPS word -- a naive `"GE" in text` check would wrongly
    fire on "HUGE" (contains "GE") or "BISQUE" (contains "SQ") since case
    alone doesn't guard a substring match. Neither of these synthetic
    descriptions carries a real keyword or a known MPN prefix, so the
    correct outcome is NEEDS_DISAMBIGUATION, not a forced guess at Haier
    or Alliance Laundry."""
    huge = resolve_manufacturer(ADC_RAW, part_desc="ZZZ9999XX HUGE Capacity Unit SS", mpn="ZZZ9999XX")
    assert huge.status == "NEEDS_DISAMBIGUATION"
    assert huge.manufacturer_name is None

    bisque = resolve_manufacturer(ADC_RAW, part_desc="ZZZ9999XX BISQUE Finish Trim Kit", mpn="ZZZ9999XX")
    assert bisque.status == "NEEDS_DISAMBIGUATION"
    assert bisque.manufacturer_name is None


def test_adc_unrecognized_row_falls_back_to_needs_disambiguation():
    """A genuinely novel ADC row (no keyword, no known MPN prefix) must
    still fall through to NEEDS_DISAMBIGUATION with the full candidate
    list intact -- never a forced guess."""
    result = resolve_manufacturer(ADC_RAW, part_desc="Some Unbranded Appliance", mpn="ZZZ0000ZZ")
    assert result.status == "NEEDS_DISAMBIGUATION"
    assert result.candidates and len(result.candidates) >= 5


def test_non_adc_ambiguous_code_unaffected_by_secondary_signal():
    """Only the exact ADC raw string gets the secondary-signal lookup --
    other genuinely ambiguous raw codes (e.g. the mined "-" code) must
    still route straight to NEEDS_DISAMBIGUATION regardless of
    part_desc/mpn, since no ADC-specific table applies to them."""
    result = resolve_manufacturer("-", part_desc="576496 45W Led R20 Med 27k", mpn="576496")
    assert result.status == "NEEDS_DISAMBIGUATION"


def test_adc_resolution_without_part_desc_or_mpn_still_needs_disambiguation():
    """Backwards-compatible default: calling resolve_manufacturer with only
    part_manuf_raw (no secondary signal at all) must behave exactly as
    before this change -- NEEDS_DISAMBIGUATION, not a crash or a guess."""
    result = resolve_manufacturer(ADC_RAW)
    assert result.status == "NEEDS_DISAMBIGUATION"


if __name__ == "__main__":
    test_adc_gt_row_count_unchanged()
    test_adc_resolves_all_23_gt_rows_to_correct_manufacturer()
    test_adc_prefix_only_rows_resolve_with_no_text_signal()
    test_adc_word_boundary_rejects_substring_false_positives()
    test_adc_unrecognized_row_falls_back_to_needs_disambiguation()
    test_non_adc_ambiguous_code_unaffected_by_secondary_signal()
    test_adc_resolution_without_part_desc_or_mpn_still_needs_disambiguation()
    print("All entity_resolver regression checks passed.")
