"""
Regression pin: one row's uncaught exception in process_job() must not
take the rest of the batch down with it. Before this fix,
asyncio.gather(...) (no return_exceptions=True) meant a single row's
exception propagated straight out of process_job(), and api/process.py's
_run_job() caught it and marked the WHOLE job FAILED regardless of how
many other rows had already succeeded or would have succeeded after.
"""
import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from pipeline import orchestrator  # noqa: E402
from schemas.product import EnrichedProduct  # noqa: E402


async def _fake_process_row(row, job_id, http_client=None, llm_client=None):
    if row["Mfg_Part_Num"] == "BAD":
        raise RuntimeError("simulated failure")
    return EnrichedProduct(product_id="p", job_id=job_id, mfg_part_num=row["Mfg_Part_Num"], part_desc="x")


def test_one_row_failure_does_not_kill_the_batch():
    rows = [
        {"Mfg_Part_Num": "GOOD1", "Part_Desc": "a"},
        {"Mfg_Part_Num": "BAD", "Part_Desc": "b"},
        {"Mfg_Part_Num": "GOOD2", "Part_Desc": "c"},
    ]
    done = []

    async def on_row_done(product):
        done.append(product.mfg_part_num)

    async def run():
        with patch.object(orchestrator, "process_row", _fake_process_row):
            return await orchestrator.process_job(rows, "job1", use_llm=False, on_row_done=on_row_done)

    products = asyncio.run(run())

    assert len(products) == 3
    assert done == ["GOOD1", "BAD", "GOOD2"], "on_row_done must still fire for every row, including the failed one"

    by_mpn = {p.mfg_part_num: p for p in products}
    assert by_mpn["GOOD1"].row_error is None
    assert by_mpn["GOOD2"].row_error is None
    assert by_mpn["BAD"].row_error is not None
    assert "simulated failure" in by_mpn["BAD"].row_error


if __name__ == "__main__":
    test_one_row_failure_does_not_kill_the_batch()
    print("All orchestrator regression checks passed.")
