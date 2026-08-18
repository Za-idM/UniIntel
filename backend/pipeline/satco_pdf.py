"""
Direct, no-LLM extraction of Satco spec-sheet PDFs into the LED Light Bulbs
leaf template's 27 slots.

Two layouts are observed in the wild, both supported here:

  Layout A -- single-SKU spec sheet (S21354 / S21363 pattern):
    One page. Right half of the page is a vertical label/value table with
    labels at x~316 and values at x~444 (pdfplumber word coordinates on a
    612x792 page). Header rows ("General", "Electrical", ...) are lone
    centered words with no value column; form blanks ("Project Name",
    "Location", "Notes") sit on the left half and are ignored. The bottom
    of the page carries two lines:
        SATCO S21354
        8T9/LED/CL/927/120V/E26
    The second of those is the bulb designation code (template slot 13).

  Layout B -- multi-SKU family sheet (S11445 pattern):
    Two pages. Page 2 carries:
      - a per-row specs table whose columns are (Item, Shape, Base, Watts,
        Replacement Wattage, Lumens, CCT, Beam Angle, Efficacy, UPC, Pack
        Qty); a row exists per SKU and is selected by the input MPN.
      - a "GENERAL SPECIFICATIONS" block of "Label: Value" lines applying
        to all SKUs in the family (Operating Voltage/Frequency, CRI, Life,
        Location Rating, ...). Only the CRI and Functional Life lines are
        load-bearing for the LED template.
      - a "DIMENSIONS" table with columns (Item, MOL, MOD, Weight) -- MOL
        maps to Length, MOD to Diameter. As with the specs table, the row
        is selected by MPN.

Direct mapping (PDF label -> template slot label), with value cleanup
that strips vendor UOM (8W -> 8, 2700K -> 2700, 800L -> 800, 5.11\" ->
5.11, "1,050" -> "1050") and canonicalises:
  - Dimmable: "Yes-Dimmable" -> "Dimmable"  (LOV value)
  - Title 20: "T20 Listed"/"Lawful for sale in California" ->
              "Title 20 Compliant"  (LOV value)
  - Energy Star "No" -> ""  (matches GT's choice to leave the slot empty)
  - CRI "90" -> "90+"       (Satco prints "90" but the LED-bulb catalog
                             convention -- and GT -- uses "90+"; S21363
                             already prints "90+", so this is purely a
                             source-format canonicalisation, not an
                             invention)

Two derived slots that the PDF does not label directly but which GT
expects, derived deterministically from extracted data (no invention):
  - Bulb Shape name (slot 4): from Bulb Shape Code prefix -- T -> Tube,
    ST -> Type ST, A -> Type A, G -> Globe, B -> Type B, BR -> Type BR,
    R -> Type R (every LED-bulb Shape Code in the wild follows this).
  - Bulb Base name (slot 8): from Bulb Base Code -- E26 -> Medium,
    E12 -> Candelabra (the only two in the LOV).

This is a deliberate bypass of the LLM Stage 4 extraction: the PDF data
is already labelled, structured and clean, so an LLM round-trip would add
cost, latency and (observed) prompt-adherence noise for zero accuracy
gain. When a slot cannot be filled from the PDF (Layout A has no
Incandescent Equivalent field for S21354; Layout B has no Bulb
Designation line) it is left empty -- never invented.
"""
from __future__ import annotations

import io
import re

import pdfplumber

from leaf_templates.registry import get_template


# ---------------------------------------------------------------------------
# PDF-label -> template-slot mapping
# ---------------------------------------------------------------------------

# None means "the PDF has this field but it isn't in the LED template" --
# we recognise it so the parser can skip it without noise.
SATCO_LABEL_MAP: dict[str, str | None] = {
    # single-SKU layout table
    "Status": None,
    "Watts": "Wattage",
    "Incandescent Equivalent": "Incandescent Wattage Equivalent",
    "Volts": "Voltage Rating",
    "Shape": "Bulb Shape Code",
    "Base": "Bulb Base",
    "ANSI Base": "Bulb Base Code",
    "Finish": "Bulb Finish",
    "CCT (Kelvin)": "Color Temperature",
    "CCT": "Color Temperature",
    "Temperature": "Light Appearance",
    "CRI": "Color Rendering Index (CRI)",
    "Lumens": "Lumens",
    "Beam Spread": "Beam Angle",
    "Dimmable": "Dimmable",
    "Dimming Note": None,
    "Hours Rated": "Average Life",
    "Product Category": None,
    "Technology": None,
    "Operating Frequency": None,
    "Power Factor": None,
    "Operating Temperature": None,
    "MOL": "Length",
    "MOD": "Diameter",
    "Housing Color": None,
    "Weight (lb.)": None,
    "Weight": None,
    "Rated For Enclosed Fixture": None,
    "Warranty": None,
    "Safety Listing": None,
    "Location Rating": None,
    "UL Application": None,
    "NSF Approved": None,
    "California Status": "Title 20 Compliant",
    "Title 20 / 24 Status": "Title 20 Compliant",
    "CA T20 / T24 Rationale": "Title 20 Compliant",
    "RoHS Compliant": None,
    "FCC Compliant": None,
    "Canadian Standard": None,
    "SDS Sheet": None,
    "Energy Star": "Energy Star Certified",
    # family-sheet GENERAL SPECIFICATIONS block labels (as the literal
    # pre-colon string, e.g. "Input Voltage, Frequency" for the line
    # "Input Voltage, Frequency: 120V/60Hz").
    "Input Voltage, Frequency": "Voltage Rating",
    "Functional Life": "Average Life",
    # family-sheet column headers
    "Item": None,
    "Replacement Wattage": "Incandescent Wattage Equivalent",
    "Beam Angle": "Beam Angle",
    "Efficacy": None,
    "UPC": None,
    "Pack Qty": None,
}


# Bulb Shape Code prefix -> Bulb Shape name (slot 4). Order matters: BR
# before B, ST before S/T. LoV: Globe/Tube/Type A/Type B/Type BR/Type R/Type ST.
_SHAPE_NAME_BY_PREFIX: list[tuple[str, str]] = [
    ("BR", "Type BR"),
    ("ST", "Type ST"),
    ("T", "Tube"),
    ("A", "Type A"),
    ("G", "Globe"),
    ("R", "Type R"),
    ("B", "Type B"),
]

# Bulb Base Code -> Bulb Base name (slot 8). LoV: Candelabra / Medium.
_BASE_NAME_BY_CODE: dict[str, str] = {
    "E26": "Medium",
    "E12": "Candelabra",
}


# ---------------------------------------------------------------------------
# Value cleanup
# ---------------------------------------------------------------------------

def _strip_uom(value: str, slot_label: str) -> str:
    """Strip the vendor-printed UOM from a raw PDF value for a known slot.
    Returns "" for empty/missing. One careful special-case per slot, since
    naive `re.sub(r"[A-Za-z]+$", "")` would corrupt values that genuinely
    end in letters (e.g. "Warm White")."""
    v = value.strip()
    if not v:
        return ""

    if slot_label == "Wattage":
        m = re.match(r"^(.+?)\s*W$", v)
        return m.group(1).strip() if m else v
    if slot_label == "Incandescent Wattage Equivalent":
        m = re.match(r"^(.+?)\s*W$", v)
        return m.group(1).strip() if m else v
    if slot_label == "Voltage Rating":
        m = re.match(r"^(\d+(?:\.\d+)?)\s*V", v)
        return m.group(1) if m else v
    if slot_label == "Color Temperature":
        m = re.match(r"^(.+?)\s*K$", v)
        return m.group(1).strip() if m else v
    if slot_label == "Lumens":
        v = re.sub(r"\s*L$", "", v)
        v = v.replace(",", "")
        return v
    if slot_label == "Beam Angle":
        v = v.replace(",", "")
        v = re.sub(r"\s*(deg|Degrees|°|\"\")\s*$", "", v, flags=re.IGNORECASE)
        return v.strip()
    if slot_label == "Average Life":
        v = v.replace(",", "")
        v = re.sub(r"\s*(Hours|Hrs|hr)\.?\s*$", "", v, flags=re.IGNORECASE)
        return v.strip()
    if slot_label in ("Length", "Diameter"):
        v = v.replace(",", "")
        v = v.replace('"', "").replace("'", "").replace(" in", "").replace("in", "")
        v = v.strip()
        m = re.match(r"^(\d+)\.(\d+?)0+$", v)
        if m:
            int_part, frac = m.group(1), m.group(2)
            return f"{int_part}.{frac}" if frac else int_part
        return v
    if slot_label == "Dimmable":
        v_low = v.lower()
        if "no" in v_low and "yes" not in v_low:
            return ""
        if "yes" in v_low or "dimmable" in v_low:
            return "Dimmable"
        return ""
    if slot_label == "Energy Star Certified":
        return "Energy Star Certified" if v.strip().lower() == "yes" else ""
    if slot_label == "Title 20 Compliant":
        v_low = v.lower()
        if "no" in v_low and "yes" not in v_low and "lawful" not in v_low and "listed" not in v_low:
            return ""
        if "listed" in v_low or "lawful" in v_low or "compliant" in v_low:
            return "Title 20 Compliant"
        return ""
    if slot_label == "Color Rendering Index (CRI)":
        v = v.replace(",", "")
        if v in ("90", "90 "):
            return "90+"
        return v

    v = v.replace(",", "").strip()
    return v


def _derive_shape_name(shape_code: str) -> str:
    code = shape_code.strip().upper()
    for prefix, name in _SHAPE_NAME_BY_PREFIX:
        if code.startswith(prefix):
            return name
    return ""


def _derive_base_name(base_code: str) -> str:
    return _BASE_NAME_BY_CODE.get(base_code.strip().upper(), "")


# ---------------------------------------------------------------------------
# pdfplumber row helpers
# ---------------------------------------------------------------------------

def _group_rows(words, row_tol: float = 2.5) -> list[list[dict]]:
    """Group pdfplumber words into rows by shared `top` coordinate."""
    rows: list[list[dict]] = []
    for w in sorted(words, key=lambda x: (round(x["top"], 1), x["x0"])):
        if rows and abs(w["top"] - rows[-1][0]["top"]) <= row_tol:
            rows[-1].append(w)
        else:
            rows.append([w])
    return rows


def _row_text(row_words: list[dict]) -> str:
    return " ".join(w["text"] for w in sorted(row_words, key=lambda x: x["x0"]))


def _word_center(w: dict) -> float:
    return (w["x0"] + w["x1"]) / 2.0


# ---------------------------------------------------------------------------
# Layout A: single-SKU vertical label/value table
# ---------------------------------------------------------------------------

# Label words live in the x range [~305, ~360]; value words at x >= ~400.
# Sections ("General", "Compliance") are lone centered words with no
# value-column content. Form blanks ("Project Name", "Notes") sit at
# x < 200 and are ignored by the label-word filter.
_LABEL_X_MIN = 305.0
_LABEL_X_MAX = 360.0
_VALUE_X_MIN = 400.0

_DESIGNATION_RE = re.compile(r"^\d[A-Z0-9]+(?:/[A-Z0-9]+)+$")
_SATCO_SKU_RE = re.compile(r"\bSATCO\s+(S\d+)\b")


def _parse_single_sku(pdf: pdfplumber.PDF) -> dict[str, str]:
    """Parse the single-SKU vertical layout. Returns {template_label: value}."""
    out: dict[str, str] = {}

    for page in pdf.pages:
        rows = _group_rows(page.extract_words())
        # First pass: extract the two-column label/value table.
        for i, row_words in enumerate(rows):
            label_words = [
                w for w in row_words
                if _LABEL_X_MIN <= w["x0"] < _LABEL_X_MAX
            ]
            value_words = [w for w in row_words if w["x0"] >= _VALUE_X_MIN]
            if not label_words or not value_words:
                continue

            label = " ".join(
                w["text"] for w in sorted(label_words, key=lambda x: x["x0"])
            ).strip()
            value = " ".join(
                w["text"] for w in sorted(value_words, key=lambda x: x["x0"])
            ).strip()
            target = SATCO_LABEL_MAP.get(label)
            if target is None:
                continue
            cleaned = _strip_uom(value, target)
            if cleaned:
                out[target] = cleaned

        # Second pass: find the bulb designation row directly. Satco
        # prints it as a single line of shape `8T9/LED/CL/927/120V/E26`
        # (digit-prefixed, slash-separated) -- scan every row's text
        # rather than walking forward from the SATCO+SKU row, because
        # pdfplumber's `top` order interleaves the bottom-of-page billing
        # line with main-table rows (in the S21354/S21363 sample the
        # "Rated For Enclosed Fixture Yes" row sits between the SATCO
        # line and the designation line at adjacent `top` values).
        for row_words in rows:
            text = _row_text(row_words).strip()
            if _DESIGNATION_RE.match(text):
                out["Bulb Designation"] = text
                break

    _post_process_derived(out)
    return out


# ---------------------------------------------------------------------------
# Layout B: multi-SKU family sheet
# ---------------------------------------------------------------------------

_SKU_RE = re.compile(r"^S\d{4,}$")

# Two-line column headers like "Replacement Wattage" / "Beam Angle" are
# split across adjacent row groups (top ~246 + ~253). Merge header words
# whose `top` falls within +/- HEADER_ROW_TOLERANCE of the row containing
# the "Item" anchor so the column index sees both fragments.
_HEADER_ROW_TOLERANCE = 12.0


def _find_header_row_idx(rows: list[list[dict]]) -> int | None:
    """Find the row index that anchors a column table header -- the row
    whose leftmost word is literally "Item" at x < 60."""
    for i, row_words in enumerate(rows):
        for w in row_words:
            if w["text"].strip() == "Item" and w["x0"] < 60:
                return i
    return None


def _collect_header_words(rows: list[list[dict]], anchor_idx: int) -> list[dict]:
    """Pull words from the rows whose `top` is within
    +/- HEADER_ROW_TOLERANCE of the anchor row's top, so multi-line
    header labels ("Replacement\\nWattage", "Beam\\nAngle") arrive in
    one merged header-list."""
    if anchor_idx is None or anchor_idx >= len(rows):
        return []
    anchor_top = rows[anchor_idx][0]["top"]
    merged: list[dict] = []
    for r in rows:
        if r and abs(r[0]["top"] - anchor_top) <= _HEADER_ROW_TOLERANCE:
            merged.extend(r)
    return merged


def _extract_table_row(
    rows: list[list[dict]],
    header_row_idx: int,
    after_idx: int,
    target_mpn: str,
) -> dict[str, str] | None:
    """Given a column-table header row index and the row index to start
    scanning data from, return {column_text: cell_value} for the row whose
    first-column word equals `target_mpn`.

    `header_row_idx` anchors the column-header-words pull (used to build
    the per-column nearest-center assignment). `after_idx` is where to
    begin scanning for data rows (>= the last header row + 1, after any
    header fragments that span multiple sub-rows).
    """
    header_words = _collect_header_words(rows, header_row_idx)
    if not header_words:
        return None
    header_centers = [(w["text"].strip(), _word_center(w)) for w in header_words]
    if not header_centers:
        return None

    for data_row in rows[after_idx:]:
        text = _row_text(data_row).strip()
        if not text:
            continue
        # Stop when the table ends: first word no longer an SKU identifier.
        first_word = sorted(data_row, key=lambda x: x["x0"])[0]["text"]
        if not _SKU_RE.match(first_word):
            break
        if first_word != target_mpn:
            continue

        cells: dict[str, list[str]] = {}
        for w in data_row:
            col = _column_for_word(w, header_centers)
            cells.setdefault(col, []).append(w["text"])
        return {k: " ".join(v) for k, v in cells.items()}
    return None


def _column_for_word(word: dict, header_centers: list[tuple[str, float]]) -> str:
    """Pick the column whose header center is nearest to this word's
    horizontal center."""
    wc = _word_center(word)
    # Merge header-centers that share column membership by collapsing
    # headers whose centers are within 8pt of each other (two-line column
    # headers like "Replacement" + "Wattage" should produce a single
    # column-anchor for lookup). Build the canonical first-fragment label
    # so the second fragment ("Wattage", "Angle") doesn't get treated as
    # its own column and steal value-words whose centers happen to be
    # slightly closer to the second-line word.
    candidates = header_centers
    # Collapse near-duplicate centers into one anchor -- pick the
    # earliest `top`-sorted fragment label of the cluster.
    collapsed: list[tuple[str, float]] = []
    used = [False] * len(candidates)
    for i, (lab, cx) in enumerate(candidates):
        if used[i]:
            continue
        used[i] = True
        cluster_label = lab
        cluster_cx = cx
        for j in range(i + 1, len(candidates)):
            if used[j]:
                continue
            if abs(candidates[j][1] - cx) <= 10.0:
                used[j] = True
        collapsed.append((cluster_label, cluster_cx))

    best_label, best_dist = collapsed[0][0], abs(wc - collapsed[0][1])
    for label, c in collapsed[1:]:
        d = abs(wc - c)
        if d < best_dist:
            best_label, best_dist = label, d
    return best_label


def _collapse_header_label(label: str) -> str:
    """Canonical header label for SATCO_LABEL_MAP lookup. pdfplumber
    sometimes splits "Replacement Wattage" across two words that the row
    builder then joins as "Replacement Wattage" or "Wattage Replacement"
    -- but the only multi-word headers we hit are
      Replacement Wattage  ->  Incandescent Wattage Equivalent
      Beam Angle            ->  Beam Angle
      Pack Qty             ->  (drop)
    Recognise both forms explicitly so the column lookup is order-
    insensitive."""
    if label in ("Wattage", "Replacement"):
        return "Replacement Wattage"
    if label in ("Beam", "Angle"):
        return "Beam Angle"
    if label in ("Pack", "Qty"):
        return "Pack Qty"
    return label


def _parse_general_specs_block_regex(page: pdfplumber.page.Page) -> dict[str, str]:
    """Use the page's `extract_text()` (which respects the underlying PDF
    line-stream, unlike `extract_words` which groups words sharing the
    same `top`) so that lines like "CRI: 90" -- which share `top` with
    the adjacent "Ambient Operating Temperature: ..." line -- are split
    cleanly instead of merged into a colon-collision cell."""
    text = page.extract_text() or ""
    out: dict[str, str] = {}

    # Per-line regexes keyed by the pre-colon label. The list is short
    # because only Input Voltage / Functional Life / CRI are load-bearing
    # for the LED template out of the GENERAL SPECIFICATIONS block.
    line_specs = [
        # S21445 PDF prints "Input Voltage, Frequency:  120V/60Hz".
        (re.compile(r"Input\s+Voltage,?\s+Frequency\s*:\s*(\S[^\n]*)"), "Input Voltage, Frequency"),
        (re.compile(r"Functional\s+Life\s*:\s*([\d,\s]+(?:Hours|Hrs|hr)?)"), "Functional Life"),
        (re.compile(r"(?<!Ambient Operating )CRI\s*:\s*(\d[\d+]*)"), "CRI"),
    ]

    for line in text.splitlines():
        for pat, label in line_specs:
            m = pat.search(line)
            if not m:
                continue
            raw_value = m.group(1).strip()
            target = SATCO_LABEL_MAP.get(label)
            if target is None or not raw_value:
                continue
            cleaned = _strip_uom(raw_value, target)
            if cleaned:
                out[target] = cleaned
    return out


def _parse_multi_sku(pdf: pdfplumber.PDF, target_mpn: str) -> dict[str, str]:
    """Parse the two-page family-sheet layout for one target SKU. Returns
    {template_label: value} for that SKU row."""
    out: dict[str, str] = {}

    for page in pdf.pages:
        rows = _group_rows(page.extract_words())
        row_top_texts = [_row_text(r).upper() for r in rows]

        # Pass 1: ITEM SPECIFICATIONS table. Identify the section header
        # row, then find the column header row inside it (the one
        # beginning with the "Item" word), then walk forward to find the
        # target MPN's data row.
        for i, ttext in enumerate(row_top_texts):
            if "ITEM SPECIFICATIONS" not in ttext:
                continue
            header_idx = _find_header_row_idx_after(rows, i)
            if header_idx is None:
                continue
            header_top = rows[header_idx][0]["top"]
            after_idx = header_idx + 1
            # Advance past the header fragments that fall inside the
            # +/-HEADER_ROW_TOLERANCE window so we don't accidentally
            # treat a sub-header word ("Wattage", "Angle") as a data row.
            while after_idx < len(rows) and rows[after_idx] and (
                abs(rows[after_idx][0]["top"] - header_top) <= _HEADER_ROW_TOLERANCE
            ):
                after_idx += 1

            row = _extract_table_row(rows, header_idx, after_idx, target_mpn)
            if not row:
                continue
            # Label the cells by column text and map to template slots.
            seen_columns: set[str] = set()
            for col_label_raw, cell_value in row.items():
                col_label = _collapse_header_label(col_label_raw)
                # Skip duplicate header fragments already collapsed.
                if col_label in seen_columns:
                    continue
                seen_columns.add(col_label)
                target = SATCO_LABEL_MAP.get(col_label)
                if target is None:
                    continue
                # The family-shell "Shape" column holds the shape CODE
                # (A19, "PAR 38"). pdfplumber may have split as "PAR 38"
                # across spaces; collapse to "PAR38" for the GT match.
                if target == "Bulb Shape Code":
                    cell_value = cell_value.replace("PAR ", "PAR")
                cleaned = _strip_uom(cell_value, target)
                if cleaned:
                    out[target] = cleaned
            break  # only one ITEM SPECIFICATIONS table per page

        # Pass 2: GENERAL SPECIFICATIONS block. Identified by a row whose
        # upper-cased text contains "GENERAL SPECIFICATIONS"; extracted via
        # the page's extract_text() (which splits the lines pdfplumber's
        # word grouping would collide -- see _parse_general_specs_block_regex).
        for i, ttext in enumerate(row_top_texts):
            if "GENERAL SPECIFICATIONS" in ttext:
                out.update(_parse_general_specs_block_regex(page))
                break

        # Pass 3: DIMENSIONS table. Its column headers ("Item"/"MOL"/
        # "MOD"/"Weight"/"(Lbs)") span multiple row-groups (top ~353..363
        # in the sample), so the strict "row contains all three" check
        # the comment above warned about isn't used here -- we detect an
        # anchor row containing any of {"MOL","MOD","(A)","(B)","Weight"}
        # near a `DIMENSIONS` section header, then build the column
        # anchor across the +/- HEADER_ROW_TOLERANCE window. The data
        # rows (Sxxxx MOL MOD Weight) sit below the anchor cluster and
        # are scanned starting after the cluster's bottom edge.
        for i, row_words in enumerate(rows):
            texts = [w["text"].strip() for w in row_words]
            if not {"MOL", "MOD", "(A)", "(B)", "Weight"}.intersection(texts):
                continue
            # Confirm this is the DIMENSIONS table by checking an
            # explicit "DIMENSIONS" section header above; otherwise a
            # stray MOL on another table could match.
            if not any(
                "DIMENSIONS" in _row_text(rows[k]).upper()
                for k in range(max(0, i - 10), i)
            ):
                continue
            header_top = row_words[0]["top"]
            after_idx = i
            # The header cluster spans the cell containing the anchor +
            # any sub-rows within tolerance. Advance `after_idx` past the
            # cluster bottom so the data row scan starts cleanly.
            while after_idx < len(rows) and rows[after_idx] and (
                abs(rows[after_idx][0]["top"] - header_top) <= _HEADER_ROW_TOLERANCE
            ):
                after_idx += 1
            # Re-anchor the header words on the row carrying "MOL" so
            # `_extract_table_row`'s `header_top` (taken from rows[i][0]
            # top) sits at the centre of the multi-line header cluster
            # rather than at its top edge -- the +/- tolerance window
            # then captures both the "Item" sub-header above and the
            # "Weight"/"(Lbs)" sub-header below.
            mol_anchor_idx = i
            for k in range(i, after_idx):
                if any(w["text"].strip() == "MOL" for w in rows[k]):
                    mol_anchor_idx = k
                    break
            row = _extract_table_row(rows, mol_anchor_idx, after_idx, target_mpn)
            if row:
                # The column-header cluster for the MOL column can be
                # labelled "(A)" (the parenthesised dimension marker)
                # or "MOL" -- both have the same x-center and collapse
                # to one anchor; accept either as the "Length" source.
                # Same for "(B)"/"MOD" -> "Diameter".
                mol_synonyms = {"MOL", "(A)"}
                mod_synonyms = {"MOD", "(B)"}
                for k, v in row.items():
                    if k in mol_synonyms:
                        cleaned = _strip_uom(v, "Length")
                        if cleaned:
                            out["Length"] = cleaned
                    elif k in mod_synonyms:
                        cleaned = _strip_uom(v, "Diameter")
                        if cleaned:
                            out["Diameter"] = cleaned
            break

    _post_process_derived(out)
    return out


def _find_header_row_idx_after(
    rows: list[list[dict]], start_idx: int
) -> int | None:
    """Find the row whose leftmost word is "Item" at x < 60, within the
    next 8 rows after `start_idx`. (The ITEM SPECIFICATIONS section has
    a sub-header row "ITEM SPECIFICATIONS / ORDER INFO" stacked above
    the actual column header row "Item | Shape | Base | ...".)"""
    for k in range(start_idx + 1, min(start_idx + 10, len(rows))):
        for w in rows[k]:
            if w["text"].strip() == "Item" and w["x0"] < 60:
                return k
    return None


def _post_process_derived(out: dict[str, str]) -> None:
    """Fill two slots that the PDF doesn't label directly but which GT
    expects, derived deterministically from codes we did extract. No
    value is invented -- the derivation rules are checked against the
    mined LOV."""
    # Family-sheet tables only print the base CODE (E26) in the "Base"
    # column, with no separate "ANSI Base" label. When the Bulb Base
    # slot ended up holding a code-like value (E26 / E12 / E17 / E39),
    # reroute it to Bulb Base Code and derive the human name for slot 8.
    if "Bulb Base" in out:
        val = out["Bulb Base"].strip().upper()
        if val in _BASE_NAME_BY_CODE:
            out["Bulb Base Code"] = val
            del out["Bulb Base"]
            name = _derive_base_name(val)
            if name:
                out["Bulb Base"] = name
    if "Bulb Shape Code" in out and "Bulb Shape" not in out:
        name = _derive_shape_name(out["Bulb Shape Code"])
        if name:
            out["Bulb Shape"] = name
    if "Bulb Base Code" in out and "Bulb Base" not in out:
        name = _derive_base_name(out["Bulb Base Code"])
        if name:
            out["Bulb Base"] = name


# ---------------------------------------------------------------------------
# Spec_Chart.pdf layout -- High Bay Fixtures (wattage/CCT/lumen matrix)
# ---------------------------------------------------------------------------
#
# Satco serves a small subset of its non-LED SKUs a "Spec_Chart" PDF at
# the constructible URL
#   https://assets.satco.com/media-prod/image/upload/Certs/{MPN}_Spec_Chart.pdf
# (only available for some SKUs; 65-771R3 and 65-771R2 confirmed live, the
# pattern isn't universal across Satco's catalog -- the orchestrator probes
# the URL and falls back to the LLM-over-page-text path when the file 404s).
#
# This layout is a strictly tabular text chart, distinctly different from
# the LED layouts (A/B) -- there's no labelled "key: value" structure.
# Format:
#   LED UFO HIGHBAYS
#   Wattage, CCT, & Lumen Chart
#   Sku Wattage/CCT 3000K 4000K 5000K
#   80W 11840L 13440L 12800L
#   65/770R3 100W 14200L 16000L 15000L
#   120W 16320L 18600L 17280L
#   80W 11840L 13440L 12800L
#   65/812 100W 14200L 16000L 15000L
#   ...
# The "Sku" column anchors a multi-row group; the next rows until another
# SKU appears are all (wattage, lumens) tuples belonging to that SKU.
# (i.e. a row without an SKU prefix is a continuation row whose wattage
# and CCT lumen values are additional configurations of the most-recently
# seen SKU.)
#
# Only three slots in the High Bay Fixtures leaf template can be filled by
# this chart:
#   * Fixture Wattage -- all wattages for the SKU, slash-joined in
#     ascending numeric order (GT for 65-771R3 expects "150/175/200").
#   * Color Temperature -- CCT column labels from the header row,
#     formatted as "3000 K, 4000 K, 5000 K" (insert space before K, comma-
#     separated).
#   * Lumens -- intentionally NOT produced here: the GT value
#     "22200 to 28800" for 65-771R3 doesn't correspond to a simple min/max
#     of the chart (the PDF max is 31000, at 200W/4000K) but rather to a
#     specific convention. The satco.com HTML page already prints "22200
#     to 28800" verbatim and the LLM Stage 4 extraction over page text
#     picks it up correctly -- so we leave Lumens to that path rather
#     than hard-code an opaque convention here that may not generalise to
#     the next High Bay Spec_Chart SKU. A future pass can derive a rule
#     if more GT High Bay rows become available.
#
# Everything else on the High Bay leaf template (Voltage Rating, CRI,
# Dimmable, Lens Type, Fixture Material, Enclosure Type, Dimensions,
# Additional Information, ...) comes from the satco.com HTML product page
# via the LLM Stage 4 extraction path that already runs for non-LED Satco
# rows -- this PDF direct-mapper only CONTRIBUTES these 2 high-precision
# slots, it does not replace the LLM extraction for the rest.

_HB_CLASSPATH = "Electrical>Lamps & Lightings>Indoor Lighting>High Bay Fixtures"

# Match a "Sku Wattage 3000K 4000K 5000K" header row, the CCT labels
# (digit-run followed by K) become the Color Temperature columns.
_HB_HEADER_RE = re.compile(
    r"^\s*Sku\s+Wattage/CCT\s+(?P<ccts>(?:\d+K\s*)+)\s*$",
    re.IGNORECASE,
)
# A row of data is "<watt>W <lumens>L ...", optionally prefixed with an SKU
# token. SKU token: alnum + optional single hyphen-or-slash + alnum + optional
# trailing digits/letters (matches 65/770R3, 65-812, 65/771R3, etc.).
_HB_SKU_TOKEN_RE = re.compile(r"^\d{1,4}[-/]\w{1,10}$")
_HB_DATA_ROW_RE = re.compile(
    r"^(?P<sku>\S+\s+)?(?P<watt>\d+)W\s+(?P<lumens>(?:\d+L\s*)+)\s*$",
    re.IGNORECASE,
)


def parse_satco_spec_chart_highbay(pdf_bytes: bytes, mpn: str) -> dict[str, str]:
    """Parse a Satco High Bay Fixtures "_Spec_Chart.pdf" into the High Bay
    leaf template's `Fixture Wattage` and `Color Temperature` slots.
    Returns an empty dict on any failure -- the LLM-over-page-text path
    remains authoritative for every slot not in this dict, so a missing /
    bad / new-layout PDF degrades to the pre-existing baseline cleanly.

    `mpn` is in dash form ("65-771R3"); the PDF's SKU column uses slash
    form ("65/771R3") -- this function normalises both for matching, so
    a caller passing either form works.

    Layout in the observed Spec_Chart PDF (LED UFO Highbays):
        Sku Wattage/CCT 3000K 4000K 5000K
        80W 11840L 13440L 12800L            <- preceding orphan
        65/770R3 100W 14200L 16000L 15000L  <- SKU anchor (middle row)
        120W 16320L 18600L 17280L           <- following orphan
        80W ...                              <- next SKU's preceding orphan
        65/812 100W ...                      <- next SKU anchor
        ...

    The SKU label physically sits at the vertical centre of its 3-row
    group, so when extract_text() flattens the page to a line stream the
    orphan row BEFORE an SKU anchor belongs to that same SKU (plus the
    orphan row AFTER -- typical 3-wattage SKU groups). The parse collects
    all data rows first, then groups each SKU-anchor's adjacent orphans
    with it."""
    try:
        pdf = pdfplumber.open(io.BytesIO(pdf_bytes))
    except Exception:
        return {}
    try:
        target_sku = mpn.strip().replace("-", "/")
        target_sku_dash = mpn.strip().replace("/", "-")

        cct_labels: list[str] = []
        # Each row is (watt:int, sku_or_None). Order is line-stream order
        # -- pdfplumber preserves the visual top-to-bottom ordering.
        data: list[tuple[int, str | None]] = []

        for page in pdf.pages:
            for line in (page.extract_text() or "").splitlines():
                # Detect header row and pull CCT column labels.
                h = _HB_HEADER_RE.match(line)
                if h:
                    cct_labels = re.findall(r"\d+K", h.group("ccts"))
                    continue

                # Detect data rows. May or may not carry an SKU prefix.
                d = _HB_DATA_ROW_RE.match(line)
                if not d:
                    continue
                sku_token = (d.group("sku") or "").strip()
                sku: str | None = None
                if sku_token and _HB_SKU_TOKEN_RE.match(sku_token):
                    sku = sku_token.replace("-", "/")
                data.append((int(d.group("watt")), sku))

        if not cct_labels:
            return {}

        # Group rows by SKU: each SKU anchor's row also pulls in the
        # immediately-adjacent orphan rows (one before, one after -- the
        # observed Spec_Chart has 3-watt-per-SKU groups with the SKU
        # anchor in the middle). Generalisation to longer SKU groups:
        # extend the orphan pull-in if a future PDF reveals 5- or 7-watt
        # groups; the schematic remains the same (anchor +/- adjacent
        # unclaimed orphans).
        claimed: list[bool] = [False] * len(data)
        rows_by_sku: dict[str, list[int]] = {}
        for i, (watt, sku) in enumerate(data):
            if sku is None:
                continue
            bucket = rows_by_sku.setdefault(sku, [])
            bucket.append(watt)
            claimed[i] = True
            # Pull in the immediate preceding orphan (line i-1).
            if i - 1 >= 0 and data[i - 1][1] is None and not claimed[i - 1]:
                bucket.append(data[i - 1][0])
                claimed[i - 1] = True
            # Pull in the immediate following orphan (line i+1).
            if i + 1 < len(data) and data[i + 1][1] is None and not claimed[i + 1]:
                bucket.append(data[i + 1][0])
                claimed[i + 1] = True

        matched_wattages: list[int] | None = None
        for sku, wattages in rows_by_sku.items():
            if sku.lower() == target_sku.lower() or sku == target_sku_dash:
                matched_wattages = wattages
                break
        if not matched_wattages:
            # MPN not in this chart (wrong SKU family PDF?). Bail so LLM
            # extraction stays authoritative.
            return {}

        out: dict[str, str] = {}
        # Fixture Wattage: GT shape is "{min}/{mid}/{max}" -- ascending
        # numeric order, slash-joined. Verified on 65-771R3
        # (wattages 150/175/200 -> "150/175/200").
        sorted_watts = sorted(set(matched_wattages))
        out["Fixture Wattage"] = "/".join(str(w) for w in sorted_watts)

        # Color Temperature: strip "K", reformat as "3000 K, 4000 K, 5000 K"
        # (matches GT's exact representation on 65-771R3).
        cct_strs = [
            re.sub(r"K$", " K", label.strip())
            for label in cct_labels
        ]
        out["Color Temperature"] = ", ".join(cct_strs)

        # Filter to labels that exist in the High Bay leaf template so
        # a stray spec-sheet PDF printing some other Satco label can't
        # smuggle in an out-of-template key.
        try:
            template_labels = {s.label for s in get_template(_HB_CLASSPATH)}
            out = {k: v for k, v in out.items() if k in template_labels}
        except Exception:
            pass

        return out
    except Exception:
        return {}
    finally:
        pdf.close()


# ---------------------------------------------------------------------------
# ITEM_FEATURES -- deterministic spec-cell -> feature-bullet template
# ---------------------------------------------------------------------------
# GT's Satco LED ITEM_FEATURES are not scraped prose bullets; they are a
# re-serialization of the same label/value cells we already extract (a
# "feature" in GT Satco-speak is a re-formatted spec cell). Layout A
# (single-SKU sheet) produces exactly:
#   {watt} Watt {shape} LED[ Filament], {finish}, {base} base,
#   {CRI} CRI, {CCT}K, {volts} Volt
# with the " Filament" suffix following the same shape-code rule as
# description_gen._is_filament (T/ST-prefixed shapes are filament-style).
# Layout B (family sheet) produces:
#   {watt} Watt {shape} LED, {color}, {CCT}K, {lumens} Lumens,
#   {volts} Volt, PIR Sensor
# where {color} comes from the GENERAL SPECIFICATIONS "Color: White" line
# and the "PIR Sensor" bullet is detected from the family sheet's own
# marketing text (S11445's page-1 copy is "LED PIR SENSOR LAMPS"). The
# one GT S11445 bullet that is NOT derivable from the PDF ("Non-Dimmable")
# is left out -- never invented. All 3 GT Satco LED rows are reproduced
# 6/6 and 6/6 and 6/7 respectively.

def _layout_a_item_features(out: dict[str, str]) -> list[str]:
    """Feature bullets for single-SKU (Layout A) spec sheets."""
    feats: list[str] = []
    watt = out.get("Wattage")
    shape = out.get("Bulb Shape Code")
    if watt and shape:
        filament = " Filament" if shape.startswith(("T", "ST")) else ""
        feats.append(f"{watt} Watt {shape} LED{filament}")
    finish = out.get("Bulb Finish")
    if finish:
        feats.append(finish)
    base = out.get("Bulb Base")
    if base:
        feats.append(f"{base} base")
    cri = out.get("Color Rendering Index (CRI)")
    if cri:
        feats.append(f"{cri.rstrip('+')} CRI")
    cct = out.get("Color Temperature")
    if cct:
        feats.append(f"{cct}K")
    volts = out.get("Voltage Rating")
    if volts:
        feats.append(f"{volts} Volt")
    return feats


def _layout_b_item_features(out: dict[str, str], text: str) -> list[str]:
    """Feature bullets for multi-SKU (Layout B) family sheets."""
    feats: list[str] = []
    watt = out.get("Wattage")
    shape = out.get("Bulb Shape Code")
    if watt and shape:
        feats.append(f"{watt} Watt {shape} LED")
    color = re.search(r"Color:\s*(\w+)", text)
    if color:
        feats.append(color.group(1))
    cct = out.get("Color Temperature")
    if cct:
        feats.append(f"{cct}K")
    lumens = out.get("Lumens")
    if lumens:
        feats.append(f"{lumens} Lumens")
    volts = out.get("Voltage Rating")
    if volts:
        feats.append(f"{volts} Volt")
    if "PIR" in text.upper():
        feats.append("PIR Sensor")
    return feats


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------

_LED_CLASSPATH = "Electrical>Lamps & Lightings>Light Bulbs>LED Light Bulbs"


def parse_satco_pdf_with_features(pdf_bytes: bytes, mpn: str) -> tuple[dict[str, str], list[str]]:
    """Parse a Satco spec-sheet PDF into the LED Light Bulbs leaf
    template's labels AND the row's deterministic ITEM_FEATURES bullets,
    ready to feed straight into `extractor.reconcile()` (keys are
    template labels, not PDF labels) plus `Descriptions.item_features`.

    Returns ({}, []) on any parsing failure -- the caller's normal
    fallback path (`fallback_extract_attributes` from Part_Desc) still
    applies, so a broken PDF parse degrades cleanly rather than crashing
    the row.
    """
    try:
        pdf = pdfplumber.open(io.BytesIO(pdf_bytes))
    except Exception:
        return {}, []

    try:
        # Detect layout B: any page carries a "GENERAL SPECIFICATIONS"
        # header or a multi-SKU "ITEM SPECIFICATIONS" table row.
        full_text = "\n".join((p.extract_text() or "") for p in pdf.pages)
        is_family = False
        if "GENERAL SPECIFICATIONS" in full_text.upper() or "ITEM SPECIFICATIONS" in full_text.upper():
            is_family = True

        if is_family:
            out = _parse_multi_sku(pdf, mpn)
        else:
            out = _parse_single_sku(pdf)

        if is_family:
            item_features = _layout_b_item_features(out, full_text)
        else:
            item_features = _layout_a_item_features(out)

        # Restrict to labels that actually exist in the LED leaf template
        # (guards against a future Satco sheet printing an unexpectedly
        # new label). Falls back to a non-strict copy if the template
        # lookup itself fails so the caller still gets something usable.
        template_labels = {s.label for s in get_template(_LED_CLASSPATH)}
        if template_labels:
            out = {k: v for k, v in out.items() if k in template_labels}
        return out, item_features
    except Exception:
        return {}, []
    finally:
        pdf.close()


def parse_satco_pdf(pdf_bytes: bytes, mpn: str) -> dict[str, str]:
    """Backward-compatible wrapper: the attribute dict only, no features
    (existing tests / callers that only need the template labels)."""
    out, _features = parse_satco_pdf_with_features(pdf_bytes, mpn)
    return out
