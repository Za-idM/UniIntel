"use client";

import { useCallback, useRef, useState } from "react";
import { parseCsvPreview } from "@/lib/csv";
import { processFile, type JobStatusResponse, type ProcessResponse, type ProductRow } from "@/lib/api";
import { ConfidenceBadge } from "@/components/Badges";

export default function UploadView({
  onStartJob,
  onSelectProduct,
  jobStatus,
  jobProducts,
}: {
  onStartJob: (res: ProcessResponse) => void;
  onSelectProduct: (id: string) => void;
  jobStatus: JobStatusResponse | null;
  jobProducts: ProductRow[];
}) {
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<{ headers: string[]; rows: string[][] } | null>(null);
  const [dragOver, setDragOver] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const loadFile = useCallback(async (f: File) => {
    setFile(f);
    setError(null);
    const text = await f.text();
    setPreview(parseCsvPreview(text, 6));
  }, []);

  const handleDrop = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault();
      setDragOver(false);
      const f = e.dataTransfer.files?.[0];
      if (f) loadFile(f);
    },
    [loadFile]
  );

  const handleProcess = async () => {
    if (!file) return;
    setSubmitting(true);
    setError(null);
    try {
      const res = await processFile(file);
      onStartJob(res);
    } catch (err) {
      setError(err instanceof Error ? err.message : "processing failed");
    } finally {
      setSubmitting(false);
    }
  };

  const running = jobStatus?.status === "RUNNING" || jobStatus?.status === "PENDING";
  const pct = jobStatus && jobStatus.total_rows > 0 ? (100 * jobStatus.processed_rows) / jobStatus.total_rows : 0;

  return (
    <div className="mx-auto max-w-5xl px-6 py-5">
      <div className="mb-3">
        <h1 className="text-[14px] font-semibold text-ink900">Upload &amp; Process</h1>
        <p className="mt-0.5 text-[12.5px] text-ink-500">
          Distributor CSV in, enriched catalog rows out. Runs classify &rarr; enrich &rarr; extract &rarr; describe
          for every row.
        </p>
      </div>

      <div
        onDragOver={(e) => {
          e.preventDefault();
          setDragOver(true);
        }}
        onDragLeave={() => setDragOver(false)}
        onDrop={handleDrop}
        onClick={() => inputRef.current?.click()}
        className={`flex cursor-pointer items-center justify-between gap-3 rounded border px-4 py-2.5 transition-colors ${
          dragOver
            ? "border-accent-600 bg-accent-50"
            : "border-ink-500/25 bg-white hover:border-accent-600 hover:bg-accent-50/40"
        }`}
      >
        <input
          ref={inputRef}
          type="file"
          accept=".csv"
          className="hidden"
          onChange={(e) => {
            const f = e.target.files?.[0];
            if (f) loadFile(f);
          }}
        />
        <div className="flex items-baseline gap-2 text-[13px]">
          <span className="font-medium text-ink900">
            {file ? file.name : "Drop a CSV file here, or click to browse"}
          </span>
          <span className="font-mono text-[11px] text-ink-400">
            {file ? `${(file.size / 1024).toFixed(1)} KB` : "Mfg_Part_Num · Part_Desc · Part_Manuf · brand cols"}
          </span>
        </div>
        {!file && <span className="shrink-0 text-[11px] font-medium text-accent-600">browse &rarr;</span>}
      </div>

      {preview && preview.rows.length > 0 && (
        <div className="mt-4">
          <div className="mb-1.5 flex items-center justify-between">
            <h2 className="font-mono text-[11px] font-semibold uppercase tracking-wide text-ink-500">
              preview &middot; first {preview.rows.length} rows
            </h2>
            <button
              onClick={handleProcess}
              disabled={submitting || running}
              className="rounded bg-accent-600 px-4 py-1.5 text-[13px] font-semibold text-white transition-colors hover:bg-accent-700 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {submitting ? "Starting…" : running ? "Processing…" : "Process"}
            </button>
          </div>
          <div className="overflow-x-auto rounded border border-paper-300 bg-white">
            <table className="w-full border-collapse text-left text-[12px]">
              <thead>
                <tr className="border-b border-paper-300 bg-paper-100">
                  {preview.headers.map((h) => (
                    <th
                      key={h}
                      className="whitespace-nowrap px-3 py-1.5 font-mono text-[11px] font-semibold uppercase tracking-wide text-ink-500"
                    >
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {preview.rows.map((row, i) => (
                  <tr key={i} className="border-b border-paper-200 last:border-0">
                    {row.map((cell, j) => (
                      <td key={j} className="whitespace-nowrap px-3 py-1 font-mono text-ink900">
                        {cell || <span className="text-ink-400">&mdash;</span>}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {error && (
        <div className="mt-3 rounded border border-status-miss/30 bg-status-missBg px-3 py-2 text-[13px] text-status-miss">
          {error}
        </div>
      )}

      {jobStatus && (
        <div className="mt-5">
          <div className="mb-1.5 flex items-baseline justify-between">
            <h2 className="font-mono text-[11px] font-semibold uppercase tracking-wide text-ink-500">
              job {jobStatus.id.slice(0, 8)} &middot; {jobStatus.processed_rows}/{jobStatus.total_rows} rows
              processed
            </h2>
            <span
              className={`font-mono text-[11px] font-semibold uppercase tracking-wide ${
                jobStatus.status === "FAILED"
                  ? "text-status-miss"
                  : jobStatus.status === "DONE"
                  ? "text-status-exact"
                  : "text-accent-600"
              }`}
            >
              {jobStatus.status}
            </span>
          </div>

          {running && (
            <div className="mb-3 h-1.5 w-full overflow-hidden rounded-full bg-paper-200">
              <div
                className="h-full rounded-full bg-accent-600 transition-all duration-500"
                style={{ width: `${Math.max(3, pct)}%` }}
              />
            </div>
          )}

          {jobStatus.status === "FAILED" && (
            <div className="mb-3 rounded border border-status-miss/30 bg-status-missBg px-3 py-2 text-[13px] text-status-miss">
              {jobStatus.error || "Processing failed."}
            </div>
          )}

          {jobProducts.length > 0 && (
            <div className="overflow-hidden rounded border border-paper-300 bg-white">
              <table className="w-full border-collapse text-left text-[13px]">
                <thead>
                  <tr className="border-b border-paper-300 bg-paper-100 text-ink-500">
                    <th className="px-3 py-1.5 font-mono text-[11px] font-semibold uppercase tracking-wide">MPN</th>
                    <th className="px-3 py-1.5 font-mono text-[11px] font-semibold uppercase tracking-wide">
                      Manufacturer
                    </th>
                    <th className="px-3 py-1.5 font-mono text-[11px] font-semibold uppercase tracking-wide">
                      Classpath
                    </th>
                    <th className="px-3 py-1.5 font-mono text-[11px] font-semibold uppercase tracking-wide">
                      Confidence
                    </th>
                    <th className="px-3 py-1.5" />
                  </tr>
                </thead>
                <tbody>
                  {jobProducts.map((p) => (
                    <tr
                      key={p.id}
                      className="cursor-pointer border-b border-paper-200 transition-colors last:border-0 hover:bg-paper-100"
                      onClick={() => onSelectProduct(p.id)}
                    >
                      <td className="px-3 py-1.5 font-mono text-ink900">{p.mfg_part_num}</td>
                      <td className="px-3 py-1.5 text-ink900">
                        {p.manufacturer_name || <span className="text-ink-400">unresolved</span>}
                      </td>
                      <td className="max-w-xs truncate px-3 py-1.5 text-ink-500" title={p.classpath || ""}>
                        {p.classpath?.split(">").pop() || <span className="text-ink-400">&mdash;</span>}
                      </td>
                      <td className="px-3 py-1.5">
                        <ConfidenceBadge band={p.confidence_band} score={p.confidence} />
                      </td>
                      <td className="px-3 py-1.5 text-right text-accent-600">view &rarr;</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
