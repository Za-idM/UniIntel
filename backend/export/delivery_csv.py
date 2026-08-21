"""Shared 252-column delivery-CSV writer.

Both submission paths -- the CLI script (`scripts/export_1000_submission.py`)
and the live API route (`backend/api/export.py`) -- funnel through this
module so the EnrichedProduct -> 252-col row mapping can never drift between
them. For a long-running judge demo where rows are uploaded through the UI
(POST /api/process -> SQLite) AND in batch via the script, byte-for-byte
equality of the two outputs is the proof the refactor worked; this module
is the only place the cell-level mapping logic lives.

The 6 brand/pass-through columns (Mfg_Part_Num, Part_Desc, E1_Brand,
Unilog_Brand, DIB_Brand, Part_Manuf) are read from EnrichedProduct's
`raw_input_cols` dict -- the orchestrator captures the verbatim, pre-cleaner
input values, so the placeholder strings sponsors wrote into the input file
("-- Unbranded --", "-- No Unilog Brand --", "-- No DIB Brand --") round-trip
into the delivery CSV as-is rather than being silently swapped for "". That
swap would happen if we instead read product.brand_name (cleaner.py maps
those placeholders to None) -- a materiality check confirmed by the 1000-row
scale data, which carries ~2554 such placeholder cells across the 3 brand
columns and would have silently changed 2.5K cells per submission if the
API route followed that line. Hence raw_input_cols on EnrichedProduct +
this shared module.

Everything else stays "" -- the "never invent values" rule from CLAUDE.md.
The columns we DO populate are documented at the top of
scripts/export_1000_submission.py; the mapping logic here is intentionally
identical to that older inlined version so byte-equality is preserved on
existing scripts."""
import csv
from pathlib import Path

# backend/export/delivery_csv.py -> backend/export -> backend/. NOT
# .parent.parent.parent (repo root) -- Railway's Root Directory is set to
# backend/, so only backend/ is deployed as the container's app root; a
# third .parent resolves one level ABOVE that root (e.g. to "/"), which is
# exactly the '/data/reference/delivery_format_template.csv' FileNotFoundError
# seen in production. Same bug, same fix as 1f06cd8 ("Fix data/bootstrap
# path resolution for Railway's backend-scoped Root Directory") applied to
# classifier.py/registry.py/entity_resolver.py/etc -- this module just
# predates that fix. The file is mirrored into backend/data/reference/ (like
# backend/data/bootstrap/ and backend/data/ground_truth/) so this same
# relative depth resolves correctly both locally and on Railway.
ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_CSV = ROOT / "data" / "reference" / "delivery_format_template.csv"

# Delivery columns populated by the orchestrator (everything else stays "").
DESC_FIELD_BY_COL = {
    "MOBILE_DESC": "mobile_desc",
    "INVOICE_DESC": "invoice_desc",
    "SHORT_DESC": "short_desc",
    "LONG_DESC1": "long_desc1",
    "RETAIL_DESC": "retail_desc",
    "MARKETING_DESCRIPTION": "marketing_description",
}

# ITEM_FEATURES_1..20 (template cols 29-48) are filled-if-present from
# Descriptions.item_features -- falls through to "" for any row without
# features (non-LED classpaths, Philips LED rows, pre-change data). Placed
# in its own map + loop below so the existing DESC_FIELD_BY_COL iteration
# order (and its byte-equality guarantee) is untouched.
MAX_ITEM_FEATURES = 20

MAX_ATTRIBUTE_SLOTS = 50  # template reserves 50 triplets; leaf templates vary (LED=27)

# The 6 pass-through input columns. Used as the canonical order for the
# raw_input_cols dict the orchestrator writes; labels match the delivery
# template's header names verbatim (the same names scale_input_1000.csv
# uses in its own header row).
INPUT_COLS = [
    "Mfg_Part_Num", "Part_Desc", "E1_Brand",
    "Unilog_Brand", "DIB_Brand", "Part_Manuf",
]


def load_template_columns() -> list[str]:
    """Read row 1 of delivery_format_template.csv -> the 252-col header.

    The template is the single source of truth for the delivery schema's
    column set AND ordering (slot triplet ordering, Ref URL index order,
    description column positional alignment all depend on this exact
    sequence). Re-reading on every export call is intentional: it keeps a
    last-minute template swap from silently producing a differently-shaped
    CSV vs the script run.

    Raises a clear RuntimeError (not a raw FileNotFoundError) if the file
    is missing -- e.g. a future deploy-root change breaks this same path
    assumption again -- so a live demo/judge sees a diagnosable 500 message
    instead of a bare stack trace with no pointer to the fix."""
    if not TEMPLATE_CSV.exists():
        raise RuntimeError(
            f"Delivery template CSV not found at {TEMPLATE_CSV}. This path is "
            "computed as backend/'s own directory + data/reference/ -- see "
            "delivery_csv.py's ROOT comment. If the deploy's Root Directory "
            "setting changed, or the file was moved/renamed, update ROOT "
            "and/or re-copy the file into backend/data/reference/."
        )
    with open(TEMPLATE_CSV, encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        return next(reader)


def build_output_row(product, columns: list[str]) -> dict:
    """Map an EnrichedProduct -> a 252-key dict keyed on the template columns.

    Brand / pass-through cells (E1_Brand / Unilog_Brand / DIB_Brand / Mfg_Part_Num
    / Part_Desc / Part_Manuf) are read from `product.raw_input_cols` -- the
    verbatim raw input captured pre-cleaner. If raw_input_cols is empty the
    brand cols fall back to "" (the documented degradation for pre-change
    SQLite data_json blobs that predate the field; every fresh upload
    populates it). The product's own resolved identity fields are still
    used as canonical -- `Mfg_Part_Num`, `Part_Manuf`, `manufacturer_name`,
    `brand_name` -- because raw_input_cols.E1_Brand is whatever the
    distributor hand-wrote, NOT the resolved canonical manufacturer, so a
    pass-through column mustn't overwrite the unwrapped pipeline output
    column."""
    out = {col: "" for col in columns}
    raw = product.raw_input_cols or {}

    # --- pass-through raw input (verbatim; placeholder strings kept) ---
    # raw_input_cols holds the un-cleaned strings; fall back to "" when a
    # pre-change SQLite row lacks raw_input_cols (then everything in `raw`
    # is empty -- documented, accepted).
    out["Mfg_Part_Num"] = product.mfg_part_num or raw.get("Mfg_Part_Num", "")
    out["Part_Desc"] = product.part_desc or raw.get("Part_Desc", "")
    out["E1_Brand"] = raw.get("E1_Brand", "")
    out["Unilog_Brand"] = raw.get("Unilog_Brand", "")
    out["DIB_Brand"] = raw.get("DIB_Brand", "")
    out["Part_Manuf"] = product.part_manuf_raw or raw.get("Part_Manuf", "")

    # --- pipeline-resolved identity ---
    out["MANUFACTURER_NAME"] = product.manufacturer_name or ""
    out["BRAND_NAME"] = product.brand_name or ""
    out["MANUFACTURER_PART_NUMBER"] = product.mfg_part_num or ""
    out["Classpath"] = product.classpath or ""

    # --- source / evidence URLs ---
    out["MFR URL"] = product.mfr_url or ""
    for i, url in enumerate(product.ref_urls or [], start=1):
        col = f"Ref URL {i}"
        if col in out:
            out[col] = url

    # --- descriptions (Stage 5 output; never invented -- S5 returns "" / None
    #     without enrichment evidence, so this just writes what S5 produced) ---
    d = product.descriptions
    for col, field in DESC_FIELD_BY_COL.items():
        out[col] = getattr(d, field) or ""

    # --- ITEM_FEATURES_1..20 (fill-if-present; default-empty list no-ops) ---
    for i, feat in enumerate((d.item_features or [])[:MAX_ITEM_FEATURES]):
        out[f"ITEM_FEATURES_{i + 1}"] = feat or ""

    # --- attributes: ATTRIBUTE_LABEL n / VALUE n / UOM n ---
    # product.attributes is ordered by slot (1-indexed); emit label even
    # when value is empty (the slot-occupancy rule). Slots beyond the leaf
    # template's length stay "" -- do NOT invent labels for empty slots.
    for attr in (product.attributes or []):
        n = attr.slot
        if n < 1 or n > MAX_ATTRIBUTE_SLOTS:
            continue
        out[f"ATTRIBUTE_LABEL {n}"] = attr.label or ""
        out[f"ATTRIBUTE_VALUE {n}"] = attr.value or ""
        out[f"ATTRIBUTE_UOM {n}"] = attr.uom or ""

    return out


def build_failed_row_stub(raw_input: dict, columns: list[str]) -> dict:
    """A row whose pipeline product is missing still round-trips the raw
    input columns + a valid baseline row -- we do NOT invent a classpath
    or attributes. Same "never invent" rule as positive rows.

    `raw_input` is the original input row dict from the upload CSV (NOT
    raw_input_cols from EnrichedProduct -- this stub path means no product
    exists, so there is no EnrichedProduct to read from). Used only by the
    CLI script for rows whose product ended up None in the orchestrator's
    returned list; the API route uses SQLite-backed products (init_db +
    process_job's on_row_done persists every product incl. _error_product
    stubs), so missing-product rows on the API path still have an
    EnrichedProduct -- build_output_row handles them through raw_input_cols."""
    out = {col: "" for col in columns}
    for k in INPUT_COLS:
        if k in out:
            out[k] = raw_input.get(k, "")
    return out
