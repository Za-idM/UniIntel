"""SQLite-backed LLM response cache.

Why this exists (the problem): Groq's free tier caps each model at 100K
tokens-per-day (org-level, not per-key -- swapping keys doesn't help).
A single 200-row eval burns ~600-1000 fresh tokens/row into that ceiling,
hitting the wall mid-batch. The result was today's attribute accuracy
dropping from 44.5% (clean 50-row run) to 16.9% (full 200-row run after
the wall hit), with no way to re-run for free. Every subsequent dev
iteration faces the same wall the next morning -- thoroughly blocking
accurate iteration.

What it does (the fix): store every Groq completion in the existing
data/uniintel.db under the llm_cache table, keyed by (namespace,
prompt_hash, model). On a re-run of the same prompt+model, return the
cached completion without spending a token. Cache-hit ratio gets
printed by the eval harness so "iteration is now free" is verifiable, not
hoped.

Why SQLite not JSON (vs teammate's design we reviewed): her JSON file
rewrites the whole file on every set() -- O(n) write cost with O(n)
locked-write contention under CONCURRENCY=4. SQLite in WAL mode is
atomic, write-cheap, queryable, and lives in the existing DB so we don't
introduce a second persistent store. One less moving part to ship.

Why `model` is in the key (the bug the JSON design had): her cache key
was (namespace, key) -- so an 8B swap would serve stale 70B outputs as
fresh 8B outputs, silently wrong. A/B testing models on the same prompt
requires the cache to separate them; this is non-negotiable. The same
prompt fed to llama-3.1-8b-instant vs llama-3.3-70b-versatile gets two
cache rows, as it should.

What this is NOT: not a token-aware budgeter (we don't predict ahead),
not a row-level attribution layer (tokens_used is logged for the
dashboard, not joined to products yet), and not a TTL'd store (no LRU,
no eviction -- the cache grows until manually cleared, which is fine
because it's structured data rarely larger than ~10MB even at thousands
of rows).
"""
import hashlib
import json
import logging
import sqlite3
import threading
from typing import Any, Callable, Awaitable

from persistence.db import get_connection

logger = logging.getLogger(__name__)


# --- connected-write coordination --------------------------------------------
# SQLite in WAL mode allows concurrent readers but only one writer at a
# time -- so writes serialize at the connection level. We holds the
# connection across the cached_call async flow (the cache lookup + the
# live call + the cache write) under a single threading.Lock so the
# CONCURRENCY=4 row workers can't open 4 simultaneous write-cursors and
# trigger "database is locked" exceptions. Reads remain lock-free (a
# second per-call connection).
_write_lock = threading.Lock()


def _hash_prompt(
    system_prompt: str,
    user_prompt: str,
    temperature: float,
    max_tokens: int,
    response_format: str | None,
    model: str,
) -> str:
    """Deterministic SHA256 of every input that materially affects the
    completion. include temperature / max_tokens / response_format because
    changing any of them changes the output (a temperature=0 call and a
    temperature=0.4 call on the same prompt are NOT the same call) and
    the cache must NOT silently serve one for the other. Model is part of
    the PRIMARY KEY, but including it in the hash too means an accidental
    query without the model column still wouldn't cross-serve models."""
    h = hashlib.sha256()
    h.update(f"{model}|".encode())
    h.update(f"{system_prompt}\x00".encode())
    h.update(f"{user_prompt}\x00".encode())
    h.update(f"temp={temperature}|".encode())
    h.update(f"max_tokens={max_tokens}|".encode())
    h.update(f"response_format={response_format}".encode())
    return h.hexdigest()


# --- public API: get / set / stats ------------------------------------------


def get(namespace: str, prompt_hash: str, model: str) -> str | None:
    """Return the cached raw completion, or None on miss. Read-only, no
    lock needed (WAL mode permits concurrent reads)."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT response FROM llm_cache WHERE namespace=? AND prompt_hash=? AND model=?",
            (namespace, prompt_hash, model),
        ).fetchone()
        return row["response"] if row else None
    finally:
        conn.close()


def set_(namespace: str, prompt_hash: str, model: str, response: str, tokens_used: int | None = None) -> None:
    """Insert (or REPLACE on the rare key collision -- happens if the
    same prompt was already cached earlier, which is safe to overwrite
    since temperature=0 completions are deterministic). Holding the
    _write_lock prevents concurrent-write contention under high
    CONCURRENCY."""
    with _write_lock:
        conn = get_connection()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO llm_cache "
                "(namespace, prompt_hash, model, response, tokens_used) "
                "VALUES (?, ?, ?, ?, ?)",
                (namespace, prompt_hash, model, response, tokens_used),
            )
            conn.commit()
        finally:
            conn.close()


def find_by_substring(namespace: str, substring: str) -> tuple[str, str] | None:
    """Recover cached content when the ORIGINAL key (typically a URL) can
    no longer be re-fetched but a prior successful call was stored.
    Searches the prompt_hash column for one containing the substring --
    since URL keys frequently end up embedded in the prompt itself, a
    match on `substring` against any cached prompt_hash suggests the
    prior extraction. Used by the enrichment layer when the live URL is
    bot-blocked but a stale cached extraction is recoverable and would
    beat a desc_fallback empty guess. Adapted from teammate's design."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT prompt_hash, response FROM llm_cache "
            "WHERE namespace=? AND prompt_hash LIKE ?",
            (namespace, f"%{substring}%"),
        ).fetchall()
        return (rows[0]["prompt_hash"], rows[0]["response"]) if rows else None
    finally:
        conn.close()


def stats() -> dict[str, int]:
    """Per-namespace cache size, for the dashboard / eval-harness
    'cache-hit ratio' line."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT namespace, COUNT(*) AS n FROM llm_cache GROUP BY namespace"
        ).fetchall()
        return {r["namespace"]: r["n"] for r in rows} or {}
    finally:
        conn.close()


def size() -> int:
    conn = get_connection()
    try:
        row = conn.execute("SELECT COUNT(*) AS n FROM llm_cache").fetchone()
        return row["n"] if row else 0
    finally:
        conn.close()


# --- the live-call wrapper ---------------------------------------------------
# Single async entrypoint all 4 Groq call sites use. Inlined in extractor.py
# / description_gen.py / llm_client.py as `await cached_call(...)`; not
# exported as a free function because the live-call shape differs per site
# (client construction, pacer, retries). Each site's helper fn below shows
# the pattern -- callers should adopt the same shape.

_hit_count = 0
_miss_count = 0


def _record_hit() -> None:
    global _hit_count
    _hit_count += 1


def _record_miss() -> None:
    global _miss_count
    _miss_count += 1


def hit_miss_summary() -> dict[str, int]:
    return {"hits": _hit_count, "misses": _miss_count}


def reset_hit_miss() -> None:
    global _hit_count, _miss_count
    _hit_count = 0
    _miss_count = 0


async def cached_call(
    namespace: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    *,
    temperature: float,
    max_tokens: int,
    response_format: str | None,
    live_fn: Callable[..., Awaitable[str | None]],
    tokens_used_fn: Callable[[str], int] | None = None,
) -> str | None:
    """The wrapper every LLM call site should use.

    `live_fn` is the actual async call to Groq for this site, already
    wired with pacing/retries/quota-exhaustion handling. Returns either
    the completion content (str) or None on hard failure. cached_call:
      1. hash inputs -> prompt_hash
      2. look up (namespace, prompt_hash, model) in llm_cache
      3. on hit: record the hit, return the cached completion (zero
         API cost, zero pacer wait)
      4. on miss: record the miss, await live_fn(), write the response
         to cache (under the write-lock so concurrent writers don't
         collide), return the response

    `tokens_used_fn(response)` if provided, lets the caller compute the
    token cost after the fact for the dashboard. Groq exposes this in the
    `response.usage` field but not on the cached replay path, so we
    record it only on the live-cold path. None means 'unknown' for the
    dashboard, which is fine.

    Note: this is intentionally async even though the cache lookup is
    sync -- the live_fn is async and that's the path that dominates cost.
    """
    prompt_hash = _hash_prompt(system_prompt, user_prompt, temperature, max_tokens, response_format, model)

    cached = get(namespace, prompt_hash, model)
    if cached is not None:
        _record_hit()
        logger.debug("cache HIT ns=%s model=%s hash=%s", namespace, model, prompt_hash[:12])
        return cached

    _record_miss()
    live_response = await live_fn()
    if live_response is not None:
        tokens = tokens_used_fn(live_response) if tokens_used_fn else None
        set_(namespace, prompt_hash, model, live_response, tokens)
    return live_response
