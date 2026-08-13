"""GET /api/product/{id}"""
import json
from fastapi import APIRouter, HTTPException
from persistence.db import get_connection

router = APIRouter()


@router.get("/api/product/{product_id}")
async def get_product(product_id: str):
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail="product not found")
    product = dict(row)
    product["data"] = json.loads(product.pop("data_json"))
    return product
