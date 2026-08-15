"""Confidence scoring (Locked Decision #3 / Reconciled specifics): 0-100 scale.

Each input is a 0-1 fraction; the weighted sum is scaled to 0-100. The five
inputs are sourced from a populated EnrichedProduct + its validation result
by `confidence_for_product` below -- let run_validators run first so LOV
match rate, required-fields pass, and brand<->manufacturer consistency are
all known before the score is computed (V1/V5 feed `rule_validation`,
V2 supplies `lov_match`)."""

from dataclasses import dataclass

WEIGHTS = {
    "source_strength": 0.35,
    "extraction_conf": 0.30,
    "lov_match": 0.20,
    "rule_validation": 0.10,
    "multi_source_bonus": 0.05,
}

VERIFIED_THRESHOLD = 90
REVIEW_THRESHOLD = 70


def compute_confidence(
    source_strength: float,
    extraction_conf: float,
    lov_match: float,
    rule_validation: float,
    multi_source_bonus: float = 0.0,
) -> float:
    """Each input is a 0-1 fraction. Returns a 0-100 score."""
    score = (
        WEIGHTS["source_strength"] * source_strength
        + WEIGHTS["extraction_conf"] * extraction_conf
        + WEIGHTS["lov_match"] * lov_match
        + WEIGHTS["rule_validation"] * rule_validation
        + WEIGHTS["multi_source_bonus"] * multi_source_bonus
    )
    return round(100 * score, 2)


def confidence_band(score: float) -> str:
    if score >= VERIFIED_THRESHOLD:
        return "VERIFIED"
    if score >= REVIEW_THRESHOLD:
        return "REVIEW"
    return "LOW"


@dataclass
class ConfidenceInputs:
    """The 5 fractions consumed by compute_confidence. Surfaced so a
    dashboard / replay harness can show WHY each row landed in its band,
    not just the final number."""
    source_strength: float
    extraction_conf: float
    lov_match: float
    rule_validation: float
    multi_source_bonus: float


def _infer_source_strength(product) -> float:
    """1.0 when the row pulled real manufacturer-page text (web_evidence
    path -- some attribute carries source_url), 0.5 when classpath + a
    manufacturer resolved but enrichment failed/blocked so only
    desc_fallback / rule_priors filled slots, 0.0 when nothing was
    resolved (no classpath / no manufacturer -- near-zero confidence)."""
    if any(a.source_url for a in product.attributes if a.value):
        return 1.0
    if product.classpath and (product.manufacturer_name or product.brand_name):
        return 0.5
    return 0.0


def _extraction_confidence(product) -> float:
    """Fraction of template slots filled with any value. A row that filled
    17/27 LED slots scores ~0.63; an empty row scores 0. This is the
    simplest defensible proxy for "how much did the extractor actually
    surface" without a per-attribute LLM self-score (a future refinement)."""
    attrs = product.attributes
    if not attrs:
        return 0.0
    filled = sum(1 for a in attrs if a.value)
    return filled / len(attrs)


def _lov_match_rate(product, validation) -> float:
    """Fraction of value-bearing attributes that pass LOV (post V2 repair).
    Falls back to 0.0 when no LOV data applies for the row (no classpath
    or no LOV entries -- LovResult.passed is True under 'nothing to check').
    Those rows lose the LOV weight per the formula's design, surfacing as
    REVIEW rather than VERIFIED even when extraction was spot-on."""
    if not product.attributes:
        return 0.0
    value_attrs = [a for a in product.attributes if a.value]
    if not value_attrs:
        return 0.0
    # If V2 ran and marked lov_valid on each attr post-repair, use it.
    if any(a.lov_valid is not None for a in value_attrs):
        return sum(1 for a in value_attrs if a.lov_valid) / len(value_attrs)
    # V2 didn't get a chance to flag lov_valid -- use the bool pass.
    return 1.0 if validation.v2_lov else 0.0


def _rule_validation(validation) -> float:
    """1.0 when the hard validators pass (V1 required fields present, V5
    brand<->manufacturer consistent). Soft warnings (V3/V4/V6) don't sink
    it -- a row with odd UOM spacing and a text-only V6 note can still be
    VERIFIED once other inputs are strong."""
    return 1.0 if (validation.v1_required and validation.v5_brand_mfr) else 0.0


def confidence_for_product(product, validation) -> tuple[float, str, ConfidenceInputs]:
    """Assemble the 5 fractions from a populated EnrichedProduct + the
    ValidationResult produced by run_validators. multi_source_bonus stays
    0 while the orchestrator is single-source (one fetched page per row)."""
    source_strength = _infer_source_strength(product)
    extraction_conf = _extraction_confidence(product)
    lov_match = _lov_match_rate(product, validation)
    rule_validation = _rule_validation(validation)
    multi_source_bonus = 0.0  # orchestrator.process_row is single-source

    score = compute_confidence(
        source_strength, extraction_conf, lov_match, rule_validation, multi_source_bonus
    )
    return score, confidence_band(score), ConfidenceInputs(
        source_strength, extraction_conf, lov_match, rule_validation, multi_source_bonus
    )
