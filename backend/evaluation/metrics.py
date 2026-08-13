"""Per-row and aggregate scoring of a stored EnrichedProduct against its
GT row. Match status is 3-way (EXACT/CLOSE/MISS) per the locked plan's
"never hide uncertainty" framing -- CLOSE surfaces near-misses instead of
silently bucketing them with exact hits or total failures."""
from rapidfuzz import fuzz

from evaluation.ground_truth import gt_attributes

CLOSE_THRESHOLD = 85  # rapidfuzz 0-100 token_sort_ratio

DESCRIPTION_FIELDS = [
    ("invoice_desc", "INVOICE_DESC"),
    ("mobile_desc", "MOBILE_DESC"),
    ("short_desc", "SHORT_DESC"),
    ("retail_desc", "RETAIL_DESC"),
]


def _match_status(got: str | None, expected: str | None) -> str:
    if not expected:
        return "NO_GT"
    if not got:
        return "MISS"
    if got == expected:
        return "EXACT"
    if fuzz.token_sort_ratio(got, expected) >= CLOSE_THRESHOLD:
        return "CLOSE"
    return "MISS"


def evaluate_product(product_data: dict, gt_row: dict) -> dict:
    """product_data is the parsed EnrichedProduct JSON (data_json column).
    Returns a per-row scorecard: classpath/manufacturer match, attribute
    field tally, and description match status per deterministic field."""
    classpath_match = product_data.get("classpath") == gt_row.get("Classpath", "").strip()
    manufacturer_match = product_data.get("manufacturer_name") == gt_row.get("MANUFACTURER_NAME", "").strip()

    gt_attrs = gt_attributes(gt_row)
    produced_attrs = {a["label"]: a.get("value") for a in product_data.get("attributes", []) if a.get("value")}

    attribute_rows = []
    field_correct = 0
    for label, expected_value in gt_attrs.items():
        got_value = produced_attrs.get(label)
        status = _match_status(got_value, expected_value)
        if status == "EXACT":
            field_correct += 1
        attribute_rows.append({"label": label, "got": got_value, "expected": expected_value, "status": status})

    descriptions = product_data.get("descriptions") or {}
    description_status = {}
    for field_key, gt_col in DESCRIPTION_FIELDS:
        got = descriptions.get(field_key)
        expected = gt_row.get(gt_col, "").strip()
        description_status[field_key] = _match_status(got, expected)

    return {
        "product_id": product_data.get("product_id"),
        "mfg_part_num": product_data.get("mfg_part_num"),
        "classpath": product_data.get("classpath"),
        "classpath_match": classpath_match,
        "manufacturer_match": manufacturer_match,
        "attribute_field_correct": field_correct,
        "attribute_field_total": len(gt_attrs),
        "attribute_rows": attribute_rows,
        "description_status": description_status,
    }


def _pct(correct: int, total: int) -> float:
    return round(100 * correct / total, 1) if total else 0.0


def summarize(rows: list[dict]) -> dict:
    """Aggregate a list of evaluate_product() rows into the dashboard's
    summary-card numbers."""
    n = len(rows)
    classpath_correct = sum(1 for r in rows if r["classpath_match"])
    manufacturer_correct = sum(1 for r in rows if r["manufacturer_match"])
    attr_correct = sum(r["attribute_field_correct"] for r in rows)
    attr_total = sum(r["attribute_field_total"] for r in rows)

    description_summary = {}
    for field_key, _ in DESCRIPTION_FIELDS:
        statuses = [r["description_status"][field_key] for r in rows]
        scored = [s for s in statuses if s != "NO_GT"]
        exact = sum(1 for s in scored if s == "EXACT")
        description_summary[field_key] = {
            "correct": exact,
            "total": len(scored),
            "pct": _pct(exact, len(scored)),
        }

    return {
        "rows_evaluated": n,
        "classpath_accuracy": {"correct": classpath_correct, "total": n, "pct": _pct(classpath_correct, n)},
        "manufacturer_accuracy": {"correct": manufacturer_correct, "total": n, "pct": _pct(manufacturer_correct, n)},
        "attribute_accuracy": {"correct": attr_correct, "total": attr_total, "pct": _pct(attr_correct, attr_total)},
        "description_match_rates": description_summary,
    }
