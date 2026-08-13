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
