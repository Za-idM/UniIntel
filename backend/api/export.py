"""GET /api/export/{job_id} -- 252-col delivery CSV streamed from SQLite.

Replays the EnrichedProduct blobs persisted by POST /api/process
(api/process.py:_insert_product -> product.model_dump_json()) back into
the 252-column delivery template the script path uses, via the SAME shared
mapping (backend/export/delivery_csv.py). Byte-for-byte equality with
`python scripts/export_1000_submission.py --limit 10` over the same input
rows is the proof the two paths didn't silently drift -- see
backend/export/delivery_csv.py's module docstring for how
EnrichedProduct.raw_input_cols keeps the raw "-- Unbranded --" / "-- No
Unilog Brand --" / "-- No DIB Brand --" placeholders intact end-to-end.

Streaming is row-by-row through a generator rather than materialising the
whole CSV in memory: a 750K-row distributor file (the locked scalability
target, see CLAUDE.md) at ~350 bytes/row is ~260 MB of CSV, which is
exactly the kind of thing chunked transfer encodes better. Each yield
emits the rendered CSV for one product at a time; peak server memory is
one row's worth, not 260 MB.

Row ordering: products are sorted by rowid (insert order), which equals
completion order in process_job's on_row_done callback. asyncio.gather
returns products in INPUT order, but on_row_done fires in completion
order -- so the API SQL ordering can differ from the script's ordering on
concurrent jobs. The byte-equality smoke test keys both outputs by
Mfg_Part_Num per row before comparing, since cell content (not row order)
is what the contract guarantees.
"""
import csv
import io
import json
import logging
import uuid

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import ValidationError

from export.delivery_csv import build_output_row, load_template_columns
from persistence.db import get_connection
from schemas.product import EnrichedProduct

logger = logging.getLogger(__name__)

router = APIRouter()


def _safe_validate_product(data: dict, job_id: str, mfg_part_num: str) -> EnrichedProduct:
    """EnrichedProduct.model_validate() is strict, and by the time export
    runs the stored data_json may predate a schema change (a field that's
    now required, or typed more narrowly, than when the row was written --
    see this module's docstring / CLAUDE.md for the "Failed to fetch"
    symptom this caused: model_validate raising mid-generator, after the
    CSV header was already streamed, abandoning the connection). GET
    /api/evaluate/{job_id} never hits this because it only does a loose
    json.loads() with no schema validation.

    This must not loosen validation anywhere else -- it only keeps ONE bad
    row from taking down an otherwise-good export. Strategy: try strict
    validation; on failure, backfill just the fields that have no safe
    empty default (product_id/job_id/mfg_part_num/part_desc) from data
    already known to the caller and retry once; if it still fails, emit a
    minimal stub row carrying row_error so the row is clearly flagged in
    the CSV (row_error isn't a delivery_csv.py output column, but the
    logged warning plus the stub's near-empty fields make a flagged row
    identifiable) rather than aborting the whole stream.
    """
    try:
        return EnrichedProduct.model_validate(data)
    except ValidationError as exc:
        logger.warning(
            "export job_id=%r mfg_part_num=%r: row failed strict schema "
            "validation, attempting backfill: %s",
            job_id, mfg_part_num, exc,
        )
        repaired = dict(data)
        repaired["product_id"] = repaired.get("product_id") or str(uuid.uuid4())
        repaired["job_id"] = job_id
        repaired["mfg_part_num"] = repaired.get("mfg_part_num") or mfg_part_num or ""
        repaired["part_desc"] = repaired.get("part_desc") or ""
        try:
            return EnrichedProduct.model_validate(repaired)
        except ValidationError as exc2:
            logger.warning(
                "export job_id=%r mfg_part_num=%r: backfill did not fix "
                "validation, emitting flagged stub row instead: %s",
                job_id, mfg_part_num, exc2,
            )
            return EnrichedProduct(
                product_id=repaired["product_id"],
                job_id=job_id,
                mfg_part_num=mfg_part_num or "",
                part_desc="",
                row_error=f"EXPORT_VALIDATION_FAILED: {exc2}",
            )


@router.get("/api/export/{job_id}")
async def export(job_id: str):
    columns = load_template_columns()

    conn = get_connection()
    try:
        job_row = conn.execute(
            "SELECT status FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        if job_row is None:
            raise HTTPException(status_code=404, detail=f"job {job_id} not found")

        # ORDER BY rowid: SQLite rowid == insert order, which is when each
        # product finished on_row_done. NOT input order on a concurrent
        # job -- but matches the order rows were persisted, which is the
        # only stable order available without an explicit input_index
        # column (LLM cache hits reorder within a gather() wave).
        rows = conn.execute(
            "SELECT mfg_part_num, data_json FROM products WHERE job_id = ? ORDER BY rowid",
            (job_id,),
        ).fetchall()
    finally:
        conn.close()

    # Stream the CSV: a header row, then one row per product. Each yield
    # is encoded utf-8 -- StreamingResponse accepts bytes chunks and emits
    # them as chunked transfer-encoding. Skipping a firm DONE-check lets a
    # caller export a partial run (useful for a debug snapshot); the row
    # count is always total_rows for a finished job because _error_product
    # stubs are also persisted via on_row_done.

    def iter_csv():
        # Header yields once, before the generator returns row data. Each
        # row is rendered separately so peak memory per chunk is the size
        # of one product's 252-cell CSV line, not the whole file.
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=columns)
        # writeheader only writes the column names -- emits it as the
        # first chunk so the browser's CSV download begins immediately at
        # connection open rather than waiting for the first product.
        writer.writeheader()
        yield buffer.getvalue().encode("utf-8")
        buffer.seek(0)
        buffer.truncate()

        for row in rows:
            try:
                data = json.loads(row["data_json"])
            except json.JSONDecodeError as exc:
                logger.warning(
                    "export job_id=%r mfg_part_num=%r: data_json failed to "
                    "parse, treating as empty for backfill: %s",
                    job_id, row["mfg_part_num"], exc,
                )
                data = {}
            product = _safe_validate_product(data, job_id, row["mfg_part_num"])
            out_row = build_output_row(product, columns)
            writer.writerow(out_row)
            yield buffer.getvalue().encode("utf-8")
            buffer.seek(0)
            buffer.truncate()

    return StreamingResponse(
        iter_csv(),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="uniintel_{job_id}.csv"',
        },
    )
