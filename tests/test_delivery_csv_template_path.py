"""Regression test for load_template_columns()'s deploy-root path resolution.

Root cause this guards against: TEMPLATE_CSV in backend/export/delivery_csv.py
was computed with 3 .parent hops (repo-root-relative), but Railway's Root
Directory is set to backend/ -- only backend/ is deployed as the container's
app root. A 3rd .parent hop from backend/export/delivery_csv.py resolves one
level ABOVE that root (e.g. "/"), producing the live
FileNotFoundError('/data/reference/delivery_format_template.csv') seen on
GET /api/export/{job_id}. Same bug class as 1f06cd8's data/bootstrap fix.

Fix: 2 .parent hops (matching every other backend module's data/ convention)
+ a copy of the template mirrored into backend/data/reference/, and a clear
RuntimeError (instead of a raw FileNotFoundError reaching a live judge/demo)
if the file is ever missing again.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

import pytest  # noqa: E402

from export.delivery_csv import TEMPLATE_CSV, load_template_columns  # noqa: E402


def test_template_csv_resolves_under_backend_data_reference():
    # backend/'s own data/reference/ -- NOT the repo-root data/reference/ --
    # is what actually ships to Railway (Root Directory = backend/).
    assert TEMPLATE_CSV == ROOT / "backend" / "data" / "reference" / "delivery_format_template.csv"


def test_template_csv_file_exists_and_loads_252_columns():
    assert TEMPLATE_CSV.exists(), (
        f"{TEMPLATE_CSV} is missing -- backend/data/reference/ must mirror "
        "repo-root data/reference/ the same way backend/data/bootstrap/ and "
        "backend/data/ground_truth/ already do, since only backend/ deploys."
    )
    columns = load_template_columns()
    assert len(columns) == 252


def test_missing_template_raises_clear_runtime_error_not_bare_filenotfound(monkeypatch):
    import export.delivery_csv as delivery_csv

    monkeypatch.setattr(delivery_csv, "TEMPLATE_CSV", ROOT / "backend" / "data" / "reference" / "does_not_exist.csv")

    with pytest.raises(RuntimeError, match="Delivery template CSV not found"):
        delivery_csv.load_template_columns()
