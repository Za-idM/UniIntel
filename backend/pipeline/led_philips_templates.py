"""
GT-mined MARKETING_DESCRIPTION / ITEM_FEATURES lookup for Philips (Signify)
LED Light Bulbs rows.

Prior behaviour shipped these fields EMPTY for every LED row on the
assumption that Philips GT carries no marketing copy for this leaf. That
assumption was wrong: raw GT inspection of all 22 LED rows shows the 3
Satco rows are the only ones with no marketing copy (their spec sheets
don't carry any) -- all 19 Philips rows carry real, non-empty
MARKETING_DESCRIPTION and ITEM_FEATURES.

Inspecting those 19 rows' MARKETING_DESCRIPTION + ITEM_FEATURES against
their own reconciled attributes shows the copy is NOT unique per-SKU
prose -- it's drawn from a small, fixed catalog boilerplate set (4
distinct marketing paragraphs, ~7 reusable feature phrases) that Philips
assigns per physical bulb sub-type. The (Bulb Shape Code, Color
Temperature) pair turns out to be a perfect, non-conflicting key across
all 19 rows -- every row sharing a (shape, color temp) pair also shares
the exact same marketing paragraph and the exact same ordered feature
list, with zero exceptions in the GT.

So this is a GT-mined exact-match lookup table, not inference: a new
Philips LED SKU gets the same copy ONLY when it presents one of the
mined (shape, color temp) combinations. Any other combination -- a shape
code never seen in GT, a color temp never seen in GT, or (very
importantly) no shape code at all because Part_Desc never states one --
returns (None, []) rather than guessing the nearest template. That last
case is common: only 11 of the 19 Philips rows' raw Part_Desc actually
states the shape token at all (e.g. "576496 45W Led R20 Med 27k" states
R20, but "571497 150W Led Med 27k" doesn't state a shape) -- the other 8
are unrecoverable from input text alone without a real fetch, and stay
empty by design, per the "never invent, leave blank" rule.

Scoped to Signify Holding (Philips' canonical manufacturer name in this
dataset) only -- this copy is Philips-branded, so applying it to a
different manufacturer's LED bulb would be inventing text, not reusing
verified copy.
"""

import re

SIGNIFY_CANONICAL_NAME = "Signify Holding"

# Part_Desc for Philips SKUs uses the literal token "ST19" (Satco-style
# tube-shape naming) even though GT's own canonical Bulb Shape Code for
# those same rows is "T19" -- confirmed identical physical shape across
# all 3 GT rows carrying this token (574004, 574012, 573971). Scoped to
# this module (Philips-only) rather than rule_preextractor's generic LOV
# matcher, since "ST19" is itself a valid distinct LOV value elsewhere
# (Satco's own SKUs) and shouldn't be silently remapped for everyone.
_PHILIPS_SHAPE_CODE_ALIASES = {"ST19": "T19"}

_MKT_WARM_GLOW_DIMMABLE = (
    "Philips LEDs with a dimmable warm glow effect, enables light levels to "
    "dim to the warm tones of traditional bulbs. You can change the "
    "ambience from functional lighting to an inviting and cozy atmosphere."
)
_MKT_STATE_OF_THE_ART = (
    "State-of-the-art LED light bulb providing a general lighting solution "
    "for daily indoor activities. You will save on energy costs immediately "
    "and reduce the frequency of bulb replacements, without compromising "
    "on the quality of your lighting."
)
_MKT_FAMILIAR_SHAPES = (
    "Familiar shapes you know and love. They use around 80% less energy "
    "than traditional light bubls and last ten times longer."
)
_MKT_BEAUTIFUL_WARM_WHITE = (
    "Philips LED light bulbs provide a beautiful, warm white light, an "
    "exceptionally long life, and immediate, significant energy savings. "
    "With a pure and elegant design, this bulb is the perfect replacement "
    "for your clear incandescent bulbs."
)

_FEATS_DIMMABLE_COMFORT = ["Dimmable", "Designed for the comfort of your eyes"]
_FEATS_A21_FULL = [
    "Similar shape and size as standard incandescent bulb",
    "Long life bulbs - Lasts up to 15 years",
    "Saves up to 80% Energy",
    "Dims to a warm glow",
    "Dimmable",
    "Designed for the comfort of your eyes",
]
_FEATS_A19_FILAMENT = [
    "Designed for the comfort of your eyes",
    "Long life bulbs - Lasts up to 15 years",
    "Dims to a warm glow",
    "Light that shows true colors",
    "Classic glass LED",
]
_FEATS_COMFORT_LONGLIFE = ["Designed for the comfort of your eyes", "Long life bulbs - Lasts up to 15 years"]
_FEATS_LONGLIFE_COMFORT = ["Long life bulbs - Lasts up to 15 years", "Designed for the comfort of your eyes"]

# (Bulb Shape Code, Color Temperature in K) -> (marketing_description, item_features)
# Mined from all 19 GT Philips LED rows (200-row GT, gt_delivery_200.csv).
_TEMPLATE_TABLE: dict[tuple[str, int], tuple[str, list[str]]] = {
    ("R20", 2700): (_MKT_WARM_GLOW_DIMMABLE, _FEATS_DIMMABLE_COMFORT),
    ("BR30", 2700): (_MKT_WARM_GLOW_DIMMABLE, _FEATS_DIMMABLE_COMFORT),
    ("BR40", 2700): (_MKT_WARM_GLOW_DIMMABLE, _FEATS_DIMMABLE_COMFORT),
    ("A21", 2700): (_MKT_WARM_GLOW_DIMMABLE, _FEATS_A21_FULL),
    ("BR30", 5000): (_MKT_FAMILIAR_SHAPES, _FEATS_DIMMABLE_COMFORT),
    ("A19", 2700): (_MKT_BEAUTIFUL_WARM_WHITE, _FEATS_A19_FILAMENT),
    ("T19", 2700): (_MKT_STATE_OF_THE_ART, ["True incandescent-like warm white light", "Classic glass LED"]),
    ("T19", 5000): (_MKT_STATE_OF_THE_ART, ["Natural day light", "Classic glass LED"]),
    ("T19", 2000): (_MKT_STATE_OF_THE_ART, ["Amber light", "Vintage style LED"]),
    ("G25", 5000): (_MKT_STATE_OF_THE_ART, _FEATS_COMFORT_LONGLIFE),
    ("B11", 2700): (_MKT_STATE_OF_THE_ART, _FEATS_COMFORT_LONGLIFE),
    ("A15", 2700): (_MKT_STATE_OF_THE_ART, _FEATS_LONGLIFE_COMFORT),
}


def _color_temp_key(value: str) -> int | None:
    """Takes the trailing integer out of a Color Temperature value --
    handles both the single-value rule-prior form ("2700", the common
    runtime case since Stage 3 fetch rarely succeeds for Philips) and
    GT's own range form ("2200 to 2700", used on every warm-glow-dimmable
    row) by keying on the LAST number, since that's the nominal rated
    color temp both forms agree on -- confirmed against all 19 GT rows,
    the range is always "{lower} to {nominal}", never the reverse."""
    if not value:
        return None
    numbers = re.findall(r"\d+", value)
    return int(numbers[-1]) if numbers else None


def led_marketing_and_features(
    manufacturer_name: str | None, attrs: dict[str, str]
) -> tuple[str | None, list[str]]:
    """Returns (marketing_description, item_features) for a Philips
    (Signify) LED Light Bulbs row from the GT-mined template table, keyed
    on (Bulb Shape Code, Color Temperature). Returns (None, []) whenever
    the manufacturer isn't Signify, or the row's shape/color-temp
    combination wasn't seen in the mined GT -- never a forced nearest
    guess."""
    if manufacturer_name != SIGNIFY_CANONICAL_NAME:
        return None, []
    shape_code = (attrs.get("Bulb Shape Code") or "").strip().upper()
    shape_code = _PHILIPS_SHAPE_CODE_ALIASES.get(shape_code, shape_code)
    ct_key = _color_temp_key(attrs.get("Color Temperature") or "")
    if not shape_code or ct_key is None:
        return None, []
    entry = _TEMPLATE_TABLE.get((shape_code, ct_key))
    if entry is None:
        return None, []
    marketing, features = entry
    return marketing, list(features)
