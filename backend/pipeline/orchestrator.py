"""
Orchestrator: wires Stage 1 (classify) -> Stage 3 (enrich) -> Stage 4
(extract/reconcile) -> Stage 5 (description generation) -> Stage 4b (L4
V1-V6 validators) -> Stage 5b (L5 calibrated confidence) into one call
per raw input row, producing a fully populated EnrichedProduct.

Per-attribute provenance: reconcile() tags each slot's origin ("llm_extract"
vs "rule_prior" vs None for an unfilled slot). source_url and
evidence_text are attached ONLY to web_evidence-origin llm_extract values --
a rule_prior or desc_fallback value came from a regex match on Part_Desc /
a LOV-constrained extraction from Part_Desc, not the fetched page, so it
must never carry that page's URL as if it were web evidence (a real bug
this fixed: enricher.py sets source_url even on FETCH_FAILED/BLOCKED_SOURCE,
e.g. Satco's 429, so blanket-applying it would have falsely attributed
rule-derived values to a page that was never actually read). Still
single-source (one fetched page per product, per the doc's Ref URL 2-5
note) -- multi-source provenance is a future extension, not a regression
from this pass.

L4/L5: run_validators (V1-V6) and confidence_for_product (the calibrated
5-term blend) are invoked on the populated EnrichedProduct before return
-- the previous _placeholder_confidence stub has been retired. Each row
now ships with a populated ValidationResult and a calibrated confidence
score + band; the assembled ConfidenceInputs are surfaced via the
product's attributes (lov_valid flags, evidence_text provenance) so the
dashboard can show WHY a row landed in its band, not just the number.
"""
import asyncio
import logging
import uuid

import httpx

from config import llm_configured
from pipeline.cleaner import clean_row
from pipeline.classifier import rule_based_classify, llm_classify, ClassificationResult
from pipeline.description_gen import (
    invoice_desc, mobile_desc, short_desc, retail_desc, generate_prose_descriptions,
)
from pipeline.entity_resolver import resolve_manufacturer
from pipeline.enricher import enrich, EnrichmentResult, USER_AGENT, TIMEOUT
from pipeline.extractor import extract_attributes, fallback_extract_attributes, reconcile
from pipeline.llm_client import GroqClassifierClient
from pipeline.rule_preextractor import extract_uom_priors
from schemas.product import AttributeValue, Descriptions, EnrichedProduct, ValidationResult
from validation.confidence import confidence_for_product
from validation.validators import run_validators

logger = logging.getLogger(__name__)

BRAND_FIELDS = ("E1_Brand", "Unilog_Brand", "DIB_Brand")
CONCURRENCY = 4
SNIPPET_CONTEXT_CHARS = 80


def _evidence_snippet(evidence_text: str | None, value: str) -> str | None:
    """Best-effort "here's where in the page we found this" snippet: a
    window of page text around the value's first case-insensitive
    occurrence. Falls back to the start of the page text if the value
    (e.g. an LLM-normalized number) doesn't appear verbatim -- still real
    text from the actual fetched page, not a fabricated quote."""
    if not evidence_text:
        return None
    idx = evidence_text.lower().find(value.lower()) if value else -1
    if idx == -1:
        return evidence_text[: SNIPPET_CONTEXT_CHARS * 2].strip()
    start = max(0, idx - SNIPPET_CONTEXT_CHARS)
    end = min(len(evidence_text), idx + len(value) + SNIPPET_CONTEXT_CHARS)
    snippet = evidence_text[start:end].strip()
    return f"...{snippet}..." if start > 0 else snippet


async def _classify(part_desc: str, manufacturer_name: str | None, llm_client) -> ClassificationResult:
    if llm_client is not None:
        try:
            return await llm_classify(part_desc, manufacturer_name, llm_client)
        except Exception:
            # LLM path failed (rate limit exhausted retries, network, etc.) --
            # fall back rather than fail the whole row. ClassificationResult.
            # method distinguishes RULE_BASED from LLM so this is visible,
            # not silently hidden, per the doc's "never hide uncertainty".
            pass
    return rule_based_classify(part_desc, manufacturer_name)


async def _generate_descriptions(
    mpn: str,
    manufacturer_name: str | None,
    classpath: str | None,
    attributes: list[AttributeValue],
    llm_configured_: bool,
) -> Descriptions:
    """Stage 5: INVOICE/MOBILE/SHORT/RETAIL are deterministic GT-mined
    templates (description_gen.py); LONG_DESC1/MARKETING_DESCRIPTION are
    LLM-generated prose fed only the reconciled attributes, so they can't
    invent a spec that isn't already validated. Without a classpath there's
    no leaf template to key off of, so every field stays empty rather than
    guessing at a format."""
    if not classpath:
        return Descriptions()

    attrs = {a.label: a.value or "" for a in attributes}

    prose: dict[str, str] = {}
    if llm_configured_ and any(attrs.values()):
        prose = await generate_prose_descriptions(mpn, manufacturer_name, classpath, attrs)

    return Descriptions(
        invoice_desc=invoice_desc(mpn, attrs, classpath) or None,
        mobile_desc=mobile_desc(mpn, manufacturer_name, attrs, classpath) or None,
        short_desc=short_desc(mpn, manufacturer_name, attrs, classpath) or None,
        retail_desc=retail_desc(attrs, classpath) or None,
        long_desc1=prose.get("long_desc1") or None,
        marketing_description=prose.get("marketing_description") or None,
    )


def _placeholder_confidence(enrichment: EnrichmentResult, attributes: list[AttributeValue]) -> tuple[float, str]:
    """DEPRECATED: provisional stand-in for L5's calibrated formula. Retained
    only so stale imports in tests or third-party scripts won't break with
    an ImportError -- the live pipeline now uses confidence_for_product
    from validation.confidence via process_row above. Do not call this;
    it returns a heuristic, not the locked 5-term blend."""
    if not attributes:
        return 0.0, "LOW"
    filled = sum(1 for a in attributes if a.value)
    fill_ratio = filled / len(attributes)
    score = round((30.0 if enrichment.status == "FETCHED" else 0.0) + fill_ratio * 70.0, 1)
    band = "VERIFIED" if score >= 90 else "REVIEW" if score >= 70 else "LOW"
    return score, band


async def process_row(
    row: dict,
    job_id: str,
    http_client: httpx.AsyncClient | None = None,
    llm_client=None,
) -> EnrichedProduct:
    """Run classify -> enrich -> extract -> reconcile for one raw input row
    and return a fully populated EnrichedProduct."""
    cleaned = clean_row(row)
    mpn = cleaned.get("Mfg_Part_Num") or ""
    part_desc = cleaned.get("Part_Desc") or ""
    part_manuf_raw = cleaned.get("Part_Manuf")

    resolution = resolve_manufacturer(part_manuf_raw)
    manufacturer_name = resolution.manufacturer_name
    brand_name = next((cleaned[f] for f in BRAND_FIELDS if cleaned.get(f)), None)

    classification = await _classify(part_desc, manufacturer_name, llm_client)
    classpath = classification.classpath

    rule_priors = extract_uom_priors(part_desc)

    if manufacturer_name:
        enrichment = await enrich(manufacturer_name, mpn, client=http_client)
    else:
        # no resolved manufacturer -- nothing to construct a product URL
        # from, skip the network round-trip entirely rather than calling
        # enrich() with a None manufacturer_name.
        enrichment = EnrichmentResult(status="NO_URL")

    llm_extracted: dict[str, str] = {}
    extraction_origin = None
    if classpath and enrichment.status == "FETCHED" and enrichment.evidence_text and llm_configured():
        try:
            llm_extracted = await extract_attributes(enrichment.evidence_text, classpath)
        except Exception:
            logger.exception(
                "extract_attributes() raised for mpn=%r classpath=%r -- treating as empty",
                mpn, classpath,
            )
            llm_extracted = {}
        extraction_origin = "web_evidence"
        logger.debug(
            "mpn=%r classpath=%r enrichment.status=%s web_extract_keys=%s",
            mpn, classpath, enrichment.status, list(llm_extracted.keys()),
        )
        if not llm_extracted and part_desc and llm_configured():
            # web evidence existed but yielded nothing usable (e.g. a
            # boilerplate/404/cookie-consent page trafilatura still managed
            # to pull text from) -- fall back to the desc-only extraction
            # rather than leaving the row with zero LLM-derived attributes.
            try:
                llm_extracted = await fallback_extract_attributes(
                    part_desc, classpath, manufacturer_name,
                )
            except Exception:
                logger.exception(
                    "fallback_extract_attributes() raised for mpn=%r classpath=%r -- treating as empty",
                    mpn, classpath,
                )
                llm_extracted = {}
            extraction_origin = "desc_fallback"
            logger.debug(
                "mpn=%r classpath=%r web_extract_empty -> fallback_extract_keys=%s",
                mpn, classpath, list(llm_extracted.keys()),
            )
    elif classpath and part_desc and llm_configured():
        # Fallback: web fetch failed/blocked/no-URL -- extract what we can
        # from Part_Desc constrained by LOV allowed values. This lifts
        # attribute accuracy from ~0% to a usable baseline.
        try:
            llm_extracted = await fallback_extract_attributes(
                part_desc, classpath, manufacturer_name,
            )
        except Exception:
            logger.exception(
                "fallback_extract_attributes() raised for mpn=%r classpath=%r -- treating as empty",
                mpn, classpath,
            )
            llm_extracted = {}
        extraction_origin = "desc_fallback"
        logger.debug(
            "mpn=%r classpath=%r enrichment.status=%s fallback_extract_keys=%s",
            mpn, classpath, enrichment.status, list(llm_extracted.keys()),
        )
    else:
        logger.debug(
            "mpn=%r classpath=%r enrichment.status=%s NO EXTRACTION ATTEMPTED "
            "(classpath=%s part_desc=%s llm_configured=%s)",
            mpn, classpath, enrichment.status,
            bool(classpath), bool(part_desc), llm_configured(),
        )

    reconciled = reconcile(classpath, llm_extracted, rule_priors) if classpath else []

    attributes = []
    for slot in reconciled:
        origin = slot["origin"]
        source_url = None
        evidence_text = None
        if origin == "llm_extract" and extraction_origin == "web_evidence":
            source_url = enrichment.source_url
            evidence_text = _evidence_snippet(enrichment.evidence_text, slot["value"])
        elif origin == "llm_extract" and extraction_origin == "desc_fallback":
            # Fallback extraction -- value came from LLM reading Part_Desc
            # constrained by LOV, NOT from a web page. No source_url.
            evidence_text = f'LOV-constrained extraction from: "{part_desc}"'
        elif origin == "rule_prior":
            evidence_text = f'Parsed from input description: "{part_desc}"'
        attributes.append(
            AttributeValue(
                slot=slot["slot"],
                label=slot["label"],
                value=slot["value"] or None,
                uom=slot["uom"] or None,
                source_url=source_url,
                evidence_text=evidence_text,
                origin=origin,
            )
        )

    descriptions = await _generate_descriptions(mpn, manufacturer_name, classpath, attributes, llm_configured())

    # Build the populated product first so L4/L5 can compute against it
    # directly (the validators read attributes for provenance + LOV checks,
    # confidence_for_product reads classpath/manufacturer_name/brand_name
    # for the source_strength input). Validation starts as the default
    # empty ValidationResult and is overwritten below by run_validators.
    product = EnrichedProduct(
        product_id=str(uuid.uuid4()),
        job_id=job_id,
        mfg_part_num=mpn,
        part_desc=part_desc,
        part_manuf_raw=part_manuf_raw,
        manufacturer_name=manufacturer_name,
        brand_name=brand_name,
        classpath=classpath,
        mfr_url=enrichment.source_url,
        ref_urls=[enrichment.source_url] if enrichment.source_url else [],
        attributes=attributes,
        descriptions=descriptions,
        validation=ValidationResult(),
        confidence=0.0,
        confidence_band="LOW",
    )

    # L4: V1-V6 against the populated row. V2 applies LOV auto-repairs to
    # attributes in place (so the reconciled/repaired values are what V3
    # re-checks for UOM and what gets exported), and V6 uses the
    # origin-gated rule (text-only provenance from rule_prior /
    # desc_fallback passes with a soft warning -- see validators.v6_source_url).
    descriptions_by_field = {
        "INVOICE_DESC": descriptions.invoice_desc,
        "MOBILE_DESC": descriptions.mobile_desc,
        "SHORT_DESC": descriptions.short_desc,
        "RETAIL_DESC": descriptions.retail_desc,
    }
    product.validation = run_validators(
        mpn, part_desc, classpath, brand_name, manufacturer_name,
        attributes, descriptions_by_field,
    )

    # L5: calibrated confidence formula (locked 5-term blend).
    # source_strength: 1.0 web-sourced / 0.5 partial / 0.0 nothing resolved.
    # extraction_conf: fraction of template slots filled.
    # lov_match: fraction of value-bearing attrs passing V2 (post-repair).
    # rule_validation: 1.0 only if V1 required + V5 brand<->mfr both pass.
    # multi_source_bonus: 0.0 while the orchestrator is single-source.
    product.confidence, product.confidence_band = confidence_for_product(
        product, product.validation
    )[:2]

    return product


def _error_product(row: dict, job_id: str, exc: Exception) -> EnrichedProduct:
    """Best-effort stub for a row whose process_row() call raised -- keeps
    it identifiable (mpn/part_desc if the raw row has them) and visibly
    marked via row_error, rather than either losing it silently or taking
    the whole job down with it. confidence stays 0/LOW: nothing here was
    actually verified."""
    return EnrichedProduct(
        product_id=str(uuid.uuid4()),
        job_id=job_id,
        mfg_part_num=row.get("Mfg_Part_Num") or "",
        part_desc=row.get("Part_Desc") or "",
        part_manuf_raw=row.get("Part_Manuf"),
        row_error=f"{type(exc).__name__}: {exc}",
    )


async def process_job(
    rows: list[dict],
    job_id: str,
    use_llm: bool | None = None,
    on_row_done=None,
) -> list[EnrichedProduct]:
    """Run process_row over every input row, semaphore-bounded per the
    locked concurrency plan (asyncio + httpx, 3-4 workers).

    on_row_done, if given, is awaited with each EnrichedProduct as soon as
    its row finishes -- not batched until the whole job completes. This is
    what lets a caller (api/process.py) persist + count progress
    incrementally instead of the client seeing nothing until every row is
    done, which on a real distributor file (hundreds of rows, each paying
    for LLM round-trips and enrichment-fetch retries) reads as a hang.

    A single row's uncaught exception (e.g. a Groq error class this
    module's own try/excepts don't already handle) must not take the
    other rows down with it -- caught per-row here and turned into an
    _error_product() stub so the rest of the batch keeps running and the
    job still reaches processed_rows == total_rows. return_exceptions=True
    on gather() is a second-layer backstop for anything that still slips
    past the per-row try/except (e.g. a bug in on_row_done itself)."""
    if use_llm is None:
        use_llm = llm_configured()
    llm_client = GroqClassifierClient() if use_llm else None

    semaphore = asyncio.Semaphore(CONCURRENCY)

    async with httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT, follow_redirects=True
    ) as http_client:

        async def _bounded(row: dict) -> EnrichedProduct:
            async with semaphore:
                try:
                    product = await process_row(row, job_id, http_client=http_client, llm_client=llm_client)
                except Exception as exc:
                    logger.exception(
                        "process_row failed for Mfg_Part_Num=%r -- marking row failed, continuing batch",
                        row.get("Mfg_Part_Num"),
                    )
                    product = _error_product(row, job_id, exc)
            if on_row_done is not None:
                await on_row_done(product)
            return product

        results = await asyncio.gather(*[_bounded(row) for row in rows], return_exceptions=True)

    products: list[EnrichedProduct] = []
    for row, result in zip(rows, results):
        if isinstance(result, Exception):
            # Slipped past _bounded's own try/except (e.g. on_row_done
            # raised) -- still log and stub it rather than propagate, same
            # contract as the per-row case above.
            logger.error(
                "unhandled exception for Mfg_Part_Num=%r after _bounded", row.get("Mfg_Part_Num"), exc_info=result
            )
            products.append(_error_product(row, job_id, result))
        else:
            products.append(result)
    return products
