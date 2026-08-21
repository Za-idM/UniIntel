"""Regression test for GET /api/export/{job_id}'s resilience to a stored
product row that fails EnrichedProduct's strict schema validation.

Root cause this guards against: export.py previously called
EnrichedProduct.model_validate(data) per row INSIDE the CSV streaming
generator, AFTER the CSV header had already been sent. A single row whose
persisted data_json didn't cleanly validate against the current schema
(e.g. a nullable DB column -- products.part_desc is TEXT, no NOT NULL --
serialized as JSON null into a field the Pydantic model requires as a
plain str) raised mid-stream, abandoning the already-200'd connection.
Browsers surface that as a bare network error ("Failed to fetch"), not a
proper HTTP error status. GET /api/evaluate/{job_id} never hit this
because it only does a loose json.loads() with no schema validation.

This test seeds one good row and one row shaped exactly like that drift
scenario, then asserts the export still completes as a single 200 CSV
covering both rows, with the bad row backfilled/flagged rather than
aborting the stream.
"""
import csv
import io
import json
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

import persistence.db as db  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "export_test.db")
    db.init_db()

    from fastapi.testclient import TestClient
    from main import app

    with TestClient(app) as c:
        yield c


def _insert_job_and_products(job_id: str, rows: list[tuple[str, dict]]) -> None:
    conn = db.get_connection()
    try:
        conn.execute(
            "INSERT INTO jobs (id, status, total_rows, processed_rows) VALUES (?, 'DONE', ?, ?)",
            (job_id, len(rows), len(rows)),
        )
        for mfg_part_num, data in rows:
            conn.execute(
                "INSERT INTO products (id, job_id, mfg_part_num, part_desc, data_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), job_id, mfg_part_num, data.get("part_desc"), json.dumps(data)),
            )
        conn.commit()
    finally:
        conn.close()


def test_export_survives_a_row_that_fails_strict_validation(client):
    job_id = str(uuid.uuid4())

    good_product = {
        "product_id": str(uuid.uuid4()),
        "job_id": job_id,
        "mfg_part_num": "GOOD-001",
        "part_desc": "45W Led R20 Med 27k",
        "manufacturer_name": "Signify Holding",
        "classpath": "Electrical>Lamps & Lightings>Light Bulbs>LED Light Bulbs",
    }
    # Simulates real schema drift: products.part_desc is a nullable TEXT
    # column, but EnrichedProduct.part_desc is a required `str` -- an older
    # or corrupted row can carry a JSON null here.
    bad_product = {
        "product_id": str(uuid.uuid4()),
        "job_id": job_id,
        "mfg_part_num": "BAD-002",
        "part_desc": None,
    }

    _insert_job_and_products(job_id, [("GOOD-001", good_product), ("BAD-002", bad_product)])

    resp = client.get(f"/api/export/{job_id}")
    assert resp.status_code == 200

    reader = csv.DictReader(io.StringIO(resp.text))
    csv_rows = list(reader)
    assert len(csv_rows) == 2

    by_mpn = {r["Mfg_Part_Num"]: r for r in csv_rows}
    assert "GOOD-001" in by_mpn
    assert "BAD-002" in by_mpn
    assert by_mpn["GOOD-001"]["MANUFACTURER_NAME"] == "Signify Holding"


def test_export_survives_a_row_with_unparsable_data_json(client):
    job_id = str(uuid.uuid4())

    good_product = {
        "product_id": str(uuid.uuid4()),
        "job_id": job_id,
        "mfg_part_num": "GOOD-010",
        "part_desc": "Some product",
    }

    conn = db.get_connection()
    try:
        conn.execute(
            "INSERT INTO jobs (id, status, total_rows, processed_rows) VALUES (?, 'DONE', 2, 2)",
            (job_id,),
        )
        conn.execute(
            "INSERT INTO products (id, job_id, mfg_part_num, part_desc, data_json) VALUES (?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), job_id, "GOOD-010", "Some product", json.dumps(good_product)),
        )
        # Corrupted JSON blob -- must not crash the whole export either.
        conn.execute(
            "INSERT INTO products (id, job_id, mfg_part_num, part_desc, data_json) VALUES (?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), job_id, "CORRUPT-011", "n/a", "{not valid json"),
        )
        conn.commit()
    finally:
        conn.close()

    resp = client.get(f"/api/export/{job_id}")
    assert resp.status_code == 200

    reader = csv.DictReader(io.StringIO(resp.text))
    csv_rows = list(reader)
    assert len(csv_rows) == 2
    mpns = {r["Mfg_Part_Num"] for r in csv_rows}
    assert "GOOD-010" in mpns
    assert "CORRUPT-011" in mpns
