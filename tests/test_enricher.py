"""
Mocked-transport tests for Stage 3 enrichment fetch (backend/pipeline/enricher.py).
Uses httpx.MockTransport rather than live network calls, since satco.com
(the demo category's manufacturer) bot-blocks with a persistent 429 --
verified live and expected per the architecture doc's "some manufacturer
sites will block bots... degrade gracefully" anticipation. Live retrieval
against the real domain should be spot-checked manually / in the deployed
environment, not depended on for CI.
"""
import asyncio
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from pipeline.enricher import enrich  # noqa: E402
from pipeline.mfr_domain_map import construct_product_url  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


def test_satco_url_constructs_correctly_from_mined_pattern():
    url = construct_product_url("Satco Products, Inc", "S21354")
    assert url == "https://www.satco.com/products/S21354"


def test_manufacturer_without_known_pattern_returns_no_url():
    """Philips/Signify URLs don't embed the MPN at all (see CLAUDE.md
    findings) -- no pattern should be mined or constructible for it."""
    url = construct_product_url("Signify Holding", "576496")
    assert url is None


def test_enrich_no_url_short_circuits_without_network_call():
    result = _run(enrich("Totally Unknown Mfr Not In Bootstrap", "X1"))
    assert result.status == "NO_URL"


def test_enrich_success_extracts_evidence_text():
    def handler(request):
        return httpx.Response(200, text="<html><body><h1>S21354 LED Filament Bulb</h1><p>8W, 2700K, T9, Medium base.</p></body></html>")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await enrich("Satco Products, Inc", "S21354", client=client)

    result = _run(run())
    assert result.status == "FETCHED"
    assert result.http_status == 200
    assert "8W" in result.evidence_text
    assert result.source_url == "https://www.satco.com/products/S21354"


def test_enrich_retries_on_429_and_recovers():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 2:
            return httpx.Response(429)
        return httpx.Response(200, text="<html><body><p>Recovered after retry</p></body></html>")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await enrich("Satco Products, Inc", "S21354", client=client)

    result = _run(run())
    assert result.status == "FETCHED"
    assert calls["n"] == 2


def test_enrich_gives_up_after_max_retries_on_persistent_429():
    def handler(request):
        return httpx.Response(429)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await enrich("Satco Products, Inc", "S21354", client=client)

    result = _run(run())
    assert result.status == "FETCH_FAILED"
    assert result.http_status == 429


if __name__ == "__main__":
    test_satco_url_constructs_correctly_from_mined_pattern()
    test_manufacturer_without_known_pattern_returns_no_url()
    test_enrich_no_url_short_circuits_without_network_call()
    test_enrich_success_extracts_evidence_text()
    test_enrich_retries_on_429_and_recovers()
    test_enrich_gives_up_after_max_retries_on_persistent_429()
    print("All enricher tests passed.")
