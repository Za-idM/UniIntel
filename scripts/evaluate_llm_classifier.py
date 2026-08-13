"""
Measures backend/pipeline/llm_client.GroqClassifierClient accuracy against
a sample of the 200-row GT set. Unlike the rule-based baseline's
leave-one-out eval, this is a genuinely held-out test -- the LLM was never
trained on our GT data.

Async, semaphore-bounded per the locked build plan (Section 8: start 3-4
concurrent workers). Requires GROQ_API_KEY in backend/.env.

Run from the uniintel/ directory: python scripts/evaluate_llm_classifier.py [sample_size]
"""
import asyncio
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from pipeline.classifier import llm_classify  # noqa: E402
from pipeline.llm_client import GroqClassifierClient  # noqa: E402

GT_DELIVERY = ROOT / "data" / "ground_truth" / "gt_delivery_200.csv"
CONCURRENCY = 2  # free-tier TPM budget is tight; see llm_client.py shortlisting comment


async def classify_one(sem, client, row, results):
    async with sem:
        result = await llm_classify(row["Part_Desc"], row.get("MANUFACTURER_NAME", "").strip(), client)
        results.append((row["Classpath"].strip(), result.classpath, row["Part_Desc"]))


async def main(sample_size: int):
    with open(GT_DELIVERY, encoding="utf-8-sig") as f:
        rows = [r for r in csv.DictReader(f) if r["Classpath"].strip()]

    # stratified-ish: all LED bulb rows (demo category) + a spread of the rest
    led_rows = [r for r in rows if r["Classpath"].endswith("LED Light Bulbs")]
    other_rows = [r for r in rows if not r["Classpath"].endswith("LED Light Bulbs")]
    step = max(1, len(other_rows) // max(1, sample_size - len(led_rows)))
    sample = led_rows + other_rows[::step][: sample_size - len(led_rows)]

    client = GroqClassifierClient()
    sem = asyncio.Semaphore(CONCURRENCY)
    results = []
    await asyncio.gather(*(classify_one(sem, client, row, results) for row in sample))

    correct = sum(1 for true, pred, _ in results if true == pred)
    print(f"LLM classification accuracy: {correct}/{len(results)} = {100*correct/len(results):.1f}%")

    led_results = [r for r in results if r[0].endswith("LED Light Bulbs")]
    led_correct = sum(1 for true, pred, _ in led_results if true == pred)
    print(f"LED Light Bulbs subset: {led_correct}/{len(led_results)} = {100*led_correct/len(led_results):.1f}%")

    print("\nMisclassifications:")
    for true, pred, desc in results:
        if true != pred:
            print(f"  {desc[:50]!r}: expected {true!r}, got {pred!r}")


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 50
    asyncio.run(main(n))
