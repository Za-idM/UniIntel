"""POST /api/review/{product_id} -- log a human correction. MVP: no auto-reingest (Locked Decision #8)."""
from fastapi import APIRouter
from pydantic import BaseModel
from persistence.db import get_connection

router = APIRouter()


class ReviewSubmission(BaseModel):
    field: str
    old_value: str | None = None
    new_value: str
    reviewer_note: str | None = None


@router.post("/api/review/{product_id}")
async def submit_review(product_id: str, submission: ReviewSubmission):
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO corrections (product_id, field, old_value, new_value, reviewer_note) "
            "VALUES (?, ?, ?, ?, ?)",
            (product_id, submission.field, submission.old_value, submission.new_value, submission.reviewer_note),
        )
        conn.commit()
    finally:
        conn.close()
    return {"product_id": product_id, "status": "LOGGED"}
