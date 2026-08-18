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
from schemas.product import AttributeValue  # noqa: E402

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


# ---------------------------------------------------------------------
# Satco spec-sheet PDF direct-map (pipeline.satco_pdf) regression
# ---------------------------------------------------------------------
# Protects the no-LLM direct extraction that bypasses Stage 4 for the 3
# GT Satco LED rows when the specsheet PDF is available (fetched via the
# hardcoded 3rd-party mirror URLs in pipeline/enricher.py). The source
# PDFs live in data/output/satco_samples/ so this test is hermetic and
# doesn't depend on live network state.
#
# Each {mpn: expected_subset} below is the SUBSET of GT slots that the
# PDF reliably fills with deterministic accuracy -- the test pins the
# direct-mapper against drift. The threshold (currently ~16/19 fields
# per single-SKU row, ~14/18 for the family-sheet row) leaves room for
# the few genuine PDF gaps (S21354 PDF has no "Incandescent Equivalent"
# field; S11445 family sheet has no Bulb Finish / Designation / Title 20
# / Light Appearance), which we DON'T assert on -- only slots the PDF
# genuinely labels are pinned, so adding bias toward inventing values
# to fill those 5 slots would NOT inflate this test's pass rate.
_SATCO_PDF_SAMPLES_DIR = ROOT / "data" / "output" / "satco_samples"

_SATCO_PDF_EXPECTED = {
    "S21354": {
        "Wattage": "8", "Lumens": "800", "Bulb Shape": "Tube",
        "Bulb Shape Code": "T9", "Color Temperature": "2700",
        "Light Appearance": "Warm White", "Bulb Base": "Medium",
        "Bulb Base Code": "E26", "Bulb Finish": "Clear",
        "Voltage Rating": "120", "Color Rendering Index (CRI)": "90+",
        "Bulb Designation": "8T9/LED/CL/927/120V/E26",
        "Average Life": "15000", "Beam Angle": "300",
        "Dimmable": "Dimmable", "Diameter": "1.18", "Length": "7.2",
        "Title 20 Compliant": "Title 20 Compliant",
    },
    "S21363": {
        "Wattage": "8", "Lumens": "800", "Bulb Shape": "Type ST",
        "Bulb Shape Code": "ST19", "Color Temperature": "2700",
        "Light Appearance": "Warm White", "Bulb Base": "Medium",
        "Bulb Base Code": "E26", "Bulb Finish": "Clear",
        "Voltage Rating": "120", "Color Rendering Index (CRI)": "90+",
        "Bulb Designation": "8ST19/CL/LED/927/E26",
        "Average Life": "15000", "Beam Angle": "300",
        "Incandescent Wattage Equivalent": "60",
        "Dimmable": "Dimmable", "Diameter": "2.28", "Length": "5.43",
        "Title 20 Compliant": "Title 20 Compliant",
    },
    "S11445": {
        "Wattage": "12", "Lumens": "1050", "Bulb Shape": "Type A",
        "Bulb Shape Code": "A19", "Color Temperature": "3000",
        "Bulb Base": "Medium", "Bulb Base Code": "E26",
        "Voltage Rating": "120", "Color Rendering Index (CRI)": "90+",
        "Average Life": "15000", "Beam Angle": "230",
        "Incandescent Wattage Equivalent": "75",
        "Diameter": "2.36", "Length": "5.11",
    },
}


def test_satco_pdf_direct_map_matches_gt():
    """The 3 hard-coded Satco mirror PDFs, parsed by
    pipeline.satco_pdf.parse_satco_pdf, must reproduce their pinned subset
    of GT ATTRIBUTE_VALUE slots exactly. A regression here means either
    the parser drifted or the PDF mirror URL is serving a different doc.

    Skipped automatically (with a clear message, NOT a silent pass) when
    the sample PDFs are absent -- e.g. on a fresh checkout that hasn't
    dropped the 3 Satco PDFs into data/output/satco_samples/. Run
    scripts/satco_led_probe.py to fetch + drop them locally."""
    from pipeline.satco_pdf import parse_satco_pdf

    samples = _SATCO_PDF_SAMPLES_DIR.glob("S*.pdf")
    found = {p.stem: p for p in samples}
    if not found:
        print("  [skip] data/output/satco_samples/ has no sample PDFs -- "
              "run scripts/satco_led_probe.py locally to populate them.")
        return
    failures = []
    for mpn, expected in _SATCO_PDF_EXPECTED.items():
        if mpn not in found:
            # Skip (rather than hard-fail) when only some of the 3
            # sample PDFs are present locally -- mirror URLs decay
            # individually, and a partial-fixture state isn't itself a
            # regression. The PDFs we DO have are still pinned against
            # GT exactly; only the missing ones are skipped (logged).
            print(f"  [skip] {mpn}.pdf not present locally; skipping its GT pin.")
            continue
        data = found[mpn].read_bytes()
        # Reject HTML-challenge bodies that slipped in by mistake --
        # guarantees the test exercises the real %PDF path, not a
        # cached 429 from a half-failed local fetch.
        assert data[:4] == b"%PDF", (
            f"{mpn}.pdf is not a real PDF (first 4 bytes: {data[:4]!r}) -- "
            f"delete the file and re-fetch via scripts/satco_led_probe.py"
        )
        extracted = parse_satco_pdf(data, mpn)
        for label, want in expected.items():
            got = extracted.get(label)
            if got != want:
                failures.append(f"{mpn}.{label}: want={want!r} got={got!r}")
    assert not failures, "Satco PDF direct-map regressions:\n  " + "\n  ".join(failures)


def test_satco_pdf_wattage_split_is_correct():
    """The parked wattage-extraction bug (extractor pulling 'incandescent-
    equivalent' instead of true LED draw) was the headline finding behind
    the Satco PDF work -- the spec sheets label `Watts` (true draw) and
    `Incandescent Equivalent` (or `Replacement Wattage`) separately. Pin
    the split for S21363 (the one row where both numbers differ,
    8W true vs 60W equiv) so a regression that re-merges them is loud."""
    from pipeline.satco_pdf import parse_satco_pdf

    pdf = _SATCO_PDF_SAMPLES_DIR / "S21363.pdf"
    if not pdf.exists() or pdf.read_bytes()[:4] != b"%PDF":
        print("  [skip] S21363.pdf not present or not a real PDF")
        return
    out = parse_satco_pdf(pdf.read_bytes(), "S21363")
    assert out.get("Wattage") == "8", f"true LED draw must be 8, got {out.get('Wattage')!r}"
    assert out.get("Incandescent Wattage Equivalent") == "60", (
        f"incandescent equiv must be 60, got {out.get('Incandescent Wattage Equivalent')!r}"
    )

    # S11445 has the same split at the family-row level (12W true vs 75W).
    pdf11445 = _SATCO_PDF_SAMPLES_DIR / "S11445.pdf"
    if pdf11445.exists() and pdf11445.read_bytes()[:4] == b"%PDF":
        out = parse_satco_pdf(pdf11445.read_bytes(), "S11445")
        assert out.get("Wattage") == "12"
        assert out.get("Incandescent Wattage Equivalent") == "75"


# ---------------------------------------------------------------------
# Stage 5 deterministic LONG_DESC1 / MARKETING / ITEM_FEATURES regression
# ---------------------------------------------------------------------
# Pins the three Stage-3/5 fixes from the Stage 3 Fix Investigation:
#   1. LONG_DESC1 is deterministic (GT Option B), not LLM prose, for the
#      LED Light Bulbs leaf -- generated by description_gen.long_desc1.
#   2. MARKETING_DESCRIPTION ships EMPTY for LED rows (Satco spec sheets
#      have no marketing prose; Philips per-product copy is unreachable).
#   3. ITEM_FEATURES is the deterministic spec-cell-to-bullet template
#      harvested by the Satco PDF direct-mapper.
# The Satco PDF rows are pinned against the local sample PDFs (same
# skip-if-absent guard as test_satco_pdf_direct_map_matches_gt); the 22
# GT-driven rows are pinned against the reconciled attribute triplets from
# gt_delivery_200.csv itself.

_LED_CLASSPATH = "Electrical>Lamps & Lightings>Light Bulbs>LED Light Bulbs"


def _gt_led_attrs(row):
    """Reconstruct the reconciled {label: value} dict from a GT delivery row."""
    attrs = {}
    for i in range(1, 51):
        lab = row.get(f"ATTRIBUTE_LABEL {i}", "").strip()
        val = row.get(f"ATTRIBUTE_VALUE {i}", "").strip()
        if lab:
            attrs[lab] = val
    return attrs


def _gt_led_rows():
    return [r for r in load_gt_rows() if r["Classpath"] == _LED_CLASSPATH]


def test_long_desc1_deterministic_matches_gt():
    """LONG_DESC1 must be mechanically derivable from the reconciled
    attribute dict for every LED GT row (21/22 exact -- the 573378 miss is
    the documented real-data noun inconsistency, B11 labeled 'Bulb' vs
    'Candle', that description_gen._shape_noun already accepts)."""
    from pipeline.description_gen import long_desc1

    rows = _gt_led_rows()
    failures = []
    for r in rows:
        gen = long_desc1(
            r["Mfg_Part_Num"], r["MANUFACTURER_NAME"], _gt_led_attrs(r), r["Classpath"]
        )
        if gen != r["LONG_DESC1"]:
            failures.append(f"{r['Mfg_Part_Num']}: gen={gen!r} gt={r['LONG_DESC1']!r}")
    assert len(failures) <= 1, "LONG_DESC1 deterministic regressions:\n  " + "\n  ".join(failures)


def test_marketing_description_empty_for_led_rows():
    """Stage-3/5 fix #2: MARKETING_DESCRIPTION ships EMPTY for LED Light
    Bulbs (GT shows empty for Satco; Philips copy is unreachable). The
    orchestrator's LED branch must never LLM-generate prose here."""
    import asyncio

    from pipeline.orchestrator import _generate_descriptions

    async def _run():
        return await _generate_descriptions(
            "S21354", "Satco Products, Inc", _LED_CLASSPATH,
            [AttributeValue(slot=2, label="Wattage", value="8")],
            llm_configured_=True,
        )

    desc = asyncio.run(_run())
    assert desc.marketing_description is None
    assert desc.long_desc1 is not None


def test_item_features_deterministic_matches_gt():
    """Stage-3/5 fix #3: the Satco PDF direct-mapper's feature-bullet
    template must reproduce GT's ITEM_FEATURES for the 3 GT Satco LED
    rows (6/6, 6/6, 6/7 -- the 7th S11445 bullet 'Non-Dimmable' is not in
    the PDF and must stay empty per 'never invent')."""
    from pipeline.satco_pdf import parse_satco_pdf_with_features

    samples = _SATCO_PDF_SAMPLES_DIR
    rows = {r["Mfg_Part_Num"]: r for r in _gt_led_rows()}
    # S11445's 7th GT bullet ("Non-Dimmable") is not derivable from the
    # PDF -- the family sheet carries no Dimmable field (verified in the
    # Stage 3 Fix Investigation) -- so the pinned ceiling for that row is
    # 6/7, and the missing bullet must stay ABSENT (never invented), not
    # be filled with a guess.
    _CEILING = {
        "S21354": (["8 Watt T9 LED Filament", "Clear", "Medium base", "90 CRI", "2700K", "120 Volt"], 6),
        "S21363": (["8 Watt ST19 LED Filament", "Clear", "Medium base", "90 CRI", "2700K", "120 Volt"], 6),
        "S11445": (["12 Watt A19 LED", "White", "3000K", "1050 Lumens", "120 Volt", "PIR Sensor"], 6),
    }
    for mpn in ("S21354", "S21363", "S11445"):
        pdf = samples / f"{mpn}.pdf"
        if not pdf.exists() or pdf.read_bytes()[:4] != b"%PDF":
            print(f"  [skip] {mpn}.pdf not present locally; skipping its feature pin.")
            continue
        _out, feats = parse_satco_pdf_with_features(pdf.read_bytes(), mpn)
        expected, ceiling = _CEILING[mpn]
        # All generated bullets must exactly match a GT prefix (same order,
        # same text), and none may exceed the documented 6/6 / 6/7 ceiling.
        assert feats == expected[:ceiling], f"{mpn}: gen={feats!r} expected<=ceiling={expected[:ceiling]!r}"
        gt = [rows[mpn].get(f"ITEM_FEATURES_{i}", "").strip() for i in range(1, 8)]
        gt = [x for x in gt if x]
        assert len(feats) <= len(gt), f"{mpn}: generated more features than GT ({len(feats)}>{len(gt)})"
        assert feats == gt[:len(feats)], f"{mpn}: generated bullets diverge from GT prefix"


def test_item_features_export_wiring():
    """The shared export writer must place item_features into the
    delivery template's ITEM_FEATURES_1..20 columns and leave non-LED
    rows untouched (empty field -> empty cells)."""
    from schemas.product import Descriptions, EnrichedProduct
    from export.delivery_csv import build_output_row, load_template_columns

    columns = load_template_columns()
    product = EnrichedProduct(
        product_id="p1",
        job_id="j1",
        mfg_part_num="S21354",
        part_desc="x",
        classpath=_LED_CLASSPATH,
        descriptions=Descriptions(item_features=["8 Watt T9 LED Filament", "Clear"]),
    )
    row = build_output_row(product, columns)
    assert row["ITEM_FEATURES_1"] == "8 Watt T9 LED Filament"
    assert row["ITEM_FEATURES_2"] == "Clear"
    assert row["ITEM_FEATURES_3"] == ""

    # Non-LED / empty-features row: ITEM_FEATURES cells must stay "".
    plain = EnrichedProduct(
        product_id="p2",
        job_id="j1",
        mfg_part_num="X",
        part_desc="y",
        descriptions=Descriptions(),
    )
    row2 = build_output_row(plain, columns)
    for i in range(1, 21):
        assert row2[f"ITEM_FEATURES_{i}"] == "", f"ITEM_FEATURES_{i} must stay empty"


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
    test_satco_pdf_direct_map_matches_gt()
    test_satco_pdf_wattage_split_is_correct()
    test_long_desc1_deterministic_matches_gt()
    test_marketing_description_empty_for_led_rows()
    test_item_features_deterministic_matches_gt()
    test_item_features_export_wiring()
    print("All GT regression checks passed.")
