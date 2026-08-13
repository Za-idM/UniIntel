"""POST /api/process -- upload a CSV and run it through the pipeline
orchestrator (classify -> enrich -> extract -> reconcile) end-to-end.

Runs as a background task, not inline in the request: a real distributor
file pays for an LLM round-trip and an enrichment-fetch (with retries) per
row, which measured ~7s/row even at 4x concurrency in practice -- a
200-row file would hold the HTTP connection open for the better part of
half an hour if this stayed synchronous. The endpoint now returns as soon
as the job row exists; callers poll GET /api/job/{id} for
processed_rows/total_rows to show real progress instead of staring at a
single request that looks indistinguishable from a hang.
"""
import asyncio
import csv
import io
import uuid

from fastapi import APIRouter, HTTPException, UploadFile

from persistence.db import get_connection
from pipeline.orchestrator import process_job

router = APIRouter()

# asyncio.create_task() doesn't keep a task alive on its own -- the loop is
# free to garbage-collect it once nothing else references it, which can
# silently kill an in-flight job. Hold a reference until it finishes.
_background_tasks: set = set()


def _insert_job(conn, job_id: str, filename: str, total_rows: int) -> None:
    conn.execute(
        "INSERT INTO jobs (id, status, input_filename, total_rows, processed_rows) VALUES (?, 'RUNNING', ?, ?, 0)",
        (job_id, filename, total_rows),
    )
    conn.commit()


def _mark_progress(conn, job_id: str, processed_rows: int) -> None:
    conn.execute(
        "UPDATE jobs SET processed_rows = ?, updated_at = datetime('now') WHERE id = ?",
        (processed_rows, job_id),
    )
    conn.commit()


def _finish_job(conn, job_id: str, status: str, error: str | None = None) -> None:
    conn.execute(
        "UPDATE jobs SET status = ?, error = ?, updated_at = datetime('now') WHERE id = ?",
        (status, error, job_id),
    )
    conn.commit()


def _insert_product(conn, product) -> None:
    conn.execute(
        """INSERT INTO products
           (id, job_id, mfg_part_num, part_desc, part_manuf_raw, manufacturer_name,
            brand_name, classpath, mfr_url, confidence, confidence_band, data_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            product.product_id, product.job_id, product.mfg_part_num, product.part_desc,
            product.part_manuf_raw, product.manufacturer_name, product.brand_name,
            product.classpath, product.mfr_url, product.confidence, product.confidence_band,
            product.model_dump_json(),
        ),
    )
    conn.commit()


async def _run_job(job_id: str, rows: list[dict]) -> None:
    processed = 0

    async def on_row_done(product) -> None:
        nonlocal processed
        conn = get_connection()
        try:
            _insert_product(conn, product)
            processed += 1
            _mark_progress(conn, job_id, processed)
        finally:
            conn.close()

    conn = get_connection()
    try:
        try:
            await process_job(rows, job_id, on_row_done=on_row_done)
        except Exception as exc:
            _finish_job(conn, job_id, "FAILED", error=str(exc))
            return
        _finish_job(conn, job_id, "DONE")
    finally:
        conn.close()


@router.post("/api/process")
async def process(file: UploadFile):
    raw = await file.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="file must be UTF-8 encoded CSV")

    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows:
        raise HTTPException(status_code=400, detail="CSV has no data rows")

    job_id = str(uuid.uuid4())
    conn = get_connection()
    try:
        _insert_job(conn, job_id, file.filename, len(rows))
    finally:
        conn.close()

    task = asyncio.create_task(_run_job(job_id, rows))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    return {
        "job_id": job_id,
        "status": "RUNNING",
        "filename": file.filename,
        "total_rows": len(rows),
    }
