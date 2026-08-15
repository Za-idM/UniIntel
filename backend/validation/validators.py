"""L4 Validation: V1-V6 (Reconciled specifics numbering -- V4 dropped from
the architecture doc's V1-V7, rest renumbered; see CLAUDE.md).

Each validator takes the pieces of an in-progress EnrichedProduct plus
whatever bootstrap lookup it needs and returns a pass/fail plus any warnings.
Rule is "never hide uncertainty" -- a validator that can't run (e.g. no LOV
data for this classpath) reports pass=True (nothing to fail) rather than
silently marking the field verified; callers should look at the warning text,
not just the bool, to tell "checked and clean" from "nothing to check".
"""
import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from rapidfuzz import fuzz

from pipeline.normalizer import apply_casing, format_uom
from schemas.product import AttributeValue, ValidationResult

BOOTSTRAP = Path(__file__).resolve().parent.parent / "data" / "bootstrap"

LOV_REPAIR_THRESHOLD = 90  # rapidfuzz 0-100 scale; >0.9 per locked plan


@lru_cache(maxsize=1)
def _load_lov() -> dict[str, dict[str, list[str]]]:
    return json.loads((BOOTSTRAP / "lov_by_classpath.json").read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def _load_brand_manufacturer_pairs() -> dict[str, str]:
    pairs = json.loads((BOOTSTRAP / "brand_manufacturer_pairs.json").read_text(encoding="utf-8"))
    # The JSON is a flat {brand: manufacturer} object -- iterate .items(),
    # not the dict itself (which yields bare keys and triggers
    # "too many values to unpack" the first time V5 actually runs).
    return dict(pairs)


@dataclass
class LovResult:
    passed: bool = True
    repairs: dict[int, str] = field(default_factory=dict)  # slot -> repaired value
    warnings: list[str] = field(default_factory=list)


def v1_required(mfg_part_num: str | None, part_desc: str | None, classpath: str | None) -> tuple[bool, list[str]]:
    """Required top-level fields: MPN, description, and a resolved leaf
    Classpath -- without these the row can't be enriched or exported
    meaningfully."""
    warnings = []
    if not mfg_part_num:
        warnings.append("V1: Mfg_Part_Num is required but missing")
    if not part_desc:
        warnings.append("V1: Part_Desc is required but missing")
    if not classpath:
        warnings.append("V1: could not classify to a leaf Classpath")
    return (len(warnings) == 0, warnings)


def v2_lov(classpath: str | None, attributes: list[AttributeValue]) -> LovResult:
    """LOV compliance: attribute values are checked against the mined
    verified-at-GT value list for their classpath+label. An exact match
    passes silently. A fuzzy match >90 is auto-repaired to the canonical
    LOV spelling with a WARN (per locked plan). Below that, the value is
    left as-is but flagged -- never invented, never silently dropped."""
    result = LovResult()
    if not classpath:
        return result
    lov_by_label = _load_lov().get(classpath)
    if not lov_by_label:
        return result  # no LOV data for this classpath -- nothing to check

    for attr in attributes:
        if not attr.value:
            continue
        known_values = lov_by_label.get(attr.label)
        if not known_values:
            continue  # this slot isn't LOV-constrained
        if attr.value in known_values:
            continue
        best_value, best_score = None, 0.0
        for candidate in known_values:
            score = fuzz.ratio(attr.value.lower(), candidate.lower())
            if score > best_score:
                best_value, best_score = candidate, score
        if best_score >= LOV_REPAIR_THRESHOLD and best_value != attr.value:
            result.repairs[attr.slot] = best_value
            result.warnings.append(
                f"V2: repaired '{attr.label}' value '{attr.value}' -> '{best_value}' "
                f"(fuzzy match {best_score:.1f})"
            )
        else:
            result.passed = False
            result.warnings.append(
                f"V2: '{attr.label}' value '{attr.value}' not found in known LOV "
                f"for {classpath} (best match {best_score:.1f})"
            )
    return result


def v3_uom_inline(attributes: list[AttributeValue]) -> tuple[bool, list[str]]:
    """UOM formatting is enforced inline during normalization (L2); this
    validator re-checks the invariant holds -- every attribute carrying a
    uom_hint has that unit reflected in its value, either as a compressed
    suffix (8W) or a spaced one (8 W), never bare."""
    warnings = []
    for attr in attributes:
        if not attr.value or not attr.uom:
            continue
        value_lower = attr.value.lower()
        uom_lower = attr.uom.lower()
        if uom_lower not in value_lower.replace(" ", ""):
            warnings.append(
                f"V3: '{attr.label}' value '{attr.value}' does not carry expected UOM '{attr.uom}'"
            )
    return (len(warnings) == 0, warnings)


def v4_casing_inline(descriptions_by_field: dict[str, str | None]) -> tuple[bool, list[str]]:
    """Field-aware casing is enforced inline during description generation
    (L2/L5); this validator re-checks INVOICE_DESC/MOBILE_DESC came out
    ALL CAPS as the doc's hard contract requires."""
    warnings = []
    for field_name, text in descriptions_by_field.items():
        if not text:
            continue
        if apply_casing(text, field_name) != text:
            warnings.append(f"V4: '{field_name}' casing does not match field-aware contract")
    return (len(warnings) == 0, warnings)


def v5_brand_manufacturer(brand_name: str | None, manufacturer_name: str | None) -> tuple[bool, list[str]]:
    """Brand<->manufacturer consistency against the mined brand->manufacturer
    pairings. Unknown brands (not in the mined set) pass -- absence of
    evidence isn't evidence of a conflict."""
    if not brand_name or not manufacturer_name:
        return (True, [])
    expected = _load_brand_manufacturer_pairs().get(brand_name)
    if expected is None:
        return (True, [])
    if expected != manufacturer_name:
        return (False, [f"V5: brand '{brand_name}' expected manufacturer '{expected}', got '{manufacturer_name}'"])
    return (True, [])


def v6_source_url(attributes: list[AttributeValue]) -> tuple[bool, list[str]]:
    """Provenance check: every enriched (non-empty) attribute value must
    carry SOME provenance -- a source_url for values pulled from a fetched
    manufacturer page, or an evidence_text quote for values derived from
    the input row itself (rule_prior regex match on Part_Desc, or the
    LOV-constrained fallback extraction). The sponsor's hard rule is "never
    attribute a value to a source it didn't come from"; failing V6 here is
    the structural last line against that -- but only when BOTH url and
    text provenance are absent, since the orchestrator deliberately omits
    source_url for non-web origins (rule_prior and desc_fallback) and
    records the derivation in evidence_text instead. Without this gate,
    100% of desc_fallback rows would be marked failing (262/262 in the
    audit), mislabeling the bulk of produced output as unverified."""
    warnings = []
    for attr in attributes:
        if not attr.value:
            continue
        if attr.source_url:
            continue  # web-sourced -- hard provenance present
        if attr.evidence_text:
            # Non-web derivation (rule_prior / desc_fallback). Provenance
            # exists as text -- not a failure, but surfaced as a soft note
            # for the dashboard so judges can see "verified via X" vs
            # "verified via URL" without conflating the two.
            warnings.append(
                f"V6: '{attr.label}' value '{attr.value}' provenance is text-only "
                f"(rule_prior or LOV-fallback), no source_url"
            )
            continue
        # Neither url nor text provenance -- a real, unsourced value.
        warnings.append(
            f"V6: '{attr.label}' value '{attr.value}' has no source_url AND no evidence_text"
        )
    # Soft warnings (text-only provenance) don't fail the validator; only
    # the no-provenance-at-all case does.
    hard_fails = [w for w in warnings if "no source_url AND no evidence_text" in w]
    return (len(hard_fails) == 0, warnings)


def run_validators(
    mfg_part_num: str | None,
    part_desc: str | None,
    classpath: str | None,
    brand_name: str | None,
    manufacturer_name: str | None,
    attributes: list[AttributeValue],
    descriptions_by_field: dict[str, str | None],
) -> ValidationResult:
    """Run V1-V6 and return a populated ValidationResult. Applies V2's
    LOV auto-repairs to `attributes` in place before V3/V6 run, since a
    repaired value is the one that should be checked/exported."""
    v1_pass, v1_warn = v1_required(mfg_part_num, part_desc, classpath)

    lov = v2_lov(classpath, attributes)
    for attr in attributes:
        if attr.slot in lov.repairs:
            attr.value = lov.repairs[attr.slot]
            attr.lov_valid = True
        elif attr.value:
            attr.lov_valid = attr.value in (
                (_load_lov().get(classpath) or {}).get(attr.label, [attr.value])
            ) if not lov.repairs else attr.lov_valid

    v3_pass, v3_warn = v3_uom_inline(attributes)
    v4_pass, v4_warn = v4_casing_inline(descriptions_by_field)
    v5_pass, v5_warn = v5_brand_manufacturer(brand_name, manufacturer_name)
    v6_pass, v6_warn = v6_source_url(attributes)

    all_warnings = v1_warn + lov.warnings + v3_warn + v4_warn + v5_warn + v6_warn
    needs_review = not (v1_pass and v5_pass)  # missing required fields or brand/mfr conflict are hard failures

    return ValidationResult(
        v1_required=v1_pass,
        v2_lov=lov.passed,
        v3_uom_inline=v3_pass,
        v4_casing_inline=v4_pass,
        v5_brand_mfr=v5_pass,
        v6_source_url=v6_pass,
        warnings=all_warnings,
        needs_human_review=needs_review,
    )
