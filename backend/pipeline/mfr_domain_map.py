"""Canonical manufacturer -> domain, and known reconstructible product-URL
templates (mined from GT MFR URLs by scripts/build_gt_seeds.py)."""
import json
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


def construct_product_url(manufacturer_name: str, mpn: str) -> str | None:
    """Direct URL construction for domains with a confirmed pattern (e.g.
    Satco: satco.com/products/{MPN}). Returns None if the manufacturer's
    domain has no reliable pattern -- caller must fall back to site-scoped
    search (not implemented here; needs a search API/LLM tool)."""
    domain_map, url_patterns = _load()
    domain = domain_map.get(manufacturer_name)
    if not domain or domain not in url_patterns:
        return None
    return url_patterns[domain]["template"].format(mpn=mpn)
