"""Confidence scoring (Locked Decision #3 / Reconciled specifics): 0-100 scale."""

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
