"""
Stage 3: enrichment fetch. Manufacturer sites first, NEVER e-commerce
(hard rule from the VPs -- see UniHack_Final_Architecture.md Section 0).

This module only fetches + extracts page TEXT and records evidence/source
URL. Turning that text into the 27-slot attribute template is Stage 4
(extractor.py) and needs an LLM -- not wired to a live provider yet, no API
key configured in this environment.
"""
import asyncio
import httpx
import trafilatura
from dataclasses import dataclass

from .mfr_domain_map import construct_product_url

BLOCKED_DOMAINS = {"amazon.com", "ebay.com", "walmart.com", "homedepot.com", "lowes.com"}
USER_AGENT = "UniIntelBot/0.1 (product enrichment research; contact via Unilog)"
TIMEOUT = 10.0
MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 1.0


@dataclass
class EnrichmentResult:
    status: str  # FETCHED, NO_URL, BLOCKED_SOURCE, FETCH_FAILED
    source_url: str | None = None
    evidence_text: str | None = None
    http_status: int | None = None


def _is_blocked(url: str) -> bool:
    return any(domain in url.lower() for domain in BLOCKED_DOMAINS)


async def enrich(manufacturer_name: str, mpn: str, client: httpx.AsyncClient | None = None) -> EnrichmentResult:
    url = construct_product_url(manufacturer_name, mpn)
    if url is None:
        # no known direct-URL pattern for this manufacturer's domain --
        # would need Stage 3b site-scoped search here, not implemented.
        return EnrichmentResult(status="NO_URL")

    if _is_blocked(url):
        return EnrichmentResult(status="BLOCKED_SOURCE", source_url=url)

    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT, follow_redirects=True)
    try:
        response = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                response = await client.get(url)
            except httpx.HTTPError:
                return EnrichmentResult(status="FETCH_FAILED", source_url=url)

            if response.status_code == 200:
                break
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(BACKOFF_BASE_SECONDS * (2 ** attempt))
                    continue
            break  # non-retryable status, or retries exhausted

        if response.status_code != 200:
            return EnrichmentResult(status="FETCH_FAILED", source_url=url, http_status=response.status_code)

        text = trafilatura.extract(response.text)
        if not text:
            return EnrichmentResult(status="FETCH_FAILED", source_url=url, http_status=response.status_code)

        return EnrichmentResult(status="FETCHED", source_url=url, evidence_text=text, http_status=response.status_code)
    finally:
        if owns_client:
            await client.aclose()
