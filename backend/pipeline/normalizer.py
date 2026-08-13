"""
Field-aware UOM/casing normalizer (V3 UOM + V5 casing, enforced inline).

Two formatting contracts, confirmed from GT (see UniHack_Final_Architecture.md
Section 6): INVOICE/MOBILE use compressed UOM ("8W", "2700K"); SHORT/LONG/
RETAIL/MARKETING use spaced UOM ("8 W", "2700 K"). Same value, two
serializations depending on target field.
"""
import re
from fractions import Fraction

# fields that use the compressed contract
COMPRESSED_FIELDS = {"INVOICE_DESC", "MOBILE_DESC"}
SPACED_FIELDS = {"SHORT_DESC", "LONG_DESC1", "RETAIL_DESC", "MARKETING_DESCRIPTION"}

# GT dimension values use denominators up to 64ths (1/16, 3/32, 1/64), not
# just eighths -- a fixed lookup table missed most of them. Imperial
# fractions are always power-of-two denominators; Fraction.limit_denominator
# picks the closest *any* denominator (e.g. 1.18 -> 9/50), which isn't a
# valid imperial fraction, so we enumerate the power-of-two set explicitly.
IMPERIAL_DENOMINATORS = (2, 4, 8, 16, 32, 64)
FRACTION_TOLERANCE = 1e-6


def format_uom(value: str, uom: str, field: str) -> str:
    """Render "8", "W" -> "8W" (compressed field) or "8 W" (spaced field)."""
    value = value.strip()
    uom = uom.strip()
    if not uom:
        return value
    if field in COMPRESSED_FIELDS:
        return f"{value}{uom}"
    return f"{value} {uom}"


def decimal_to_fraction(value: str) -> str:
    """0.25 -> 1/4, "50.25" -> "50-1/4". Falls back to the original string
    if the value isn't a plain decimal, or doesn't land on a standard
    imperial (power-of-two-denominator) fraction within tolerance --
    e.g. "1.18" stays "1.18" rather than becoming a bogus "1-9/50"."""
    match = re.match(r"^(\d+)\.(\d+)$", value.strip())
    if not match:
        return value
    whole, frac_digits = match.groups()
    decimal_part = Fraction(f"0.{frac_digits}")

    for denom in IMPERIAL_DENOMINATORS:
        candidate = Fraction(round(decimal_part * denom), denom)
        if abs(candidate - decimal_part) < FRACTION_TOLERANCE:
            if candidate.denominator == 1:
                return value  # rounds to a whole number, e.g. "3.000"
            fraction_str = f"{candidate.numerator}/{candidate.denominator}"
            return fraction_str if whole == "0" else f"{whole}-{fraction_str}"

    return value  # no clean imperial fraction match -- keep as decimal


def apply_casing(text: str, field: str) -> str:
    if field == "INVOICE_DESC":
        return text.upper()
    if field == "MOBILE_DESC":
        return text.title()
    return text
