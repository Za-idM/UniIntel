"""FastAPI app wiring the 7 locked API endpoints (Locked Decision #4)."""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api import process, jobs, products, evaluate, export, review
from config import CORS_ORIGINS
from persistence.db import init_db

app = FastAPI(title="UniIntel API")

# No auth on this API (Locked Decision #4) -- frontend runs on a separate
# origin (Next.js dev server / Vercel) from this backend (Railway), so
# without this every fetch() from the browser fails CORS preflight.
# CORS_ORIGINS (config.py) defaults to localhost:3000 for dev; set the
# CORS_ORIGINS env var to the real Vercel domain(s) (comma-separated) in
# production instead of leaving this wildcarded.
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(process.router)
app.include_router(jobs.router)
app.include_router(products.router)
app.include_router(evaluate.router)
app.include_router(export.router)
app.include_router(review.router)


@app.on_event("startup")
async def startup():
    init_db()


@app.get("/api/health")
async def health():
    return {"status": "ok"}
