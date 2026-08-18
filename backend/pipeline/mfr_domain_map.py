"""Canonical manufacturer -> domain, and known reconstructible product-URL
templates (mined from GT MFR URLs by scripts/build_gt_seeds.py)."""
import json
import re
from functools import lru_cache
from pathlib import Path

BOOTSTRAP = Path(__file__).resolve().parent.parent / "data" / "bootstrap"


@lru_cache(maxsize=1)
def _load():
    domain_map = json.loads((BOOTSTRAP / "manufacturer_domain_map.json").read_text(encoding="utf-8"))
    url_patterns = json.loads((BOOTSTRAP / "manufacturer_url_patterns.json").read_text(encoding="utf-8"))
    return domain_map, url_patterns


def get_domain(manufacturer_name: str) -> str | None:
    domain_map, _ = _load()
    return domain_map.get(manufacturer_name)


# ---------------------------------------------------------------------------
# Per-domain MPN -> URL-token transforms.
#
# Some manufacturers' real MFR URLs do not embed the raw Mfg_Part_Num
# directly but a normalised form of it (e.g. Leviton /products/gfnt1 from
# MPN R00-GFNT1-00K -- the storefront packaging prefix R00- and the
# colour-suffix -00K are stripped before the URL is built). These can never
# be auto-mined by build_gt_seeds.py since the URL doesn't literally contain
# the MPN, so each such domain needs a hand-coded transformer here.
#
# A transformer takes (mpn) and returns the URL-token string, or None to
# signal "this MPN doesn't fit the rule -- give up constructing a URL for
# it and let the caller fall back to NO_URL". Adding a new transform
# requires: (1) a function here, (2) registering it in _MPN_TRANSFORMERS,
# (3) a matching entry in manufacturer_url_patterns.json whose template
# uses {mpn} (the post-transform token is substituted into that placeholder).
# ---------------------------------------------------------------------------

# Leviton consumer GFCI/AFCI/receptacle SKUs follow
#   R##-MODEL-<digits><letters>
# where R## is the channel/packaging prefix (R00 retail, R12 TradeR-Spec
# bulk, R62 light-commercial, R92 ...), MODEL is the bare product id
# (GFNT1, AGTR1, GUAC1, ...) and the trailing suffix is a small digit-
# then-letters colour code. The consumer-facing /products/ URL uses the
# bare model, lower-cased, with a colour suffix appended only when the
# suffix encodes a non-default colour (the last letter is W -> -w for
# White; K alone is the default no-colour). Verified across all 5 GT
# Leviton rows: R00-GFNT1-00K / R92-GFWT1-0KW / R12-AGTR1-0KW /
# R62-GFTA1-0KW / R02-GUAC1-0BW all map cleanly:
#   -00K  -> gfnt1          (K last letter, no colour suffix)
#   -0KW  -> gfwt1-w        (W last letter -> -w)
#   -0KW  -> agtr1-w
#   -0KW  -> gfta1-w
#   -0BW  -> guac1-w
# If the transform can't recognise the MPN shape (not a 3-segment R##-
# prefixed SKU), it returns None -- caller falls back to NO_URL.
_LEVITON_MPN_RE = re.compile(
    r"^R\d{2}-(?P<model>[A-Z0-9]+)-(?P<digits>\d+)(?P<letters>[A-Z]+)$",
    re.IGNORECASE,
)


def _leviton_short_sku(mpn: str) -> str | None:
    m = _LEVITON_MPN_RE.match(mpn.strip())
    if not m:
        return None
    model = m.group("model").lower()
    letters = m.group("letters").upper()
    # Colour suffix rule (verified across all 5 GT Leviton rows): W as the
    # last letter -> -w; K alone or any K-terminated suffix -> no colour
    # suffix (K denotes natural/no colour in Leviton's part-numbering).
    suffix = "-w" if letters.endswith("W") else ""
    return f"{model}{suffix}"


_MPN_TRANSFORMERS = {
    "leviton.com": _leviton_short_sku,
}


def construct_product_url(manufacturer_name: str, mpn: str) -> str | None:
    """Direct URL construction for domains with a confirmed pattern (e.g.
    Satco: satco.com/products/{MPN}). Returns None if the manufacturer's
    domain has no reliable pattern -- caller must fall back to site-scoped
    search (not implemented here; needs a search API/LLM tool).

    For domains that need an MPN-normalisation step before substitution
    (e.g. Leviton R00-GFNT1-00K -> gfnt1), the matching transformer in
    _MPN_TRANSFORMERS is applied first; if the transformer declines
    (returns None) we treat the URL as unconstructible for that MPN and
    return None -- this is "URL pattern exists but doesn't fit this MPN",
    which is the same caller contract as an unknown domain."""
    domain_map, url_patterns = _load()
    domain = domain_map.get(manufacturer_name)
    if not domain or domain not in url_patterns:
        return None
    token = mpn
    transformer = _MPN_TRANSFORMERS.get(domain)
    if transformer is not None:
        token = transformer(mpn)
        if token is None:
            return None
    return url_patterns[domain]["template"].format(mpn=token)
