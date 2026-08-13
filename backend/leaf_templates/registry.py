"""
Load-bearing registry: leaf Classpath -> ordered attribute slot template.

Slots are mined from ground truth by scripts/build_gt_seeds.py into
data/bootstrap/leaf_templates.json. This module just loads and serves them;
it does not derive templates itself.
"""
import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

BOOTSTRAP_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "bootstrap" / "leaf_templates.json"


@dataclass(frozen=True)
class AttributeSlot:
    slot: int
    label: str
    uom_hint: str


@lru_cache(maxsize=1)
def _load() -> dict[str, list[AttributeSlot]]:
    raw = json.loads(BOOTSTRAP_PATH.read_text(encoding="utf-8"))
    return {
        classpath: [AttributeSlot(**s) for s in slots]
        for classpath, slots in raw.items()
    }


def get_template(classpath: str) -> list[AttributeSlot]:
    """Ordered attribute slots for a leaf Classpath. Empty list if unknown."""
    return _load().get(classpath, [])


def known_classpaths() -> list[str]:
    return sorted(_load().keys())
