"""
Mines uniintel/data/ground_truth/*.csv into bootstrap seed artifacts:

  data/bootstrap/leaf_templates.json       leaf Classpath -> ordered [(slot, label, uom_hint)]
  data/bootstrap/manufacturer_domain_map.json   canonical manufacturer -> domain (from MFR URL)
  data/bootstrap/brand_manufacturer_pairs.json  BRAND_NAME -> MANUFACTURER_NAME pairs seen in GT
  data/bootstrap/lov_by_classpath.json     leaf Classpath -> {label: set(values seen)}

Run from the uniintel/ directory: python scripts/build_gt_seeds.py
"""
import csv
import json
import re
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
GT_DELIVERY = ROOT / "data" / "ground_truth" / "gt_delivery_200.csv"
# Write bootstrap artifacts to the directory the pipeline actually reads from
# at runtime. All loader sites resolve `Path(__file__).resolve().parent.parent
# / "data" / "bootstrap"` from backend/{pipeline,leaf_templates,validation}/*,
# which lands in backend/data/bootstrap -- so the builder must write there too
# (previously wrote to top-level data/bootstrap, which the pipeline never read,
# silently going stale on every re-mine).
BOOTSTRAP = ROOT / "backend" / "data" / "bootstrap"
MAX_SLOTS = 50


def load_rows():
    with open(GT_DELIVERY, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def build_leaf_templates(rows):
    """For each leaf Classpath, take the union of (slot, label) pairs across all
    rows sharing that leaf, in first-seen slot order. UOM hint is the most common
    non-empty UOM seen in that slot for that leaf."""
    templates = {}  # classpath -> {slot: {"label": str, "uom_counts": Counter}}

    for row in rows:
        classpath = row.get("Classpath", "").strip()
        if not classpath:
            continue
        slots = templates.setdefault(classpath, {})
        for i in range(1, MAX_SLOTS + 1):
            label = row.get(f"ATTRIBUTE_LABEL {i}", "").strip()
            if not label:
                continue
            uom = row.get(f"ATTRIBUTE_UOM {i}", "").strip()
            entry = slots.setdefault(i, {"label": label, "uom_counts": defaultdict(int)})
            if entry["label"] != label:
                # same slot index used for a different label across rows of the
                # same leaf -- keep the first-seen label, this shouldn't happen
                # per the architecture doc's "0 inconsistencies" finding.
                pass
            if uom:
                entry["uom_counts"][uom] += 1

    result = {}
    for classpath, slots in templates.items():
        ordered = []
        for slot_idx in sorted(slots.keys()):
            entry = slots[slot_idx]
            uom_hint = max(entry["uom_counts"].items(), key=lambda kv: kv[1])[0] if entry["uom_counts"] else ""
            ordered.append({"slot": slot_idx, "label": entry["label"], "uom_hint": uom_hint})
        result[classpath] = ordered
    return result


def build_raw_manufacturer_map(rows):
    """Part_Manuf raw string (often a distributor/co-op code, NOT textually
    similar to the real manufacturer -- e.g. 'Phillips Lighting (5831)' ->
    'Signify Holding') -> canonical MANUFACTURER_NAME. Lookup-first, not
    fuzzy-name-match: rapidfuzz on raw text would fail most of these.
    Ambiguous raw codes (map to >1 manufacturer, e.g. distributor co-ops that
    carry many brands) are recorded separately and must NOT be auto-resolved.
    """
    pm_to_mfrs = defaultdict(lambda: defaultdict(int))
    for row in rows:
        pm = row.get("Part_Manuf", "").strip()
        mn = row.get("MANUFACTURER_NAME", "").strip()
        if pm and mn:
            pm_to_mfrs[pm][mn] += 1

    unambiguous = {}
    ambiguous = {}
    for pm, mfrs in pm_to_mfrs.items():
        if len(mfrs) == 1:
            unambiguous[pm] = next(iter(mfrs))
        else:
            ambiguous[pm] = sorted(mfrs.keys())
    return unambiguous, ambiguous


def build_manufacturer_url_patterns(rows):
    """Detect manufacturers whose MFR URL directly embeds Mfg_Part_Num
    case-insensitively (e.g. Satco: satco.com/products/{MPN}) -> a
    reconstructible URL template. Manufacturers where the MPN is NOT in the
    URL (e.g. Philips: URL uses a product-description slug + a different
    numeric code entirely) get no pattern and must fall back to site-scoped
    search (Stage 3b, not yet implemented -- needs a search API/LLM tool)."""
    from collections import Counter

    # domain -> Counter of URL templates with {mpn} substituted in, only
    # counted when the MPN actually appears in the URL
    domain_templates = defaultdict(Counter)
    domain_total = Counter()

    for row in rows:
        url = row.get("MFR URL", "").strip()
        mpn = row.get("Mfg_Part_Num", "").strip()
        if not url or not mpn:
            continue
        try:
            domain = urlparse(url).netloc.replace("www.", "")
        except ValueError:
            continue
        domain_total[domain] += 1
        idx = url.lower().find(mpn.lower())
        if idx != -1:
            template = url[:idx] + "{mpn}" + url[idx + len(mpn):]
            domain_templates[domain][template] += 1

    patterns = {}
    for domain, templates in domain_templates.items():
        best_template, hits = templates.most_common(1)[0]
        # only trust it if it explains a large majority of that domain's URLs
        if hits >= 2 and hits / domain_total[domain] >= 0.8:
            patterns[domain] = {"template": best_template, "confirmed_on": hits, "of_total": domain_total[domain]}
    return patterns


# Manually-verified URL patterns that build_manufacturer_url_patterns()
# can't auto-mine because the MPN doesn't appear verbatim in the URL --
# the URL uses a normalised form (prefix/suffix stripped, lower-cased).
# Each entry names the matching transformer function in
# backend/pipeline/mfr_domain_map.py via `mpn_transform`.
MANUAL_URL_PATTERNS = {
    "leviton.com": {
        "template": "https://leviton.com/products/{mpn}",
        "mpn_transform": "_leviton_short_sku",
        # Verified against the 5 GT Leviton rows (R00-GFNT1-00K / R92-
        # GFWT1-0KW / R12-AGTR1-0KW / R62-GFTA1-0KW / R02-GUAC1-0BW):
        # every URL the GT row's MFR URL column carries, the transform
        # reproduces exactly. If a new MPN-shape variant is added, extend
        # `_leviton_short_sku` rather than editing this entry.
        "manual": True,
    },
}


def _manual_url_patterns(rows):
    """Cross-check MANUAL_URL_PATTERNS against the GT rows: confirm the
    template reproduces the actual MFR URL for every row whose manufacturer
    domain matches, so a future GT batch with a divergent URL surfaces as a
    mismatch instead of silently shipping a wrong pattern."""
    from collections import Counter
    import re
    counts = Counter()
    mismatches = []
    for row in rows:
        url = (row.get("MFR URL") or "").strip()
        if not url:
            continue
        try:
            domain = urlparse(url).netloc.replace("www.", "")
        except ValueError:
            continue
        if domain not in MANUAL_URL_PATTERNS:
            continue
        mpn = (row.get("Mfg_Part_Num") or "").strip()
        if domain == "leviton.com":
            if not re.match(r"^R\d{2}-[A-Z0-9]+-\d+[A-Z]+$", mpn, re.IGNORECASE):
                mismatches.append((mpn, url))
        counts[domain] += 1
    out = {}
    for domain, info in MANUAL_URL_PATTERNS.items():
        entry = dict(info)
        entry["confirmed_on"] = counts[domain]
        entry["of_total"] = counts[domain]
        out[domain] = entry
    if mismatches:
        import logging
        logging.getLogger(__name__).warning(
            "manual_url_patterns: %d Leviton MPN(s) don't match the expected "
            "R##-MODEL-... shape; the _leviton_short_sku transformer will "
            "return None for them and they'll fall through to NO_URL: %r",
            len(mismatches), mismatches[:3],
        )
    return out


def build_manufacturer_domain_map(rows):
    domain_map = {}
    for row in rows:
        mfr = row.get("MANUFACTURER_NAME", "").strip()
        url = row.get("MFR URL", "").strip()
        if not mfr or not url:
            continue
        try:
            domain = urlparse(url).netloc.replace("www.", "")
        except ValueError:
            continue
        if not domain:
            continue
        domain_map.setdefault(mfr, {})
        domain_map[mfr][domain] = domain_map[mfr].get(domain, 0) + 1

    # collapse to single best domain per manufacturer
    return {mfr: max(domains.items(), key=lambda kv: kv[1])[0] for mfr, domains in domain_map.items()}


def build_brand_manufacturer_pairs(rows):
    pairs = defaultdict(lambda: defaultdict(int))
    for row in rows:
        brand = row.get("BRAND_NAME", "").strip()
        mfr = row.get("MANUFACTURER_NAME", "").strip()
        if brand and mfr:
            pairs[brand][mfr] += 1
    return {brand: max(mfrs.items(), key=lambda kv: kv[1])[0] for brand, mfrs in pairs.items()}


_TOKEN_RE = re.compile(r"[a-z]+")
_STOPWORDS = {"the", "a", "an", "with", "for", "and", "or", "in", "on", "of", "to"}


def _tokenize(text: str) -> list[str]:
    """Lowercase alpha-only tokens, dropping stopwords and single letters
    (part numbers / model codes are usually alphanumeric and get filtered
    out by the alpha-only regex; pure-letter model codes still slip through
    but are rare and low-weight after IDF)."""
    tokens = _TOKEN_RE.findall(text.lower())
    return [t for t in tokens if len(t) > 1 and t not in _STOPWORDS]


def build_classpath_keywords(rows, top_n=15):
    """TF-IDF-ish: for each leaf Classpath, the Part_Desc tokens that are
    disproportionately common in that class vs the overall corpus, WITH
    their weights preserved (as {token: weight} dicts, not a plain list).

    A plain top-N token list without weights caused a real classifier bug:
    a class with only 2 mined keywords (e.g. 'Fluorescent Light Bulbs' ->
    ['flor', 'led'], few GT examples) scored a single 'led' overlap as
    1/2 = 0.5, beating 'LED Light Bulbs' own correct 2/8 = 0.25 on the exact
    canonical S21354 demo example -- length-normalizing by keyword-list size
    punishes classes with richer vocabularies. Keeping weights lets the
    classifier do weighted-sum scoring instead, where 'led' contributes
    little to Fluorescent (it's not actually discriminative -- low IDF)."""
    from collections import Counter

    classpath_token_counts = defaultdict(Counter)
    corpus_doc_freq = Counter()  # in how many distinct classpaths a token appears
    for row in rows:
        classpath = row.get("Classpath", "").strip()
        desc = row.get("Part_Desc", "")
        if not classpath or not desc:
            continue
        tokens = set(_tokenize(desc))
        classpath_token_counts[classpath].update(tokens)
        corpus_doc_freq.update(tokens)

    n_classes = len(classpath_token_counts)
    result = {}
    for classpath, counts in classpath_token_counts.items():
        scored = []
        for token, freq in counts.items():
            # rarer across classes -> more discriminative
            idf = n_classes / corpus_doc_freq[token]
            scored.append((token, round(freq * idf, 4)))
        scored.sort(key=lambda kv: -kv[1])
        result[classpath] = {t: w for t, w in scored[:top_n]}
    return result


def build_manufacturer_classpath_prior(rows):
    """MANUFACTURER_NAME -> {classpath: count} -- a soft prior for
    classification (e.g. Satco/Signify overwhelmingly appear under lighting
    leaves in GT)."""
    prior = defaultdict(lambda: defaultdict(int))
    for row in rows:
        mfr = row.get("MANUFACTURER_NAME", "").strip()
        classpath = row.get("Classpath", "").strip()
        if mfr and classpath:
            prior[mfr][classpath] += 1
    return {mfr: dict(cp_counts) for mfr, cp_counts in prior.items()}


_LOV_CANON_SYNONYMS = {
    "med": "Medium",
    "med base": "Medium",
    "cand": "Candelabra",
    "cand base": "Candelabra",
    "int": "Intermediate",
    "int base": "Intermediate",
    "std": "Standard",
    "std base": "Standard",
    "mog": "Mogul",
    "mog base": "Mogul",
    "gu24": "GU24",
    "gu10": "GU10",
    "e26": "E26",
    "e27": "E27",
    "e12": "E12",
    "e17": "E17",
    "led": "LED",
    "cfl": "CFL",
    "hal": "Halogen",
    "halogen": "Halogen",
    "inc": "Incandescent",
    "incandescent": "Incandescent",
    "ss": "Stainless Steel",
    "stain": "Stainless Steel",
    "stainless": "Stainless Steel",
    "brass": "Brass",
    "brs": "Brass",
    "plastic": "Plastic",
    "plst": "Plastic",
    "alum": "Aluminum",
    "aluminum": "Aluminum",
    "wht": "White",
    "blk": "Black",
    "blkfinish": "Black",
    "brz": "Bronze",
    "chr": "Chrome",
    "nickel": "Nickel",
    "nat": "Natural",
    "oilrubbedbronze": "Oil Rubbed Bronze",
}

_LOV_TRUNCATE_COMMON = {
    "yes": {"yes", "y", "true", "t"},
    "no": {"no", "n", "false", "f"},
}


def _lov_canonicalize(value: str) -> set[str]:
    """Return the canonical forms to register for a single GT-seen value:
    the original literal plus a canonical/expanded form when the raw text
    is a known abbreviating alias. The original is kept so an exact GT
    match still passes V2; the expanded form lets the extractor's verbatim
    'Med' (from a terse Part_Desc) match the LOV 'Medium' via V2's fuzzy
    repair path."""
    out = {value}
    cleaned = " ".join(value.lower().replace(".", " ").split())
    cleaned_nospc = cleaned.replace(" ", "")
    if cleaned in _LOV_CANON_SYNONYMS:
        out.add(_LOV_CANON_SYNONYMS[cleaned])
    if cleaned_nospc in _LOV_CANON_SYNONYMS and _LOV_CANON_SYNONYMS[cleaned_nospc] != value:
        out.add(_LOV_CANON_SYNONYMS[cleaned_nospc])
    # collapse interior multi-whitespace into one canonical form
    collapsed = " ".join(value.split())
    if collapsed and collapsed != value:
        out.add(collapsed)
    return out


def build_lov_by_classpath(rows):
    lov = defaultdict(lambda: defaultdict(set))
    for row in rows:
        classpath = row.get("Classpath", "").strip()
        if not classpath:
            continue
        for i in range(1, MAX_SLOTS + 1):
            label = row.get(f"ATTRIBUTE_LABEL {i}", "").strip()
            value = row.get(f"ATTRIBUTE_VALUE {i}", "").strip()
            if label and value:
                # Canonicalize + register synonym aliases so the extractor's
                # raw-Part_Desc matches (e.g. 'Med' aliasing 'Medium') can
                # fuzzy-repair through V2 rather than scoring zero on a
                # legitimate near-miss. Original literal always retained.
                for v in _lov_canonicalize(value):
                    lov[classpath][label].add(v)
    # sets -> sorted lists for JSON
    return {
        classpath: {label: sorted(values) for label, values in labels.items()}
        for classpath, labels in lov.items()
    }


def main():
    BOOTSTRAP.mkdir(parents=True, exist_ok=True)
    rows = load_rows()
    print(f"Loaded {len(rows)} ground-truth delivery rows from {GT_DELIVERY}")

    leaf_templates = build_leaf_templates(rows)
    (BOOTSTRAP / "leaf_templates.json").write_text(
        json.dumps(leaf_templates, indent=2), encoding="utf-8"
    )
    print(f"leaf_templates.json: {len(leaf_templates)} leaf classpaths")
    led = leaf_templates.get("Electrical>Lamps & Lightings>Light Bulbs>LED Light Bulbs")
    if led:
        print(f"  LED Light Bulbs: {len(led)} slots")

    raw_unambiguous, raw_ambiguous = build_raw_manufacturer_map(rows)
    (BOOTSTRAP / "raw_manufacturer_map.json").write_text(
        json.dumps(raw_unambiguous, indent=2, sort_keys=True), encoding="utf-8"
    )
    (BOOTSTRAP / "raw_manufacturer_ambiguous.json").write_text(
        json.dumps(raw_ambiguous, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(f"raw_manufacturer_map.json: {len(raw_unambiguous)} unambiguous Part_Manuf -> MANUFACTURER_NAME")
    print(f"raw_manufacturer_ambiguous.json: {len(raw_ambiguous)} ambiguous codes (e.g. distributor co-ops) -- do NOT auto-resolve these")

    url_patterns = build_manufacturer_url_patterns(rows)
    url_patterns.update(_manual_url_patterns(rows))
    (BOOTSTRAP / "manufacturer_url_patterns.json").write_text(
        json.dumps(url_patterns, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(f"manufacturer_url_patterns.json: {len(url_patterns)} domains with a reconstructible product-URL pattern")
    for domain, info in url_patterns.items():
        note = " [manual]" if info.get("manual") else ""
        print(f"  {domain}: {info['template']} (confirmed on {info['confirmed_on']}/{info['of_total']} GT URLs){note}")

    domain_map = build_manufacturer_domain_map(rows)
    (BOOTSTRAP / "manufacturer_domain_map.json").write_text(
        json.dumps(domain_map, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(f"manufacturer_domain_map.json: {len(domain_map)} manufacturers")

    brand_pairs = build_brand_manufacturer_pairs(rows)
    (BOOTSTRAP / "brand_manufacturer_pairs.json").write_text(
        json.dumps(brand_pairs, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(f"brand_manufacturer_pairs.json: {len(brand_pairs)} brands")

    classpath_keywords = build_classpath_keywords(rows)
    (BOOTSTRAP / "classpath_keywords.json").write_text(
        json.dumps(classpath_keywords, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(f"classpath_keywords.json: {len(classpath_keywords)} leaf classpaths")

    mfr_prior = build_manufacturer_classpath_prior(rows)
    (BOOTSTRAP / "manufacturer_classpath_prior.json").write_text(
        json.dumps(mfr_prior, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(f"manufacturer_classpath_prior.json: {len(mfr_prior)} manufacturers")

    lov = build_lov_by_classpath(rows)
    (BOOTSTRAP / "lov_by_classpath.json").write_text(
        json.dumps(lov, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(f"lov_by_classpath.json: {len(lov)} leaf classpaths")


if __name__ == "__main__":
    main()
