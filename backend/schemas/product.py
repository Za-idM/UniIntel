"""Pydantic models for an enriched product record and its attribute evidence."""
from typing import Literal
from pydantic import BaseModel, Field

ConfidenceBand = Literal["VERIFIED", "REVIEW", "LOW"]


AttributeOrigin = Literal["rule_prior", "llm_extract"]


class AttributeValue(BaseModel):
    slot: int
    label: str
    value: str | None = None
    uom: str | None = None
    source_url: str | None = None
    evidence_text: str | None = None
    lov_valid: bool | None = None
    confidence: float | None = None
    origin: AttributeOrigin | None = None


class Descriptions(BaseModel):
    invoice_desc: str | None = None
    mobile_desc: str | None = None
    short_desc: str | None = None
    long_desc1: str | None = None
    retail_desc: str | None = None
    marketing_description: str | None = None


class ValidationResult(BaseModel):
    v1_required: bool = False
    v2_lov: bool = False
    v3_uom_inline: bool = False
    v4_casing_inline: bool = False
    v5_brand_mfr: bool = False
    v6_source_url: bool = False
    warnings: list[str] = Field(default_factory=list)
    needs_human_review: bool = False


class EnrichedProduct(BaseModel):
    product_id: str
    job_id: str

    mfg_part_num: str
    part_desc: str
    part_manuf_raw: str | None = None

    manufacturer_name: str | None = None
    brand_name: str | None = None
    classpath: str | None = None

    mfr_url: str | None = None
    ref_urls: list[str] = Field(default_factory=list)

    attributes: list[AttributeValue] = Field(default_factory=list)
    descriptions: Descriptions = Field(default_factory=Descriptions)

    validation: ValidationResult = Field(default_factory=ValidationResult)
    confidence: float = Field(0.0, ge=0, le=100)
    confidence_band: ConfidenceBand = "LOW"

    # Set only when process_row raised and orchestrator.process_job caught
    # it to keep the rest of the batch running -- this row's fields are a
    # best-effort stub (whatever was known before the failure), not a real
    # result. None for every normally-processed row.
    row_error: str | None = None
