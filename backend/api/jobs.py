"""GET /api/job/{id} and GET /api/job/{id}/results"""
from fastapi import APIRouter, HTTPException
from persistence.db import get_connection

router = APIRouter()


@router.get("/api/job/{job_id}")
async def get_job(job_id: str):
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail="job not found")
    return dict(row)


@router.get("/api/job/{job_id}/results")
async def get_job_results(job_id: str):
    conn = get_connection()
    try:
        rows = conn.execute("SELECT * FROM products WHERE job_id = ?", (job_id,)).fetchall()
    finally:
        conn.close()
    return {"job_id": job_id, "count": len(rows), "products": [dict(r) for r in rows]}
