"""Loads .env (if present) and exposes settings. No secrets are hardcoded
or logged here -- values come from environment/.env only."""
import os
from pathlib import Path

from dotenv import load_dotenv

ENV_PATH = Path(__file__).resolve().parent / ".env"
load_dotenv(ENV_PATH)

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Comma-separated list of allowed browser origins for CORS (main.py). Defaults
# to the Next.js dev server only -- deployed environments (Railway/Render env
# vars) must set this to the real Vercel domain(s), e.g.
# "https://uniintel.vercel.app,https://uniintel-git-main-yourteam.vercel.app".
# No wildcard default: an enrichment API that accepts uploads and returns
# scraped page content is not something to leave open to every origin in
# production.
CORS_ORIGINS = [o.strip() for o in os.getenv("CORS_ORIGINS", "http://localhost:3000").split(",") if o.strip()]

# SQLite file path. Defaults to <repo_root>/data/uniintel.db for local dev.
# On Railway/Render's default ephemeral filesystem this resets on every
# deploy/restart -- see persistence/db.py's module docstring for the
# persistent-volume requirement this implies for production.
DB_PATH = os.getenv("DB_PATH") or str(
    Path(__file__).resolve().parent.parent / "data" / "uniintel.db"
)

# Locked build plan (Section 9): Groq (Llama) primary LLM. llama-3.1-70b-versatile
# was decommissioned by Groq mid-build (confirmed via a live 400
# model_decommissioned error) -- llama-3.3-70b-versatile is the current
# equivalent per `Groq().models.list()`. Re-check that listing if this
# breaks again; Groq deprecates models on their own schedule.
#
# GROQ_EXTRACT_MODEL: swapped to llama-3.1-8b-instant on 2026-08-15 based on
# the ab_8b_vs_70b.py A/B result -- 8B scored 45.2% attribute accuracy on a
# clean 30-row run vs 70B's 1.1% (admittedly quota-throttled mid-run, but
# every historical 70B number in this project was similarly throttled and
# never came close to 8B's figure). 8B is on a separate quota pool so it
# sidesteps the shared 100K TPD org-level wall the 70B model keeps hitting.
# For the constrained-select extraction task (LOV allowed-values + JSON
# object output) 8B's tighter context appears to help, not hurt. JSON-mode
# reliably honored -- no parse failures observed in the A/B. The 70B model
# is NOT deleted: callers can still override via the GROQ_EXTRACT_MODEL env
# var to A/B test, and descriptions (GROQ_DESC_MODEL below) still default
# to 70B because prose generation is a different cognitive profile and
# hasn't been A/B tested yet.
GROQ_CLASSIFY_MODEL = os.getenv("GROQ_CLASSIFY_MODEL", "llama-3.1-8b-instant")
GROQ_EXTRACT_MODEL = os.getenv("GROQ_EXTRACT_MODEL", "llama-3.1-8b-instant")
GROQ_DESC_MODEL = os.getenv("GROQ_DESC_MODEL", "llama-3.3-70b-versatile")


def llm_configured() -> bool:
    return bool(GROQ_API_KEY)
