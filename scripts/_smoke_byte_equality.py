"""Byte-equality smoke test: compare script_path CSV vs API_path CSV.

Both `scripts/export_1000_submission.py --limit 10` and the live API
(POST /api/process -> GET /api/export/{job_id}) funnel through
backend/export/delivery_csv.py, so cell contents must match exactly.

Row ordering MAY differ: the script preserves input order in its out_rows
list (gather returns in input order), but the API path persists each
product via on_row_done in asyncio.gather completion order, then exports
ordered by SQLite rowid == insert order. With concurrency 4 and LLM cache
hits varying row latency, completion order can diverge from input order
on the very same 10 rows. The comparison is therefore keyed on
Mfg_Part_Num: for each MPN, fetch the matching row from each CSV and
compare every one of the 252 cells. A row-content mismatch is a real
drift; a row-order difference is the documented completion-order artifact
and is NOT a failure (the CSV row set is still identical as a set).

Reports:
  - Same 252 column headers, same order.
  - Same row count.
  - For each MPN, the number of differing cells (0 == byte-equal row).
  - Explicit PASS/FAIL verdict.
"""
import csv
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT_CSV = ROOT / "data" / "output" / "script_smoke10.csv"
API_CSV = ROOT / "data" / "output" / "api_smoke10.csv"


def load_rows(path: pathlib.Path) -> tuple[list[str], list[dict]]:
    with path.open(encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        cols = reader.fieldnames or []
        rows = list(reader)
    return cols, rows


def main() -> int:
    script_cols, script_rows = load_rows(SCRIPT_CSV)
    api_cols, api_rows = load_rows(API_CSV)

    print(f"script: {len(script_rows)} rows, {len(script_cols)} cols, "
          f"{SCRIPT_CSV.name}, {SCRIPT_CSV.stat().st_size} bytes")
    print(f"api   : {len(api_rows)} rows, {len(api_cols)} cols, "
          f"{API_CSV.name}, {API_CSV.stat().st_size} bytes")

    failures: list[str] = []

    # 1. Header equality (same cols, same order).
    if script_cols != api_cols:
        failures.append("Column header mismatch")
        only_script = [c for c in script_cols if c not in api_cols]
        only_api = [c for c in api_cols if c not in script_cols]
        if only_script:
            failures.append(f"  only in script: {only_script[:5]}...")
        if only_api:
            failures.append(f"  only in api   : {only_api[:5]}...")
        # Order diff
        diffs = [(i, s, a) for i, (s, a) in enumerate(zip(script_cols, api_cols)) if s != a]
        if diffs:
            failures.append(f"  first order diffs: {diffs[:5]}")
    else:
        print(f"[OK] headers identical: {len(script_cols)} cols, same order")

    # 2. Row count.
    if len(script_rows) != len(api_rows):
        failures.append(f"Row count differs: script={len(script_rows)} api={len(api_rows)}")
    else:
        print(f"[OK] row count identical: {len(script_rows)} rows")

    # 3. Cell-by-cell, keyed on Mfg_Part_Num (row order may differ).
    def index_by_mpn(rows):
        out: dict[str, list[dict]] = {}
        for r in rows:
            out.setdefault(r.get("Mfg_Part_Num", ""), []).append(r)
        return out

    script_by_mpn = index_by_mpn(script_rows)
    api_by_mpn = index_by_mpn(api_rows)

    mpn_diff = set(script_by_mpn) ^ set(api_by_mpn)
    if mpn_diff:
        failures.append(f"MPN set differs: only-script={sorted(m for m in mpn_diff & set(script_by_mpn))[:5]} "
                        f"only-api={sorted(m for m in mpn_diff & set(api_by_mpn))[:5]}")
    else:
        print(f"[OK] MPN sets identical: {len(script_by_mpn)} unique MPNs")

    total_cell_diffs = 0
    rows_with_diffs = 0
    example_diffs: list[str] = []
    for mpn in sorted(set(script_by_mpn) & set(api_by_mpn)):
        s_rows = script_by_mpn[mpn]
        a_rows = api_by_mpn[mpn]
        if len(s_rows) != len(a_rows):
            failures.append(f"MPN {mpn!r}: multiplicity differs "
                            f"script={len(s_rows)} api={len(a_rows)}")
            continue
        for i, (s_row, a_row) in enumerate(zip(s_rows, a_rows)):
            row_diffs = []
            for col in script_cols:
                sv = s_row.get(col, "")
                av = a_row.get(col, "")
                if sv != av:
                    row_diffs.append((col, sv, av))
                    total_cell_diffs += 1
            if row_diffs:
                rows_with_diffs += 1
                for col, sv, av in row_diffs[:3]:
                    example_diffs.append(
                        f"  MPN={mpn} col={col!r}: script={sv!r} api={av!r}"
                    )
                if len(row_diffs) > 3:
                    example_diffs.append(f"    ...+{len(row_diffs)-3} more diffs on this row")

    if total_cell_diffs == 0 and not failures:
        print(f"[OK] all {len(script_rows)} rows byte-equal cell-by-cell "
              f"(keyed on Mfg_Part_Num; row order may differ and is ignored)")
    else:
        failures.append(f"cell diffs: {total_cell_diffs} across {rows_with_diffs} rows")
        if example_diffs:
            print("--- first example diffs ---")
            for line in example_diffs[:15]:
                print(line)

    print()
    if failures:
        print("=== SMOKE TEST FAIL ===")
        for f in failures:
            print(" -", f)
        return 1
    print("=== SMOKE TEST PASS ===")
    print("script and API CSVs are byte-equal (content-keyed on Mfg_Part_Num, "
          "row-order differences ignored per completion-order artifact).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
