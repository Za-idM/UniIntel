"""Drive the live API for the byte-equality smoke test.

Uploads data/output/smoke10_input.csv (the first 10 rows of
scale_input_1000.csv) to POST /api/process running on the already-
started uvicorn (port 8765), polls GET /api/job/{id} until DONE/FAILED,
then downloads GET /api/export/{job_id} and saves the streamed CSV to
data/output/api_smoke10.csv for the byte-equality comparison."""
import asyncio
import pathlib
import sys

import httpx

ROOT = pathlib.Path(__file__).resolve().parent.parent
PORT = 8765
INPUT = ROOT / "data" / "output" / "smoke10_input.csv"
OUT_CSV = ROOT / "data" / "output" / "api_smoke10.csv"
OUT_JOBID = ROOT / "data" / "output" / "api_smoke10_jobid.txt"


async def main() -> None:
    INPUT.open("rb").close()  # assert exists
    with INPUT.open("rb") as f:
        files = {"file": ("smoke10_input.csv", f.read(), "text/csv")}
    async with httpx.AsyncClient(base_url=f"http://localhost:{PORT}", timeout=60.0) as c:
        r = await c.post("/api/process", files=files)
        print(f"POST /api/process: {r.status_code} {r.text[:200]}")
        r.raise_for_status()
        data = r.json()
        job_id = data["job_id"]
        sd = {}
        for i in range(200):
            await asyncio.sleep(3)
            sr = await c.get(f"/api/job/{job_id}")
            sd = sr.json()
            print(f"  poll {i}: status={sd['status']} "
                  f"{sd['processed_rows']}/{sd['total_rows']}"
                  + (f" err={sd.get('error')}" if sd['status'] == 'FAILED' else ''))
            if sd["status"] in ("DONE", "FAILED"):
                break
        OUT_JOBID.write_text(job_id, encoding="utf-8")
        print(f"FINAL job_id={job_id} status={sd['status']}")

        er = await c.get(f"/api/export/{job_id}")
        print(f"GET /api/export: {er.status_code} "
              f"content-type={er.headers.get('content-type')} "
              f"bytes={len(er.content)}")
        er.raise_for_status()
        OUT_CSV.write_bytes(er.content)
        print(f"Saved {OUT_CSV}")


if __name__ == "__main__":
    asyncio.run(main())
