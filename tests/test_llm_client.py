"""
Regression pin for a real bug found live (2026-08-13): GroqClassifierClient
.classify() used to catch RateLimitError (retries exhausted) and
PermissionDeniedError (403) and return the string "UNKNOWN" instead of
raising. orchestrator._classify() only falls back to rule_based_classify
when llm_classify() raises -- a returned "UNKNOWN" looked like a real
(if unhelpful) LLM answer, so the fallback never engaged and "UNKNOWN"
got used directly as the row's classpath. That cascades into an empty
leaf template (get_template("UNKNOWN") finds nothing), zeroed-out
attribute reconciliation, and every description falling into the
non-LED generic path -- even for genuine LED Light Bulb rows. Confirmed
live on a 200-row run where a flaky network was 403-blocking Groq: it
regressed classpath accuracy 87.5%->66.0%, manufacturer 86.5%->52.5%,
attributes 2.1%->0.0%, and every description field to a flat 0.0%.

Both branches must re-raise so orchestrator._classify's try/except (see
pipeline/orchestrator.py) actually falls back to the rule-based baseline,
per this module's own docstring promise ("never hide uncertainty").
"""
import asyncio
import sys
from pathlib import Path

import httpx
import pytest
from groq import PermissionDeniedError, RateLimitError

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from pipeline.classifier import rule_based_classify  # noqa: E402
from pipeline.orchestrator import _classify  # noqa: E402

PART_DESC = "S21354 8W Led T9 Med 27k"
MANUFACTURER = "Signify Holding"


class _RaisingLLMClient:
    def __init__(self, exc: Exception):
        self._exc = exc

    async def classify(self, part_desc: str, manufacturer_name: str | None) -> str:
        raise self._exc


def _fake_response(status: int) -> httpx.Response:
    return httpx.Response(status, request=httpx.Request("POST", "https://api.groq.com/x"))


def test_403_falls_back_to_rule_based_not_unknown():
    exc = PermissionDeniedError("forbidden", response=_fake_response(403), body=None)
    result = asyncio.run(_classify(PART_DESC, MANUFACTURER, _RaisingLLMClient(exc)))
    expected = rule_based_classify(PART_DESC, MANUFACTURER)
    assert result.method == "RULE_BASED"
    assert result.classpath == expected.classpath
    assert result.classpath != "UNKNOWN"


def test_rate_limit_exhaustion_falls_back_to_rule_based_not_unknown():
    exc = RateLimitError("rate limited", response=_fake_response(429), body=None)
    result = asyncio.run(_classify(PART_DESC, MANUFACTURER, _RaisingLLMClient(exc)))
    expected = rule_based_classify(PART_DESC, MANUFACTURER)
    assert result.method == "RULE_BASED"
    assert result.classpath == expected.classpath
    assert result.classpath != "UNKNOWN"


if __name__ == "__main__":
    test_403_falls_back_to_rule_based_not_unknown()
    test_rate_limit_exhaustion_falls_back_to_rule_based_not_unknown()
    print("All llm_client regression checks passed.")
