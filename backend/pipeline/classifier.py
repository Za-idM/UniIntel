"""
Stage 1: classify -> leaf Classpath.

Two strategies:
  - rule_based_classify: deterministic token-overlap + manufacturer-prior
    baseline, mined from GT (data/bootstrap/classpath_keywords.json,
    manufacturer_classpath_prior.json). No LLM/API key required -- this is
    what runs today.
  - llm_classify: pluggable Stage-1 LLM call (small/batched model per the
    architecture doc). Not wired to a live provider yet -- no API key is
    configured in this environment. Swap in a real client behind
    LLMClassifierClient once Groq/Gemini credentials are available; the
    rule-based baseline is the fallback either way per the doc's "never
    hide uncertainty" principle (Section 14.6).
"""
import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Protocol

BOOTSTRAP = Path(__file__).resolve().parent.parent / "data" / "bootstrap"

_TOKEN_RE = re.compile(r"[a-z]+")
_STOPWORDS = {"the", "a", "an", "with", "for", "and", "or", "in", "on", "of", "to"}


def _tokenize(text: str) -> set[str]:
    return {t for t in _TOKEN_RE.findall(text.lower()) if len(t) > 1 and t not in _STOPWORDS}


@dataclass
class ClassificationResult:
    classpath: str | None
    confidence: float  # 0-1, relative score margin, not calibrated probability
    method: str  # RULE_BASED or LLM
    runner_up: str | None = None


@lru_cache(maxsize=1)
def _load():
    keywords = json.loads((BOOTSTRAP / "classpath_keywords.json").read_text(encoding="utf-8"))
    mfr_prior = json.loads((BOOTSTRAP / "manufacturer_classpath_prior.json").read_text(encoding="utf-8"))
    return keywords, mfr_prior


def rule_based_classify(part_desc: str, manufacturer_name: str | None = None) -> ClassificationResult:
    """Score each known leaf Classpath by IDF-weighted token overlap with
    its mined keyword weights, plus a manufacturer-history bonus. Not a
    real classifier -- a cheap, explainable baseline to run before any LLM
    call is available.

    Scoring is a weighted sum (sum of matched tokens' IDF weights), NOT
    overlap-count normalized by keyword-list length -- the latter let a
    thin-vocabulary class (few GT examples) win off a single common-word
    match. See build_gt_seeds.build_classpath_keywords for the regression
    this fixes."""
    keywords, mfr_prior = _load()
    if not part_desc:
        return ClassificationResult(classpath=None, confidence=0.0, method="RULE_BASED")

    desc_tokens = _tokenize(part_desc)
    scores: dict[str, float] = {}
    for classpath, weighted_kw in keywords.items():
        matched_weight = sum(weight for token, weight in weighted_kw.items() if token in desc_tokens)
        if matched_weight:
            scores[classpath] = matched_weight

    if manufacturer_name and manufacturer_name in mfr_prior:
        total = sum(mfr_prior[manufacturer_name].values())
        for classpath, count in mfr_prior[manufacturer_name].items():
            bonus = 0.5 * (count / total)
            scores[classpath] = scores.get(classpath, 0.0) + bonus

    if not scores:
        return ClassificationResult(classpath=None, confidence=0.0, method="RULE_BASED")

    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    best_classpath, best_score = ranked[0]
    runner_up = ranked[1][0] if len(ranked) > 1 else None
    # normalize into a rough 0-1 confidence via score margin over the field,
    # not a calibrated probability
    total_score = sum(scores.values())
    confidence = best_score / total_score if total_score else 0.0
    return ClassificationResult(
        classpath=best_classpath, confidence=round(confidence, 3),
        method="RULE_BASED", runner_up=runner_up,
    )


class LLMClassifierClient(Protocol):
    async def classify(self, part_desc: str, manufacturer_name: str | None) -> str: ...


async def llm_classify(part_desc: str, manufacturer_name: str | None, client: LLMClassifierClient) -> ClassificationResult:
    classpath = await client.classify(part_desc, manufacturer_name)
    if classpath == "UNKNOWN":
        # LLM explicitly demurred (the configured prompt instructs it to
        # reply UNKNOWN when no candidate fits). Without this fallback the
        # row would emit ClassificationResult(classpath="UNKNOWN"), which
        # the downstream pipeline treats as a real classification and
        # blanks manufacturer-history priors + leaf-template slots
        # downstream for a 252-col row. Observed on 2026-08-17: the live
        # Groq openai/gpt-oss-* successors to the decommissioned
        # llama-3.x family aren't as strict about emitting the exact leaf
        # Classpath string -- they often truncate to an intermediate class
        # (e.g. "Electrical>Lamps & Lightings>Light Bulbs" rather than
        # "...>LED Light Bulbs"), which the validator in
        # GroqClassifierClient.classify routes to "UNKNOWN". Defer to the
        # rule-based baseline (87.3% LOO per CLAUDE.md) -- never hide that
        # we did, per the doc's "never hide uncertainty" principle.
        fb = rule_based_classify(part_desc, manufacturer_name)
        return ClassificationResult(
            classpath=fb.classpath,
            confidence=fb.confidence,
            method="RULE_BASED_LLM_UNKNOWN_FALLBACK",
            runner_up=fb.runner_up,
        )
    return ClassificationResult(classpath=classpath, confidence=1.0, method="LLM")
