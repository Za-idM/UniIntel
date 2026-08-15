"""Run the orchestrator over the 1000-row blind scale_input dataset
and write out a 252-column delivery-format CSV -- the actual submission
artefact for judging.

Output schema: data/reference/delivery_format_template.csv row 1 (252 cols).

Mapping decisions for the 252 cols (the columns not mentioned are emitted
EMPTY -- "never invent values" from CLAUDE.md applies; we do not fabricate
UPCs / weights / item features / spec sheet URLs we never fetched):

  * Pass-through (from the 6-col input row): Part_Desc, E1_Brand,
    Unilog_Brand, DIB_Brand, Part_Manuf -- these are the sponsor's raw
    input, so the submission must round-trip them verbatim.
  * Populated by the pipeline:
      MFR URL                 <- product.mfr_url
      Ref URL 1..5            <- product.ref_urls (left-padded with "")
      Mfg_Part_Num            <- product.mfg_part_num
      MANUFACTURER_NAME       <- product.manufacturer_name
      BRAND_NAME              <- product.brand_name
      MANUFACTURER_PART_NUMBER<- product.mfg_part_num (== GT's column 20)
      Classpath               <- product.classpath
      MOBILE_DESC/INVOICE_DESC/SHORT_DESC/LONG_DESC1/RETAIL_DESC/
      MARKETING_DESCRIPTION   <- product.descriptions.*
      ATTRIBUTE_LABEL n/VALUE n/UOM n  (n=1..50) <- product.attributes,
          slotted by AttributeValue.slot (1-indexed), empty for absent
          slots beyond the leaf template's length (e.g. LED has 27 slots,
          slots 28-50 stay blank); inside-template slots with no value
          still emit the LABEL in the right position (the
          "label occupies its slot, value stays empty" rule, CLAUDE.md).
  * Left empty (no source -- never invent):
      PART_NUMBER, Dept, Class, Fine, SKU - MY_PART_NUMBER  -- GT only:
      these are the 5 answer-leak cribs we explicitly DROPPED from eval
      input; the blind submission genuinely does not have them and the
      judges know it. Same principle as E.4.
      TRADE_NAME, ALTERNATE_PART_NUMBER -- not resolved by this build.
      ITEM_FEATURES_1..20 -- we don't extract these (no reliable source
      without Stage 3 fetch coverage); leave blank rather than fabricate.
      With, Standard/Approvals, Prop 65, Application, Includes,
      Product Name -- same reason.
      slots 28..50 of the attribute triplet block if the leaf template
      has fewer slots.
      UPC, EAN, GTIN, UNSPSC, Warranty, List Price, Selling Qty,
      Selling UOM, Standard Packaging Information, **_UOM (for dims),
      Product Image, Alternate Image *, SDS *, Warranty Information,
      Catalog, Specification Sheet, Instruction/Installation Manual,
      Service Manual, Owners/User Manual, Line Drawing, MTR, RoHS,
      Full Engineering Drawing, Energy Star Guide, Technical Bulletin,
      Submittal, Compatibility Chart, Size Chart, Product Label/Insert,
      Video Link *, Country Of Origin, Discontinued, Actual Image.
      These are all genuinely missing without enrichment reach; emitting
      "" is the contract, not a gap to paper over.

Running:
    python scripts/export_1000_submission.py
    python scripts/export_1000_submission.py --limit 50     # quick smoke
    python scripts/export_1000_submission.py --debug        # orchestrator DEBUG log

Output: data/output/submission_1000.csv (relative to uniintel/).
"""
import argparse
import asyncio
import csv
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from persistence.db import init_db  # noqa: E402
from persistence.llm_cache import hit_miss_summary, reset_hit_miss  # noqa: E402
from pipeline.orchestrator import process_job  # noqa: E402

INPUT_CSV = ROOT / "data" / "input" / "scale_input_1000.csv"
TEMPLATE_CSV = ROOT / "data" / "reference" / "delivery_format_template.csv"
OUT_DIR = ROOT / "data" / "output"

# The 6 columns the scale_input file has -- the ONLY input we get for the
# blind 1000-row submission. Same set evaluate_orchestrator_full.py uses
# after the E.4 crib-col drop.
INPUT_COLS = [
    "Mfg_Part_Num", "Part_Desc", "E1_Brand", "Unilog_Brand", "DIB_Brand", "Part_Manuf",
]

# Delivery columns populated by the orchestrator (everything else stays "").
DESC_FIELD_BY_COL = {
    "MOBILE_DESC": "mobile_desc",
    "INVOICE_DESC": "invoice_desc",
    "SHORT_DESC": "short_desc",
    "LONG_DESC1": "long_desc1",
    "RETAIL_DESC": "retail_desc",
    "MARKETING_DESCRIPTION": "marketing_description",
}

MAX_ATTRIBUTE_SLOTS = 50  # template reserves 50 triplets; leaf templates vary (LED=27)


def load_template_columns() -> list[str]:
    """Read row 1 of delivery_format_template.csv -> the 252-col header."""
    with open(TEMPLATE_CSV, encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        return next(reader)


def load_input_rows(limit: int | None = None) -> list[dict]:
    with open(INPUT_CSV, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    return rows[:limit] if limit else rows


def build_output_row(
    product,
    input_row: dict,
    columns: list[str],
) -> dict:
    """Map an EnrichedProduct + its raw input row -> a 252-key dict."""
    out = {col: "" for col in columns}

    # --- pass-through raw input (cols 11-16, 12..15 with 1-idx in header) ---
    out["Mfg_Part_Num"] = product.mfg_part_num or input_row.get("Mfg_Part_Num", "")
    out["Part_Desc"] = product.part_desc or input_row.get("Part_Desc", "")
    out["E1_Brand"] = input_row.get("E1_Brand", "")
    out["Unilog_Brand"] = input_row.get("Unilog_Brand", "")
    out["DIB_Brand"] = input_row.get("DIB_Brand", "")
    out["Part_Manuf"] = product.part_manuf_raw or input_row.get("Part_Manuf", "")

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


def write_csv(rows: list[dict], columns: list[str], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None,
                        help="only export the first N input rows (smoke test)")
    parser.add_argument("--debug", action="store_true",
                        help="enable orchestrator per-row DEBUG logging")
    parser.add_argument("--out", type=str, default=None,
                        help="output CSV path (default: data/output/submission_1000.csv)")
    args = parser.parse_args()

    if args.debug:
        logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(name)s: %(message)s")
    else:
        logging.basicConfig(level=logging.WARNING)

    init_db()  # idempotent; same guard evaluate_orchestrator_full.py has
    reset_hit_miss()

    columns = load_template_columns()
    input_rows = load_input_rows(args.limit)
    print(f"Loaded {len(input_rows)} input rows ({INPUT_CSV.name}); "
          f"output schema: {len(columns)} columns.")

    # The orchestrator needs the input dict's keys to match what its
    # process_row reads (Mfg_Part_Num / Part_Desc / Part_Manuf / brand
    # fields). scale_input_1000.csv already has exactly those keys +
    # the 3 brand cols; passing the raw dicts through is correct.
    products = asyncio.run(process_job(input_rows, job_id="submission-1000"))

    # Index products by MPN so we can match each input row to its result
    # (process_job can drop a row only on hard failure; with the init_db
    # guard in place, every input row should have an output product).
    products_by_mpn: dict[str, list] = {}
    for p in products:
        products_by_mpn.setdefault(p.mfg_part_num, []).append(p)

    out_rows: list[dict] = []
    rows_with_output = 0
    rows_missing_output = 0
    for input_row in input_rows:
        mpn = input_row.get("Mfg_Part_Num", "")
        bucket = products_by_mpn.get(mpn) or []
        product = bucket.pop(0) if bucket else None
        if product is None:
            rows_missing_output += 1
            # Still emit a row that round-trips the raw input so the
            # submission CSV has exactly len(input_rows) rows -- a missing
            # product is an enrichment failure, not a schema failure. The
            # row will have empty manufacturer/classpath/attributes -- per
            # "never invent", that's the honest output for a failed row.
            stub = out_stub_for_failed_input(input_row, columns)
            out_rows.append(stub)
            continue
        rows_with_output += 1
        out_rows.append(build_output_row(product, input_row, columns))

    out_path = Path(args.out) if args.out else (OUT_DIR / "submission_1000.csv")
    write_csv(out_rows, columns, out_path)

    hm = hit_miss_summary()
    hits, misses = hm.get("hits", 0), hm.get("misses", 0)
    total_calls = hits + misses
    ratio = (100 * hits / total_calls) if total_calls else 0.0

    print()
    print(f"Input rows:           {len(input_rows)}")
    print(f"Rows with pipeline output: {rows_with_output}")
    print(f"Rows with missing output (stubbed): {rows_missing_output}")
    print(f"Output columns:       {len(columns)}")
    print(f"Output CSV:           {out_path}")
    print(f"LLM cache hits/misses: {hits}/{misses} "
          f"(hit ratio: {ratio:.1f}% of {total_calls} calls)")


def out_stub_for_failed_input(input_row: dict, columns: list[str]) -> dict:
    """A row whose pipeline product is missing still emits the round-trip
    raw input columns + a valid Classpath-less/attribute-less row -- we
    do NOT invent a classpath or attributes. Same "never invent" rule."""
    out = {col: "" for col in columns}
    for k in INPUT_COLS:
        if k in out:
            out[k] = input_row.get(k, "")
    return out


if __name__ == "__main__":
    main()
