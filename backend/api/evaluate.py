"""GET /api/evaluate/{job_id} -- accuracy vs the 200-row ground truth."""
import json

from fastapi import APIRouter

from evaluation.ground_truth import by_mpn
from evaluation.metrics import evaluate_product, summarize
from persistence.db import get_connection

router = APIRouter()


@router.get("/api/evaluate/{job_id}")
async def evaluate(job_id: str):
    conn = get_connection()
    try:
        db_rows = conn.execute("SELECT * FROM products WHERE job_id = ?", (job_id,)).fetchall()
    finally:
        conn.close()

    gt_by_mpn = by_mpn()
    scored_rows = []
    unscored_count = 0

    for db_row in db_rows:
        product_data = json.loads(db_row["data_json"])
        gt_row = gt_by_mpn.get(db_row["mfg_part_num"])
        if gt_row is None:
            unscored_count += 1
            continue
        scored_rows.append(evaluate_product(product_data, gt_row))

    if not scored_rows:
        return {
            "job_id": job_id,
            "rows_total": len(db_rows),
            "rows_scored": 0,
            "rows_unscored": unscored_count,
            "summary": None,
            "rows": [],
            "note": "no products in this job matched a ground-truth Mfg_Part_Num",
        }

    return {
        "job_id": job_id,
        "rows_total": len(db_rows),
        "rows_unscored": unscored_count,
        "summary": summarize(scored_rows),
        "rows": scored_rows,
    }
