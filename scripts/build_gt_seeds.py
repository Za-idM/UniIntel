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
BOOTSTRAP = ROOT / "data" / "bootstrap"
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
                lov[classpath][label].add(value)
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
    (BOOTSTRAP / "manufacturer_url_patterns.json").write_text(
        json.dumps(url_patterns, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(f"manufacturer_url_patterns.json: {len(url_patterns)} domains with a reconstructible product-URL pattern")
    for domain, info in url_patterns.items():
        print(f"  {domain}: {info['template']} (confirmed on {info['confirmed_on']}/{info['of_total']} GT URLs)")

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
