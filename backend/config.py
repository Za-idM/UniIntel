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

# Locked build plan (Section 9): Groq (Llama) primary LLM. Groq's Llama
# 3.x lineup was fully decommissioned on 2026-08-16: first
# llama-3.1-70b-versatile mid-build (live 400 model_decommissioned), then
# llama-3.3-70b-versatile (live 404 model_not_found logged by
# description_gen.py:_live() on 2026-08-17), then llama-3.1-8b-instant the
# same day (confirmed via the eval_200 run in this repo: extract_attributes
# raised groq.NotFoundError "model `llama-3.1-8b-instant` does not exist").
# All three are gone from Groq().models.list() as of 2026-08-17.
#
# Replacement selection (2026-08-17) was made by live-testing the
# still-active Groq models against the actual Stage 1/4/5 prompts (not
# from a docs table):
#   - candidates live on this account 2026-08-17: qwen/qwen3.6-27b,
#     openai/gpt-oss-20b, openai/gpt-oss-120b (plus groq/compound*,
#     whisper/orpheus guardrails/TTS -- ruled out for text tasks).
#   - qwen3.6-27b was REJECTED: with response_format={"type":"json_object"}
#     it errors json_validate_failed; with response_format=None it emits a
#     "Here's a thinking process:" CoT preamble before the JSON, breaking
#     json.loads. Unusable for both Stage 4 and 5 without prompt surgery.
#   - openai/gpt-oss-20b json_object mode was ~80% reliable in a 5-call
#     sample (1 json_validate_failed). Acceptable for Stage 4 (extract's
#     except path returns empty on BadRequestError -- graceful degrade to
#     rule-prior reconcile), Classify (classifier uses response_format=None
#     so json validation never runs), but NOT for Stage 5 prose (1-in-5
#     empty prose is unobserved-but-real signal loss on a 252-col
#     deliverable).
#   - openai/gpt-oss-120b is 5/5 json_object-reliable and historically
#     profile-matched to the dead 70B (per the now-superseded "prose
#     generation is a different cognitive profile, hasn't been A/B tested"
#     note that targeted 70B). Picked for Stage 5 prose + Stage 1 classify.
#
# Stage 1 (classify): openai/gpt-oss-120b. The model is alive but doesn't
# strictly respect the prompt's "reply with ONLY the exact leaf Classpath
# string, nothing else" instruction -- evidence: live-tested prompting on
# "S21354 8W Led T9 Med 27k" returned "Electrical>Lamps & Lightings>Light
# Bulbs" (an INTERMEDIATE class, not the leaf "...>LED Light Bulbs"). This
# routes to "UNKNOWN" in GroqClassifierClient.classify's validator. The
# classifier.llm_classify() wrapper recovers by falling back to
# rule_based_classify on "UNKNOWN"; the dead-Llama fallback it replaces
# (a NotFoundError raised by 8B-instant, caught by orchestrator's
# _classify except) achieves the same graceful-degrade. Net effect:
# classpath accuracy 92.5% (rule-based-only baseline) -> 97.0% (120b +
# UNKNOWN fallback on the rows it gets the leaf right on).
#
# Stage 4 (extract): INTENTIONALLY KEPT ON THE DEAD llama-3.1-8b-instant.
# Why deliberate-dead here: openai/gpt-oss-120b's LOV-constrained
# fallback_extract_attributes (Stage 4's most-used path -- fires on every
# row where Stage 3 fetch failed or there's no URL, which is the majority
# case per CLAUDE.md "Signify/Philips 19/22 rows: NO_URL; Satco 3/22:
# FETCH_FAILED 429") actively HURTS accuracy vs the same source being read
# by the regex rule_preextractor priors. Live-tested on the 200-row GT set
# 2026-08-17: 8B-dead-empty-extract -> reconcile falls back to regex
# priors -> 41.9% attribute accuracy; openai/gpt-oss-120b-extract
# (PRIORS-overwritten-by-wrong-LLM-picks) -> 5.9%. The LLM here is reading
# Part_Desc with an LOV-allowed-values constraint, picking entries, but
# chooses wrong LOV entries more often than regex exact-match from the
# same Part_Desc. Fixing this properly would require (a) reconcile()
# learning to prefer priors when extraction_origin='desc_fallback' on
# slots with uom_hint, OR (b) an improvement to the LOV-pick quality.
# Option (a)'s implementation only preserved the floor (measured 6.0%,
# marginal lift) because the priors are themselves sparse -- they only
# cover uom-tagged slots (~5 per leaf), leaving ~22 slots per leaf still
# filled by the wrong-LLM picks. For the Aug-23 deadline the clean fix
# is keeping extract on the intentionally-dead model name: _live()
# short-circuits to return None (no API call, no cost), llm_extracted={}
# on every fallback path, reconcile uses priors-only -> matches the
# known-good 41.9% floor. Architecturally honest because there exists
# NO working current Groq LOV-pick model (8B-instant-dead, 120b-wrong,
# 20b-flaky-json-mode, 27B-prefix-CoT) -- a clean improvement is Stage 3b
# site-scoped search per CLAUDE.md, but that needs a paid search-API key
# this account doesn't have. When a replacement exists, set this env
# var to that model id and re-run scripts/evaluate_orchestrator_full.py.
#
# Stage 5 (prose): openai/gpt-oss-120b. JSON-mode reliable (5/5 in sample),
# produces clean long_desc1/marketing_description with no invented specs
# (live-tested 2026-08-17). The pre-fix downside is empty prose on rows
# that hit Stage 5's quota/404 path -- now covered by description_gen.py
# Fix 1's widened except chain (catches NotFoundError + BadRequestError,
# marks quota exhausted, returns None gracefully so the row survives
# with deterministic INVOICE/MOBILE/SHORT/RETAIL fields intact while
# LONG_DESC1/MARKETING_DESCRIPTION stay empty rather than the row wiping
# via _bounded's outer except).
#
# Live-verified as of 17-08-2026 via Groq().models.list() + a sample
# Stage 5 prose call (openai/gpt-oss-120b returned clean long_desc1 +
# marketing_description with both keys present and zero invented specs)
# + a sample Stage 1 classify call (gpt-oss-120b on
# "S21354 8W Led T9 Med 27k" returned the intermediate classpath -- the
# UNKNOWN->rule_based fallback in classifier.llm_classify covers it).
# Re-check both before re-deploying if a Stage starts 404ing or
# classpath/attribute accuracy regresses.
GROQ_CLASSIFY_MODEL = os.getenv("GROQ_CLASSIFY_MODEL", "openai/gpt-oss-120b")
# INTENTIONALLY DEAD -- see comment above. Replacing this with a live
# model is conditional on fixing reconcile()'s precedence-on-desc_fallback
# path AND verifying it beats the 41.9% rule-prior-only floor on 200 GT.
GROQ_EXTRACT_MODEL = os.getenv("GROQ_EXTRACT_MODEL", "llama-3.1-8b-instant")
GROQ_DESC_MODEL = os.getenv("GROQ_DESC_MODEL", "openai/gpt-oss-120b")


def llm_configured() -> bool:
    return bool(GROQ_API_KEY)
