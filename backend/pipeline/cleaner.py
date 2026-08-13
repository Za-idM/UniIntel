"""L1/L2 cleaning: placeholder values -> NULL. No LLM."""

PLACEHOLDER_VALUES = {
    "-- unbranded --",
    "-- no unilog brand --",
    "-- no dib brand --",
    "-",
    "",
    "n/a",
    "na",
    "none",
}


def clean_value(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    if stripped.lower() in PLACEHOLDER_VALUES:
        return None
    return stripped


def clean_row(row: dict[str, str]) -> dict[str, str | None]:
    return {key: clean_value(value) for key, value in row.items()}
