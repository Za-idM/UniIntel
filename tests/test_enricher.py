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
    # XO Ventilation (xoappliance.com), not Satco: Satco now fails fast
    # before ever reaching the network (see the FETCH_BLOCKED tests below),
    # so it's no longer a usable stand-in for exercising the normal
    # fetch-succeeds path.
    def handler(request):
        return httpx.Response(200, text="<html><body><h1>XOU2470BCGS Range Hood</h1><p>Stainless steel, 30in.</p></body></html>")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await enrich("XO Ventilation", "XOU2470BCGS", client=client)

    result = _run(run())
    assert result.status == "FETCHED"
    assert result.http_status == 200
    assert "Stainless steel" in result.evidence_text
    assert result.source_url == "https://xoappliance.com/xo_products/XOU2470BCGS/"


def test_enrich_retries_on_429_and_recovers():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 2:
            return httpx.Response(429)
        return httpx.Response(200, text="<html><body><p>Recovered after retry</p></body></html>")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await enrich("XO Ventilation", "XOU2470BCGS", client=client)

    result = _run(run())
    assert result.status == "FETCHED"
    assert calls["n"] == 2


def test_enrich_gives_up_after_max_retries_on_persistent_429():
    def handler(request):
        return httpx.Response(429)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await enrich("XO Ventilation", "XOU2470BCGS", client=client)

    result = _run(run())
    assert result.status == "FETCH_FAILED"
    assert result.http_status == 429


def test_satco_fails_fast_without_any_network_call():
    """The real regression this is pinning: satco.com is confirmed
    persistently 429-blocked (not transient), so it must never even reach
    the retry loop -- zero requests, immediate FETCH_BLOCKED."""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(429)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await enrich("Satco Products, Inc", "S21354", client=client)

    result = _run(run())
    assert result.status == "FETCH_BLOCKED"
    assert result.source_url == "https://www.satco.com/products/S21354"
    assert calls["n"] == 0, "Satco must skip the network call entirely, not just skip retries after one attempt"


def test_satco_fast_fail_does_not_construct_client_when_none_given():
    """No injected client at all (production path, not just the mocked-
    transport test path) -- still short-circuits before constructing a
    real httpx.AsyncClient or making any request."""
    result = _run(enrich("Satco Products, Inc", "S21354"))
    assert result.status == "FETCH_BLOCKED"
    assert result.source_url == "https://www.satco.com/products/S21354"


def test_non_satco_manufacturer_unaffected_by_fast_fail():
    """Scope check: the fast-fail path is keyed on the satco.com domain
    specifically -- a different manufacturer hitting persistent 429s still
    goes through the normal retry/backoff and lands on FETCH_FAILED, not
    FETCH_BLOCKED."""
    def handler(request):
        return httpx.Response(429)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await enrich("XO Ventilation", "XOU2470BCGS", client=client)

    result = _run(run())
    assert result.status == "FETCH_FAILED"


if __name__ == "__main__":
    test_satco_url_constructs_correctly_from_mined_pattern()
    test_manufacturer_without_known_pattern_returns_no_url()
    test_enrich_no_url_short_circuits_without_network_call()
    test_enrich_success_extracts_evidence_text()
    test_enrich_retries_on_429_and_recovers()
    test_enrich_gives_up_after_max_retries_on_persistent_429()
    test_satco_fails_fast_without_any_network_call()
    test_satco_fast_fail_does_not_construct_client_when_none_given()
    test_non_satco_manufacturer_unaffected_by_fast_fail()
    print("All enricher tests passed.")
