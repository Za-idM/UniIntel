"use client";

export type Tab = "upload" | "product" | "evaluate";

const TABS: { id: Tab; label: string }[] = [
  { id: "upload", label: "Upload" },
  { id: "product", label: "Product Detail" },
  { id: "evaluate", label: "Evaluation" },
];

export default function Header({
  active,
  onChange,
  jobId,
}: {
  active: Tab;
  onChange: (tab: Tab) => void;
  jobId: string | null;
}) {
  return (
    <header className="sticky top-0 z-20 flex h-12 items-center justify-between border-b border-ink-600 bg-ink-800 px-4 text-paper">
      <div className="flex h-full items-center gap-6">
        <div className="flex items-center gap-1.5">
          <span className="h-1.5 w-1.5 rounded-full bg-accent-400" />
          <span className="text-[13px] font-semibold tracking-wide text-paper">
            UniIntel
          </span>
        </div>
        <nav className="flex h-full items-center gap-4">
          {TABS.map((tab) => (
            <button
              key={tab.id}
              onClick={() => onChange(tab.id)}
              className={`relative h-full text-[13px] font-medium transition-colors ${
                active === tab.id ? "text-paper" : "text-ink-400 hover:text-paper"
              }`}
            >
              {tab.label}
              <span
                className={`absolute inset-x-0 -bottom-px h-0.5 rounded-full transition-colors ${
                  active === tab.id ? "bg-accent-400" : "bg-transparent"
                }`}
              />
            </button>
          ))}
        </nav>
      </div>
      <div className="font-mono text-[11px] text-ink-400">
        {jobId ? (
          <span>
            job <span className="text-accent-400">{jobId.slice(0, 8)}</span>
          </span>
        ) : (
          <span>no job yet</span>
        )}
      </div>
    </header>
  );
}
