"use client";

import { useState } from "react";
import type { Descriptions } from "@/lib/api";

const FIELDS: { key: keyof Descriptions; label: string; format: string; generated: boolean }[] = [
  { key: "invoice_desc", label: "Invoice", format: "compressed UOM · ≤40 chars", generated: false },
  { key: "mobile_desc", label: "Mobile", format: "compressed UOM", generated: false },
  { key: "short_desc", label: "Short", format: "spaced UOM", generated: false },
  { key: "retail_desc", label: "Retail", format: "spaced UOM", generated: false },
  { key: "long_desc1", label: "Long", format: "generated prose", generated: true },
  { key: "marketing_description", label: "Marketing", format: "generated prose", generated: true },
];

export default function DescriptionTabs({ descriptions }: { descriptions: Descriptions }) {
  const [active, setActive] = useState<keyof Descriptions>("invoice_desc");
  const activeField = FIELDS.find((f) => f.key === active)!;
  const value = descriptions[active];

  return (
    <div>
      <div className="flex items-center gap-1 border-b border-paper-300">
        {FIELDS.map((f) => (
          <button
            key={f.key}
            onClick={() => setActive(f.key)}
            className={`relative px-2.5 py-1.5 text-[12px] font-medium transition-colors ${
              active === f.key ? "text-ink900" : "text-ink-500 hover:text-ink900"
            }`}
          >
            {f.label}
            {active === f.key && <span className="absolute inset-x-1.5 -bottom-px h-0.5 bg-accent-600" />}
          </button>
        ))}
      </div>
      <div className="px-1 pt-2">
        <div className="mb-1 flex items-center gap-2">
          <span className="font-mono text-[10px] font-semibold uppercase tracking-wide text-ink-400">
            {activeField.format}
          </span>
          {activeField.generated && (
            <span className="inline-flex items-center rounded border border-accent-400/40 bg-accent-50 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-accent-700">
              LLM generated
            </span>
          )}
        </div>
        <p className={`text-[13px] leading-relaxed text-ink900 ${!activeField.generated ? "font-mono" : ""}`}>
          {value || <span className="text-ink-400">Not generated for this row.</span>}
        </p>
      </div>
    </div>
  );
}
