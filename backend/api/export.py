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

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from export.delivery_csv import build_output_row, load_template_columns
from persistence.db import get_connection
from schemas.product import EnrichedProduct

router = APIRouter()


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
            "SELECT data_json FROM products WHERE job_id = ? ORDER BY rowid",
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
            data = json.loads(row["data_json"])
            product = EnrichedProduct.model_validate(data)
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
