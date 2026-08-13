"""GET /api/export/{job_id} -- 252-col delivery CSV."""
from fastapi import APIRouter

router = APIRouter()


@router.get("/api/export/{job_id}")
async def export(job_id: str):
    # TODO(Step 7+): render products for job_id into the 252-col template
    # (data/reference/delivery_format_template.csv) and stream as CSV.
    return {"job_id": job_id, "status": "NOT_IMPLEMENTED"}
