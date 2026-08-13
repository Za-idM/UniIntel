"""
Leave-one-out cross-validation of the rule-based classifier baseline
(backend/pipeline/classifier.py) against the 200-row GT set. Rebuilds the
keyword/prior index excluding each row before classifying it, so the
reported accuracy isn't circular (train-on-everything, test-on-everything
would be meaningless self-recall).

Run from the uniintel/ directory: python scripts/evaluate_classifier.py
"""
import csv
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from build_gt_seeds import build_classpath_keywords, build_manufacturer_classpath_prior  # noqa: E402

GT_DELIVERY = ROOT / "data" / "ground_truth" / "gt_delivery_200.csv"

_TOKEN_RE = re.compile(r"[a-z]+")
_STOPWORDS = {"the", "a", "an", "with", "for", "and", "or", "in", "on", "of", "to"}


def _tokenize(text):
    return {t for t in _TOKEN_RE.findall(text.lower()) if len(t) > 1 and t not in _STOPWORDS}


def classify_with_index(part_desc, manufacturer_name, keywords, mfr_prior):
    desc_tokens = _tokenize(part_desc)
    scores = {}
    for classpath, weighted_kw in keywords.items():
        matched_weight = sum(weight for token, weight in weighted_kw.items() if token in desc_tokens)
        if matched_weight:
            scores[classpath] = matched_weight
    if manufacturer_name and manufacturer_name in mfr_prior:
        total = sum(mfr_prior[manufacturer_name].values())
        for classpath, count in mfr_prior[manufacturer_name].items():
            scores[classpath] = scores.get(classpath, 0.0) + 0.5 * (count / total)
    if not scores:
        return None
    return max(scores.items(), key=lambda kv: kv[1])[0]


def main():
    with open(GT_DELIVERY, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    classpath_counts = Counter(r["Classpath"].strip() for r in rows if r["Classpath"].strip())

    correct = 0
    total = 0
    no_prediction = 0
    per_class = defaultdict(lambda: [0, 0])  # classpath -> [correct, total]
    singleton_skipped = 0

    for i, row in enumerate(rows):
        true_classpath = row["Classpath"].strip()
        if not true_classpath:
            continue
        if classpath_counts[true_classpath] < 2:
            # a class with only 1 GT example can never be predicted under
            # leave-one-out (its only training signal is held out) -- not a
            # classifier failure, just an unanswerable question. Reported
            # separately rather than counted against accuracy.
            singleton_skipped += 1
            continue

        held_out_rows = rows[:i] + rows[i + 1:]
        keywords = build_classpath_keywords(held_out_rows)
        mfr_prior = build_manufacturer_classpath_prior(held_out_rows)

        predicted = classify_with_index(row["Part_Desc"], row.get("MANUFACTURER_NAME", "").strip(), keywords, mfr_prior)

        total += 1
        per_class[true_classpath][1] += 1
        if predicted is None:
            no_prediction += 1
        elif predicted == true_classpath:
            correct += 1
            per_class[true_classpath][0] += 1

    print(f"Leave-one-out accuracy (classes with >=2 GT examples, n={total}): {correct}/{total} = {100*correct/total:.1f}%")
    print(f"No prediction at all: {no_prediction}")
    print(f"Singleton classes skipped (unanswerable under LOO): {singleton_skipped}")
    print()
    led = "Electrical>Lamps & Lightings>Light Bulbs>LED Light Bulbs"
    c, t = per_class[led]
    print(f"LED Light Bulbs (demo category): {c}/{t} = {100*c/t:.1f}%" if t else "LED Light Bulbs: no data")


if __name__ == "__main__":
    main()
