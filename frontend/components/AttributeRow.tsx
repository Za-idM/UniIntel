"use client";

import { useState } from "react";
import type { AttributeValue } from "@/lib/api";
import { OriginTag } from "@/components/Badges";

export default function AttributeRow({ attr }: { attr: AttributeValue }) {
  const [open, setOpen] = useState(false);
  const hasEvidence = Boolean(attr.value && (attr.source_url || attr.evidence_text));
  const empty = !attr.value;

  return (
    <>
      <tr
        onClick={() => hasEvidence && setOpen((v) => !v)}
        className={`border-b border-paper-200 last:border-0 ${
          hasEvidence ? "cursor-pointer hover:bg-paper-100" : ""
        } ${open ? "bg-paper-100" : ""}`}
      >
        <td className="w-8 px-3 py-1.5 text-center text-ink-400">
          {hasEvidence ? (
            <span className={`inline-block transition-transform ${open ? "rotate-90" : ""}`}>&rsaquo;</span>
          ) : null}
        </td>
        <td className="whitespace-nowrap px-3 py-1.5 text-ink-500">{attr.label}</td>
        <td className={`px-3 py-1.5 font-mono ${empty ? "text-ink-400" : "text-ink900"}`}>
          {attr.value ? (
            <>
              {attr.value}
              {attr.uom && <span className="ml-1 text-ink-400">{attr.uom}</span>}
            </>
          ) : (
            "—"
          )}
        </td>
        <td className="px-3 py-1.5">
          <OriginTag origin={attr.origin} />
        </td>
      </tr>
      {open && hasEvidence && (
        <tr className="border-b border-paper-200 bg-paper-100 last:border-0">
          <td />
          <td colSpan={3} className="px-3 pb-2 pt-0">
            <div className="rounded border border-paper-300 bg-white px-3 py-2.5">
              {attr.source_url && (
                <div className="mb-2 flex items-center gap-2 text-[12px]">
                  <span className="font-semibold uppercase tracking-wide text-ink-500">Source</span>
                  <a
                    href={attr.source_url}
                    target="_blank"
                    rel="noreferrer"
                    className="truncate font-mono text-accent-700 underline decoration-accent-400/50 underline-offset-2 hover:text-accent-600"
                  >
                    {attr.source_url}
                  </a>
                </div>
              )}
              {attr.evidence_text && (
                <div className="text-[12px]">
                  <div className="mb-1 font-semibold uppercase tracking-wide text-ink-500">Evidence</div>
                  <div className="rounded bg-ink-800 px-3 py-2 font-mono text-[12px] leading-relaxed text-paper-100">
                    {attr.evidence_text}
                  </div>
                </div>
              )}
              {attr.confidence !== null && attr.confidence !== undefined && (
                <div className="mt-2 flex items-center gap-2 text-[12px]">
                  <span className="font-semibold uppercase tracking-wide text-ink-500">Confidence</span>
                  <span className="font-mono text-ink900">{attr.confidence.toFixed(0)}</span>
                </div>
              )}
            </div>
          </td>
        </tr>
      )}
    </>
  );
}
