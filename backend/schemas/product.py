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
    # Deterministic spec-cell-derived feature bullets (Satco LED rows via
    # pipeline.satco_pdf); empty for every other classpath. Written to the
    # delivery template's ITEM_FEATURES_1..20 columns by the shared
    # export/delivery_csv.py writer.
    item_features: list[str] = Field(default_factory=list)


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

    # Verbatim pre-cleaner values of the 6 input row columns (Mfg_Part_Num,
    # Part_Desc, E1_Brand, Unilog_Brand, DIB_Brand, Part_Manuf). Populated
    # by the orchestrator from the raw input row BEFORE cleaner.py maps
    # the "-- Unbranded --" / "-- No Unilog Brand --" / "-- No DIB Brand --"
    # placeholders to None. The export path (api/export.py + scripts/
    # export_1000_submission.py via backend/export/delivery_csv.py) must
    # round-trip those placeholder strings verbatim into the 252-col
    # delivery template -- sponsors expect "-- Unbranded --", not "".
    # persistence/schema.sql's products table does NOT carry the 3 raw
    # brand cols as separate columns, so persisting them inside data_json
    # via this field is the only way an SQLite-backed API route can emit
    # the original input values without a migration. Empty {} on pre-
    # change SQLite rows -- Pydantic default -- so old data_json blobs
    # still parse and the API falls through to "" for brand cells (an
    # accepted, documented degradation that only affects pre-change data;
    # every fresh upload populates this field).
    raw_input_cols: dict[str, str] = Field(default_factory=dict)
