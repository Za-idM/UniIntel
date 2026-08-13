"use client";

import { useEffect, useState } from "react";
import { getEvaluation, type EvaluateResponse, type JobStatusResponse } from "@/lib/api";
import { StatCard, MiniBar } from "@/components/StatCard";
import { MatchBadge } from "@/components/Badges";

const DESC_LABELS: Record<string, string> = {
  invoice_desc: "Invoice",
  mobile_desc: "Mobile",
  short_desc: "Short",
  retail_desc: "Retail",
};

export default function EvaluateView({
  jobId,
  jobStatus,
  onSelectProduct,
}: {
  jobId: string | null;
  jobStatus: JobStatusResponse | null;
  onSelectProduct: (id: string) => void;
}) {
  const [evalData, setEvalData] = useState<EvaluateResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Only score once the job is done -- evaluate() reads whatever's in the
  // products table right now, and re-fetching every poll tick while rows
  // are still landing would just show a misleadingly low, constantly
  // shifting accuracy.
  useEffect(() => {
    if (!jobId || jobStatus?.status !== "DONE") return;
    setLoading(true);
    setError(null);
    getEvaluation(jobId)
      .then(setEvalData)
      .catch((err) => setError(err instanceof Error ? err.message : "failed to load evaluation"))
      .finally(() => setLoading(false));
  }, [jobId, jobStatus?.status]);

  if (!jobId) {
    return (
      <div className="mx-auto max-w-5xl px-6 py-10 text-left">
        <p className="text-sm text-ink-500">Process a file on the Upload tab to see accuracy against ground truth.</p>
      </div>
    );
  }

  if (jobStatus && jobStatus.status !== "DONE" && jobStatus.status !== "FAILED") {
    return (
      <div className="mx-auto max-w-5xl px-6 py-10 text-left">
        <p className="text-sm text-ink-500">
          Job still processing ({jobStatus.processed_rows}/{jobStatus.total_rows} rows) &mdash; evaluation runs once
          it finishes. Check the Upload tab for live progress.
        </p>
      </div>
    );
  }

  if (jobStatus?.status === "FAILED") {
    return (
      <div className="mx-auto max-w-5xl px-6 py-10 text-left">
        <p className="text-sm text-status-miss">Job failed: {jobStatus.error || "unknown error"}</p>
      </div>
    );
  }

  if (loading || !evalData) {
    return (
      <div className="mx-auto max-w-5xl px-6 py-10 text-left">
        <p className="text-sm text-ink-500">{error || "Scoring against ground truth…"}</p>
      </div>
    );
  }

  if (!evalData.summary) {
    return (
      <div className="mx-auto max-w-5xl px-6 py-10 text-left">
        <p className="text-sm text-ink-500">
          {evalData.note || "No rows in this job matched a ground-truth part number."}
        </p>
      </div>
    );
  }

  const { summary, rows } = evalData;

  return (
    <div className="mx-auto max-w-5xl px-6 py-5">
      <div className="mb-3">
        <h1 className="text-[14px] font-semibold text-ink900">Evaluation vs. Ground Truth</h1>
        <p className="mt-0.5 text-[12.5px] text-ink-500">
          {summary.rows_evaluated} of {evalData.rows_total} processed rows matched a labeled ground-truth part
          number{evalData.rows_unscored > 0 && ` (${evalData.rows_unscored} unmatched, not scored)`}.
        </p>
      </div>

      <div className="mb-3 grid grid-cols-3 gap-3">
        <StatCard label="Classpath Accuracy" stat={summary.classpath_accuracy} />
        <StatCard label="Manufacturer Accuracy" stat={summary.manufacturer_accuracy} />
        <StatCard label="Attribute Accuracy" stat={summary.attribute_accuracy} />
      </div>

      <div className="mb-3 rounded border border-paper-300 bg-white px-4 py-2.5">
        <div className="mb-2 font-mono text-[11px] font-semibold uppercase tracking-wide text-ink-500">
          description match rates
        </div>
        <div className="grid grid-cols-2 gap-x-8 gap-y-2.5">
          {Object.entries(summary.description_match_rates).map(([key, stat]) => (
            <MiniBar key={key} label={DESC_LABELS[key] || key} stat={stat} />
          ))}
        </div>
      </div>

      <div>
        <h2 className="mb-1.5 font-mono text-[11px] font-semibold uppercase tracking-wide text-ink-500">
          per-row breakdown
        </h2>
        <div className="overflow-x-auto rounded border border-paper-300 bg-white">
          <table className="w-full border-collapse text-left text-[12px]">
            <thead>
              <tr className="border-b border-paper-300 bg-paper-100 text-ink-500">
                <th className="px-3 py-1.5 font-mono text-[11px] font-semibold uppercase tracking-wide">MPN</th>
                <th className="px-3 py-1.5 font-mono text-[11px] font-semibold uppercase tracking-wide">
                  Classpath
                </th>
                <th className="px-3 py-1.5 font-mono text-[11px] font-semibold uppercase tracking-wide">
                  Manufacturer
                </th>
                <th className="px-3 py-1.5 font-mono text-[11px] font-semibold uppercase tracking-wide">
                  Attributes
                </th>
                {Object.keys(DESC_LABELS).map((k) => (
                  <th key={k} className="px-3 py-1.5 font-mono text-[11px] font-semibold uppercase tracking-wide">
                    {DESC_LABELS[k]}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr
                  key={r.product_id}
                  onClick={() => onSelectProduct(r.product_id)}
                  className="cursor-pointer border-b border-paper-200 transition-colors last:border-0 hover:bg-paper-100"
                >
                  <td className="whitespace-nowrap px-3 py-1.5 font-mono text-ink900">{r.mfg_part_num}</td>
                  <td className="px-3 py-1.5">
                    <MatchBadge status={r.classpath_match ? "EXACT" : "MISS"} />
                  </td>
                  <td className="px-3 py-1.5">
                    <MatchBadge status={r.manufacturer_match ? "EXACT" : "MISS"} />
                  </td>
                  <td className="whitespace-nowrap px-3 py-1.5 font-mono text-ink-500">
                    {r.attribute_field_correct}/{r.attribute_field_total}
                  </td>
                  {Object.keys(DESC_LABELS).map((k) => (
                    <td key={k} className="px-3 py-1.5">
                      <MatchBadge status={r.description_status[k] || "NO_GT"} />
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
