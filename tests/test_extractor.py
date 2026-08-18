"""
Tests for Stage 4 extraction (backend/pipeline/extractor.py).

test_extraction_matches_real_gt_s21354_values is a mocked-completion
regression pinned to a REAL live result: extract_attributes() was run live
against Groq (llama-3.3-70b-versatile) with evidence text built from the
real S21354 LONG_DESC1 in data/ground_truth/gt_delivery_200.csv, and every
one of the 19 non-empty slots it returned exactly matched that row's real
ATTRIBUTE_VALUE fields (including which slots GT itself leaves empty:
Series, Halogen/Fluorescent/HID Wattage Equivalent, Smart Compatible With,
Energy Star Certified, Title 24 Compliant, Additional Information). This
test mocks the completion to pin that verified-correct JSON shape so CI
doesn't depend on live API state, while test_reconcile_* below exercise the
pure merge logic with no network at all.
"""
import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
from groq import PermissionDeniedError

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from pipeline.extractor import extract_attributes, reconcile  # noqa: E402
from pipeline.rule_preextractor import extract_uom_priors  # noqa: E402


def _403_client():
    """A Groq client whose completion call always raises PermissionDeniedError."""
    client = MagicMock()
    response = httpx.Response(403, request=httpx.Request("POST", "https://api.groq.com/x"))
    client.chat.completions.create = AsyncMock(
        side_effect=PermissionDeniedError("forbidden", response=response, body=None)
    )
    return client

LED_CLASSPATH = "Electrical>Lamps & Lightings>Light Bulbs>LED Light Bulbs"

# Verified-live extraction result for the real S21354 GT row (see module
# docstring) -- matches ATTRIBUTE_VALUE 1-27 in gt_delivery_200.csv exactly.
_S21354_LIVE_VERIFIED_EXTRACTION = {
    "Wattage": "8", "Lumens": "800", "Bulb Shape": "Tube", "Bulb Shape Code": "T9",
    "Color Temperature": "2700", "Light Appearance": "Warm White", "Bulb Base": "Medium",
    "Bulb Base Code": "E26", "Bulb Finish": "Clear", "Voltage Rating": "120",
    "Color Rendering Index (CRI)": "90+", "Bulb Designation": "8T9/LED/CL/927/120V/E26",
    "Average Life": "15000", "Beam Angle": "300", "Incandescent Wattage Equivalent": "60",
    "Dimmable": "Dimmable", "Diameter": "1.18", "Length": "7.2",
    "Title 20 Compliant": "Title 20 Compliant",
}


def _mock_client(json_payload: dict):
    client = MagicMock()
    response = MagicMock()
    response.choices = [MagicMock(message=MagicMock(content=json.dumps(json_payload)))]
    client.chat.completions.create = AsyncMock(return_value=response)
    return client


def test_extraction_matches_real_gt_s21354_values():
    client = _mock_client(_S21354_LIVE_VERIFIED_EXTRACTION)
    result = asyncio.run(extract_attributes("irrelevant with a mocked client", LED_CLASSPATH, client=client))
    assert result == _S21354_LIVE_VERIFIED_EXTRACTION


def test_extraction_drops_keys_not_in_the_template():
    """A JSON-schema-forced extraction must not accept invented/off-template
    keys even if the model hallucinates one."""
    client = _mock_client({"Wattage": "8", "Not A Real Slot Label": "should be dropped"})
    # Unique evidence_text per test (not re-used "text") so each test gets a
    # distinct cache key -- otherwise cached_call() returns a prior test's
    # stored completion and the mock client is never invoked, defeating the
    # isolation this test was written for. Pre-existing fragility surfaced
    # 2026-08-17 while validating the dynamic-upload extractor.py Fix2.
    result = asyncio.run(extract_attributes("text-drops-keys-unique", LED_CLASSPATH, client=client))
    assert result == {"Wattage": "8"}
    assert "Not A Real Slot Label" not in result


def test_extraction_drops_empty_values():
    client = _mock_client({"Wattage": "8", "Lumens": "", "Bulb Shape": None})
    result = asyncio.run(extract_attributes("text-drops-empty-unique", LED_CLASSPATH, client=client))
    assert result == {"Wattage": "8"}


def test_extraction_403_returns_empty_dict_not_raise():
    """extract_attributes() has no upstream fallback layer the way
    classify() does -- reconcile() already handles an empty dict
    gracefully (falls back to rule_priors), so a 403 here should degrade
    to {} rather than propagate and take the row down with it."""
    result = asyncio.run(extract_attributes("text-403-probe-unique", LED_CLASSPATH, client=_403_client()))
    assert result == {}


def test_reconcile_emits_full_27_slot_template_in_order():
    reconciled = reconcile(LED_CLASSPATH, _S21354_LIVE_VERIFIED_EXTRACTION, [])
    assert len(reconciled) == 27
    assert [s["slot"] for s in reconciled] == list(range(1, 28))
    assert reconciled[0]["label"] == "Series"
    assert reconciled[0]["value"] == ""  # not in extraction, GT also leaves it empty
    assert reconciled[1]["label"] == "Wattage"
    assert reconciled[1]["value"] == "8"
    assert reconciled[1]["uom"] == "W"


def test_reconcile_prefers_rule_prior_uom_when_value_matches():
    priors = extract_uom_priors("S21354 8W Led T9 Med 27k")  # [{value:8,uom:W},{value:2700,uom:K}]
    reconciled = reconcile(LED_CLASSPATH, _S21354_LIVE_VERIFIED_EXTRACTION, priors)
    wattage_slot = next(s for s in reconciled if s["label"] == "Wattage")
    assert wattage_slot["uom"] == "W"
    color_temp_slot = next(s for s in reconciled if s["label"] == "Color Temperature")
    assert color_temp_slot["value"] == "2700"
    assert color_temp_slot["uom"] == "K"


if __name__ == "__main__":
    test_extraction_matches_real_gt_s21354_values()
    test_extraction_drops_keys_not_in_the_template()
    test_extraction_drops_empty_values()
    test_extraction_403_returns_empty_dict_not_raise()
    test_reconcile_emits_full_27_slot_template_in_order()
    test_reconcile_prefers_rule_prior_uom_when_value_matches()
    print("All extractor tests passed.")
