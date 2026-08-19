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
    invoice_desc, mobile_desc, short_desc, retail_desc, long_desc1, generate_prose_descriptions,
    marketing_desc_should_skip_llm,
)
from pipeline.entity_resolver import resolve_manufacturer
from pipeline.enricher import enrich, EnrichmentResult, BROWSER_HEADERS, TIMEOUT
from pipeline.extractor import extract_attributes, fallback_extract_attributes, reconcile
from pipeline.led_philips_templates import led_marketing_and_features
from pipeline.llm_client import GroqClassifierClient
from pipeline.rule_preextractor import extract_led_shape_code, extract_uom_priors
from pipeline.satco_pdf import parse_satco_pdf_with_features, parse_satco_spec_chart_highbay
from schemas.product import AttributeValue, Descriptions, EnrichedProduct, ValidationResult
from validation.confidence import confidence_for_product
from validation.validators import run_validators

logger = logging.getLogger(__name__)

BRAND_FIELDS = ("E1_Brand", "Unilog_Brand", "DIB_Brand")
CONCURRENCY = 4
SNIPPET_CONTEXT_CHARS = 80

# The 6 input columns persisted verbatim (pre-cleaner) into EnrichedProduct.
# raw_input_cols: the export path must round-trip "-- Unbranded --" /
# "-- No Unilog Brand --" / "-- No DIB Brand --" placeholders verbatim, which
# cleaner.py maps to None -- so the orchestrator captures the raw row BEFORE
# clean_row runs. See EnrichedProduct.raw_input_cols docstring + scripts/
# export_1000_submission.py's caveat comment for the round-trip guarantee.
_INPUT_COLS = (
    "Mfg_Part_Num", "Part_Desc", "E1_Brand",
    "Unilog_Brand", "DIB_Brand", "Part_Manuf",
)


def _capture_raw_input(row: dict) -> dict[str, str]:
    """Slice the cleaned/persisted input row down to the 6 known input
    columns, as strings. Missing keys stay absent rather than "" -- means
    a pre-change cached SQLite row that lacks raw_input_cols altogether
    still parses (``raw_input_cols == {}``) and the exporter falls through
    to "" for the 3 brand cells, matching the documented accepted
    degradation for old data."""
    return {k: str(row[k]) for k in _INPUT_COLS if k in row}

# The leaf Classpath that pipeline.satco_pdf knows how to direct-map.
# Kept here rather than in satco_pdf.py so a future addition (another
# classpath Satco publishes PDFs for) needs only a parser update + an
# update here, not a schema change.
_LED_CLASSPATH = "Electrical>Lamps & Lightings>Light Bulbs>LED Light Bulbs"

# Satco High Bay Fixtures: a per-MPN "Spec_Chart.pdf" is hosted at a
# constructible URL on assets.satco.com that gives a deterministic
# {wattages, CCT columns, lumens matrix} for the SKU. We fetch it
# opportunistically as a Stage-3 supplement AFTER the LLM-over-page-text
# extraction has run (the HTML product page already fills ~17/26 slots on
# its own); the PDF overrides exactly two slots the LLM tends to flatten
# to single values ("Fixture Wattage=150" instead of "150/175/200" and
# "Color Temperature=4000 K" instead of the full CCT set). See the
# module docstring of pipeline.satco_pdf.parse_satco_spec_chart_highbay
# for the rationale.
_HB_CLASSPATH = "Electrical>Lamps & Lightings>Indoor Lighting>High Bay Fixtures"
_SATCO_CANONICAL_NAME = "Satco Products, Inc"
_SATCO_SPEC_CHART_URL_TEMPLATE = (
    "https://assets.satco.com/media-prod/image/upload/Certs/{mpn}_Spec_Chart.pdf"
)


def _satco_spec_chart_url(mpn: str) -> str:
    """Constructible URL for Satco's per-SKU Spec_Chart.pdf. Returns the
    URL unconditionally -- the caller probes it with a HEAD/GET and
    treats a 404 as 'no Spec_Chart available for this MPN', which is the
    common case outside the few UFO Highbay SKUs that actually carry one
    (65-771R2, 65-771R3 observed so far). No prefetched MPN allowlist
    because the URL template produces a real PDF even for MPNs that
    aren't in the GT 200 row set (per row would 200 OK if Satco has one
    -- 404 otherwise), and prefetching thousands would be wasteful."""
    return _SATCO_SPEC_CHART_URL_TEMPLATE.format(mpn=mpn)


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
    item_features: list[str] | None = None,
) -> Descriptions:
    """Stage 5: INVOICE/MOBILE/SHORT/RETAIL are deterministic GT-mined
    templates (description_gen.py); LONG_DESC1 is a deterministic GT-mined
    template for LED Light Bulbs and LLM-generated prose for every other
    leaf. MARKETING_DESCRIPTION is LLM-generated prose fed only the
    reconciled attributes (so it can't invent a spec that isn't already
    validated) -- except for LED Light Bulbs, where the Satco path ships
    EMPTY (spec sheets carry no marketing prose, never invented) and the
    Philips/Signify path uses a GT-mined boilerplate lookup keyed on
    (Bulb Shape Code, Color Temperature) instead of an LLM call -- see
    pipeline/led_philips_templates.py for why that's a template lookup,
    not free-text generation. Without a classpath there's no leaf
    template to key off of, so every field stays empty rather than
    guessing at a format."""
    if not classpath:
        return Descriptions()

    attrs = {a.label: a.value or "" for a in attributes}

    if classpath == _LED_CLASSPATH:
        # LED Light Bulbs: deterministic LONG_DESC1 (GT Option B). Satco
        # rows carry item_features harvested by the PDF direct-mapper and
        # ship no marketing copy (matches GT). Philips/Signify rows get
        # MARKETING_DESCRIPTION + ITEM_FEATURES from the GT-mined template
        # lookup when the row's (shape, color temp) combination was seen
        # in GT; otherwise both stay empty.
        marketing = None
        features = item_features or []
        if not features:
            marketing, features = led_marketing_and_features(manufacturer_name, attrs)
        return Descriptions(
            invoice_desc=invoice_desc(mpn, attrs, classpath) or None,
            mobile_desc=mobile_desc(mpn, manufacturer_name, attrs, classpath) or None,
            short_desc=short_desc(mpn, manufacturer_name, attrs, classpath) or None,
            retail_desc=retail_desc(attrs, classpath) or None,
            long_desc1=long_desc1(mpn, manufacturer_name, attrs, classpath) or None,
            marketing_description=marketing,
            item_features=features,
        )

    prose: dict[str, str] = {}
    if llm_configured_ and any(attrs.values()):
        prose = await generate_prose_descriptions(mpn, manufacturer_name, classpath, attrs)

    # marketing_description is suppressed (not the whole prose call) for
    # classpaths GT itself usually leaves blank -- LONG_DESC1 stays real
    # prose even on these classpaths (GT has non-empty LONG_DESC1 on every
    # one of them; it's specifically MARKETING_DESCRIPTION that's usually
    # empty), so the LLM call still runs to produce it. Without this, the
    # same free-text call invents plausible-sounding marketing copy (e.g.
    # "ideal for office spaces, retail displays...") for a field GT scores
    # as wrong unless it's blank -- see marketing_desc_should_skip_llm's
    # docstring for the GT-mined threshold.
    marketing = None if marketing_desc_should_skip_llm(classpath) else prose.get("marketing_description")
    return Descriptions(
        invoice_desc=invoice_desc(mpn, attrs, classpath) or None,
        mobile_desc=mobile_desc(mpn, manufacturer_name, attrs, classpath) or None,
        short_desc=short_desc(mpn, manufacturer_name, attrs, classpath) or None,
        retail_desc=retail_desc(attrs, classpath) or None,
        long_desc1=prose.get("long_desc1") or None,
        marketing_description=marketing or None,
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

    brand_name = next((cleaned[f] for f in BRAND_FIELDS if cleaned.get(f)), None)
    resolution = resolve_manufacturer(part_manuf_raw, part_desc=part_desc, mpn=mpn, brand=brand_name)
    manufacturer_name = resolution.manufacturer_name

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
    # Satco spec-sheet PDF shortcut: when the enricher fetched one of the
    # hard-coded Satco mirror URLs it returns status=FETCHED with
    # evidence_bytes set (a %PDF body) rather than evidence_text (HTML
    # page text). For LED Light Bulbs rows we run the deterministic
    # direct-mapper in pipeline.satco_pdf over those bytes -- this is
    # cheaper, faster and (measured on the 3 GT Satco LED rows) more
    # accurate than the LLM Stage 4 extraction path, because the PDF is
    # already labelled key-value data. The result is fed to reconcile()
    # identically to an LLM extraction, so all downstream pipeline
    # behaviour (slot ordering, rule_prior fallback, evidence provenance)
    # is unchanged.
    #
    # pdf_direct_mapped guards the second `if` block below from
    # re-running the LLM Stage 4 extraction over the same row. Without
    # it, when enrich() returns BOTH evidence_bytes (the %PDF body) AND
    # evidence_text (a short preview string built by enricher.py), the
    # PDF-direct-map result is silently overwritten by the LLM cache-hit
    # / fallback extraction -- observed regression: Satco LED row field
    # accuracy dropped from ~91% to 31.6% on S21354 because the prior
    # llm_extracted dict was clobbered by an LLM path that never even
    # saw the PDF (it scored evidence_text/Part_Desc, not the bytes).
    pdf_direct_mapped = False
    satco_item_features: list[str] = []
    if (
        classpath == _LED_CLASSPATH
        and enrichment.status == "FETCHED"
        and enrichment.evidence_bytes
    ):
        try:
            llm_extracted, satco_item_features = parse_satco_pdf_with_features(
                enrichment.evidence_bytes, mpn
            )
        except Exception:
            logger.exception(
                "parse_satco_pdf() raised for mpn=%r -- treating as empty",
                mpn,
            )
            llm_extracted = {}
            satco_item_features = []
        logger.debug(
            "mpn=%r satco_pdf direct-map keys=%s",
            mpn, list(llm_extracted.keys()),
        )
        if llm_extracted:
            # Successful PDF direct-map: short-circuit Stage 4 LLM
            # entirely for this row -- the PDF is already labelled
            # key-value data; the LLM extraction path operates on the
            # evidence_text preview (or Part_Desc fallback), neither of
            # which carries the PDF's structured content, so it would
            # only degrade the result.
            extraction_origin = "web_evidence"
            pdf_direct_mapped = True
        else:
            # If the PDF parse yielded nothing usable (e.g. a mirror URL
            # decayed into serving HTML instead of %PDF and slipped past
            # enricher.py's %PDF-body guard) fall through to the LLM path
            # / desc-only fallback below, rather than leaving the row
            # empty. Reset enrichment to FETCH_FAILED so the LLM block's
            # `FETCHED + evidence_text` guard below fails; the elif-desc
            # fallback then engages.
            logger.warning(
                "mpn=%r satco_pdf direct-map returned 0 slots; falling back to LLM/desc path",
                mpn,
            )
            enrichment = EnrichmentResult(
                status="FETCH_FAILED",
                source_url=enrichment.source_url,
                http_status=enrichment.http_status,
            )

    if (
        not pdf_direct_mapped
        and classpath
        and enrichment.status == "FETCHED"
        and enrichment.evidence_text
        and llm_configured()
    ):
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
    elif not pdf_direct_mapped and classpath and part_desc and llm_configured():
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
    elif not pdf_direct_mapped:
        # Neither web-extract nor desc-fallback engaged (no classpath, no
        # LLM key, no Part_Desc). Skipped entirely when pdf_direct_mapped
        # since Stage 4 was already done by the deterministic PDF mapper.
        logger.debug(
            "mpn=%r classpath=%r enrichment.status=%s NO EXTRACTION ATTEMPTED "
            "(classpath=%s part_desc=%s llm_configured=%s)",
            mpn, classpath, enrichment.status,
            bool(classpath), bool(part_desc), llm_configured(),
        )

    # Satco High Bay Fixtures Spec_Chart.pdf supplement: the LLM-over-
    # page-text path typically flattens the high-bay's multi-wattage /
    # multi-CCT configurations to a single representative value (e.g.
    # "Fixture Wattage=150" instead of the GT-expected "150/175/200").
    # The SKUs in this leaf publish a per-MPN "Spec_Chart.pdf" at a
    # constructible assets.satco.com URL containing the wattage/CCT/
    # lumen matrix; the deterministic PDF parser recovers exactly the
    # slash-separated multi-value formats GT carries. We MERGE the PDF
    # output into llm_extracted rather than replacing the whole dict --
    # the LLM stays authoritative for the ~24 other slots in the High
    # Bay leaf template (Voltage Rating, CRI, Fixture Material, etc.),
    # which the PDF doesn't cover. The PDF URL is recorded as the
    # source_url for the 2 specifically-PDF-derived slots rather than
    # the satco.com HTML page URL so V6 provenance traces to the actual
    # artefact the value was read from.
    spec_chart_source_urls: dict[str, str] = {}
    if (
        classpath == _HB_CLASSPATH
        and manufacturer_name == _SATCO_CANONICAL_NAME
        and http_client is not None
    ):
        spec_chart_url = _satco_spec_chart_url(mpn)
        try:
            response = await http_client.get(spec_chart_url)
            if response.status_code == 200 and response.content[:4] == b"%PDF":
                pdf_overrides = parse_satco_spec_chart_highbay(
                    response.content, mpn,
                )
                if pdf_overrides:
                    llm_extracted.update(pdf_overrides)
                    spec_chart_source_urls = {
                        label: spec_chart_url for label in pdf_overrides
                    }
                    if extraction_origin is None:
                        # Page text wasn't usable (LLM failure?) but the
                        # PDF gave us something -- mark the row web-
                        # evidenced because the PDF was fetched from
                        # the mfr's own CDN (assets.satco.com).
                        extraction_origin = "web_evidence"
                    logger.debug(
                        "mpn=%r spec_chart_pdf_overrides_keys=%s",
                        mpn, list(pdf_overrides.keys()),
                    )
            else:
                logger.debug(
                    "mpn=%r Spec_Chart.pdf not available (http=%s, head=%r)",
                    mpn, response.status_code,
                    response.content[:4] if response.content else b"<empty>",
                )
        except httpx.HTTPError as exc:
            logger.debug(
                "mpn=%r Spec_Chart.pdf fetch HTTPError: %r -- "
                "LLM-derived attributes remain authoritative",
                mpn, exc,
            )
        except Exception:
            logger.exception(
                "Unexpected error fetching/parsing Spec_Chart.pdf for "
                "mpn=%r -- treating as not-applicable, LLM extraction "
                "remains authoritative",
                mpn,
            )

    reconciled = reconcile(classpath, llm_extracted, rule_priors) if classpath else []

    if classpath == _LED_CLASSPATH:
        # Bulb Shape Code has no uom_hint, so reconcile()'s rule_prior
        # fallback (which only fills uom-tagged slots) never populates it
        # from Part_Desc. Fill it here via LOV-constrained matching when
        # still empty -- this is what lets led_marketing_and_features()
        # key off the shape code for rows that never got a real fetch
        # (see pipeline/led_philips_templates.py).
        for slot in reconciled:
            if slot["label"] == "Bulb Shape Code" and not slot["value"]:
                shape_code = extract_led_shape_code(part_desc)
                if shape_code:
                    slot["value"] = shape_code
                    slot["origin"] = "rule_prior"
                break

    attributes = []
    for slot in reconciled:
        origin = slot["origin"]
        source_url = None
        evidence_text = None
        if origin == "llm_extract" and slot["label"] in spec_chart_source_urls:
            # PDF-extracted value override (Satco High Bay Spec_Chart.pdf):
            # the value came from assets.satco.com's PDF, NOT from the
            # satco.com HTML product page, so its evidence / source_url
            # points to the PDF URL rather than the page URL.
            source_url = spec_chart_source_urls[slot["label"]]
            evidence_text = (
                f"[Satco Spec_Chart.pdf at {source_url}: parsed "
                f"{slot['label']}={slot['value']}]"
            )
        elif origin == "llm_extract" and extraction_origin == "web_evidence":
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

    descriptions = await _generate_descriptions(
        mpn, manufacturer_name, classpath, attributes, llm_configured(),
        item_features=satco_item_features or None,
    )

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
        raw_input_cols=_capture_raw_input(row),
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
        raw_input_cols=_capture_raw_input(row),
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
        headers=BROWSER_HEADERS, timeout=TIMEOUT, follow_redirects=True
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
