-- SQLite schema for UniIntel. Locked build plan tables: jobs, job_stages,
-- products, attributes, corrections, audit_logs.

CREATE TABLE IF NOT EXISTS jobs (
    id              TEXT PRIMARY KEY,
    status          TEXT NOT NULL DEFAULT 'PENDING',  -- PENDING, RUNNING, DONE, FAILED
    input_filename  TEXT,
    total_rows      INTEGER,
    processed_rows  INTEGER NOT NULL DEFAULT 0,
    error           TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS job_stages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id          TEXT NOT NULL REFERENCES jobs(id),
    product_id      TEXT NOT NULL,
    stage           TEXT NOT NULL,   -- clean, resolve, classify, enrich, extract, describe, validate
    status          TEXT NOT NULL DEFAULT 'PENDING',
    error           TEXT,
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(job_id, product_id, stage)
);

CREATE TABLE IF NOT EXISTS products (
    id              TEXT PRIMARY KEY,
    job_id          TEXT NOT NULL REFERENCES jobs(id),
    mfg_part_num    TEXT NOT NULL,
    part_desc       TEXT,
    part_manuf_raw  TEXT,
    manufacturer_name TEXT,
    brand_name      TEXT,
    classpath       TEXT,
    mfr_url         TEXT,
    confidence      REAL,
    confidence_band TEXT,
    data_json       TEXT NOT NULL,   -- full EnrichedProduct JSON
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_products_job ON products(job_id);
CREATE INDEX IF NOT EXISTS idx_products_mpn_mfr ON products(mfg_part_num, manufacturer_name);

CREATE TABLE IF NOT EXISTS attributes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id      TEXT NOT NULL REFERENCES products(id),
    slot            INTEGER NOT NULL,
    label           TEXT NOT NULL,
    value           TEXT,
    uom             TEXT,
    source_url      TEXT,
    evidence_text   TEXT,
    lov_valid       INTEGER,
    confidence      REAL,
    UNIQUE(product_id, slot)
);

-- MVP feedback loop: log reviews, no auto-reingest (Locked Decision #8)
CREATE TABLE IF NOT EXISTS corrections (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id      TEXT NOT NULL REFERENCES products(id),
    field           TEXT NOT NULL,
    old_value       TEXT,
    new_value       TEXT,
    reviewer_note   TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id          TEXT,
    product_id      TEXT,
    event           TEXT NOT NULL,
    detail_json     TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Phase E: LLM response cache. Stores every Groq completion so re-runs of
-- the same (prompt, model) pair return from cache instead of re-spending
-- the limited Groq free-tier daily quota (100K TPD on both 70B and 8B
-- pools). Without this, every dev iteration on the 200-row eval burns
-- ~600-1000 fresh tokens/row into the same daily ceiling and hits the
-- wall mid-batch -- the exact wall that capped attribute accuracy at
-- 16.9% today despite the 8B swap showing 44.5% on a clean 50-row run.
--
-- Key design notes:
--   - `model` is part of the key (NOT just `namespace, prompt_hash`):
--     an 8B-swap without this column would serve stale 70B outputs as if
--     they were 8B outputs -- silently wrong -- and the whole point of
--     A/B testing models is to compare outputs ON THE SAME PROMPT, which
--     the cache MUST separate. Phase F's ab_8b_vs_70b.py depends on this.
--   - `prompt_hash` is the SHA256 of (system_prompt + user_prompt +
--     temperature + max_tokens + response_format). Temperature and
--     response_format ARE part of the hash because they materially
--     change the output (a 0.0 vs 0.4 call on the same prompt produces
--     different text). max_tokens is included for the same reason.
--   - `namespace` separates reasoning pools: classify / extract_evidence
--     / extract_fallback / prose / pdf_extract. A hit in one must never
--     be served as a hit in another, even on identical prompts.
--   - `tokens_used` for the dashboard's per-row "tokens spent" view
--     (join with products on the future; not wired to a row yet -- the
--     30K cap makes exact attribution per call cheap to add later).
--   - `hits` + `misses` Summary view at the end of a cached_call run
--     lets the eval harness print "cache hit ratio: 87%" -- the single
--     number that tells us whether the iteration loop is now free.
--
-- Substring recovery (cache.find_by_substring) is a query, not a schema
-- feature: when the original URL key is no longer fetchable (e.g. Satco
-- bot-blocks again), we look for cached extractions whose URL key
-- contains the MPN substring and recover the prior extraction. Adapted
-- from teammate Deepthit's namespace+substring pattern (GLM prompt,
-- recommendation #3), with the model-aware key column added.
CREATE TABLE IF NOT EXISTS llm_cache (
    namespace       TEXT NOT NULL,       -- classify | extract_evidence | extract_fallback | prose | pdf_extract
    prompt_hash     TEXT NOT NULL,       -- sha256(system + user + temp + max_tokens + response_format)
    model           TEXT NOT NULL,       -- llama-3.1-8b-instant | llama-3.3-70b-versatile | ...
    response        TEXT,                -- raw completion (parsed by caller; cache is opaque to format)
    tokens_used     INTEGER,             -- for dashboard token-accounting; NULL when unknown
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (namespace, prompt_hash, model)
);

CREATE INDEX IF NOT EXISTS idx_llm_cache_lookup ON llm_cache(namespace, prompt_hash, model);
