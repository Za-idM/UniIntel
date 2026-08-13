import type { AccuracyStat } from "@/lib/api";

function pctColor(pct: number): string {
  if (pct >= 90) return "bg-status-exact";
  if (pct >= 50) return "bg-status-close";
  return "bg-status-miss";
}

export function StatCard({ label, stat }: { label: string; stat: AccuracyStat }) {
  return (
    <div className="rounded border border-paper-300 bg-white px-4 py-2.5">
      <div className="font-mono text-[10.5px] font-semibold uppercase tracking-wide text-ink-500">{label}</div>
      <div className="mt-1 flex items-baseline gap-2">
        <span className="text-xl font-semibold text-ink900">{stat.pct.toFixed(1)}%</span>
        <span className="font-mono text-[12px] text-ink-400">
          {stat.correct}/{stat.total}
        </span>
      </div>
      <div className="mt-1.5 h-1.5 w-full overflow-hidden rounded-full bg-paper-200">
        <div
          className={`h-full rounded-full ${pctColor(stat.pct)}`}
          style={{ width: `${Math.min(100, stat.pct)}%` }}
        />
      </div>
    </div>
  );
}

export function MiniBar({ label, stat }: { label: string; stat: AccuracyStat }) {
  return (
    <div>
      <div className="mb-1 flex items-baseline justify-between text-[12px]">
        <span className="font-medium text-ink900">{label}</span>
        <span className="font-mono text-ink-500">
          {stat.pct.toFixed(1)}% <span className="text-ink-400">({stat.correct}/{stat.total})</span>
        </span>
      </div>
      <div className="h-1.5 w-full overflow-hidden rounded-full bg-paper-200">
        <div
          className={`h-full rounded-full ${pctColor(stat.pct)}`}
          style={{ width: `${Math.min(100, stat.pct)}%` }}
        />
      </div>
    </div>
  );
}
