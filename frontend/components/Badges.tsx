import type { AttributeOrigin, ConfidenceBand, MatchStatus } from "@/lib/api";

const MATCH_STYLES: Record<MatchStatus, string> = {
  EXACT: "bg-status-exactBg text-status-exact",
  CLOSE: "bg-status-closeBg text-status-close",
  MISS: "bg-status-missBg text-status-miss",
  NO_GT: "bg-status-neutralBg text-status-neutral",
};

export function MatchBadge({ status }: { status: MatchStatus }) {
  return (
    <span
      className={`inline-flex items-center rounded px-1.5 py-0.5 text-[11px] font-semibold tracking-wide ${MATCH_STYLES[status]}`}
    >
      {status}
    </span>
  );
}

const BAND_STYLES: Record<ConfidenceBand, string> = {
  VERIFIED: "bg-status-exactBg text-status-exact",
  REVIEW: "bg-status-closeBg text-status-close",
  LOW: "bg-status-missBg text-status-miss",
};

export function ConfidenceBadge({ band, score }: { band: ConfidenceBand; score?: number }) {
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded px-2 py-0.5 text-xs font-semibold ${BAND_STYLES[band]}`}
    >
      {band}
      {score !== undefined && <span className="font-mono font-normal opacity-70">{score.toFixed(0)}</span>}
    </span>
  );
}

const ORIGIN_LABEL: Record<string, string> = {
  rule_prior: "rule prior",
  llm_extract: "web evidence",
};

const ORIGIN_STYLE: Record<string, string> = {
  rule_prior: "bg-ink-600/[0.06] text-ink-500 border-ink-500/20",
  llm_extract: "bg-accent-50 text-accent-700 border-accent-400/40",
};

export function OriginTag({ origin }: { origin: AttributeOrigin }) {
  if (!origin) return null;
  return (
    <span
      className={`inline-flex items-center rounded border px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide ${ORIGIN_STYLE[origin]}`}
    >
      {ORIGIN_LABEL[origin]}
    </span>
  );
}
