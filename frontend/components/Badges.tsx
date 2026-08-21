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

const RULE_PRIOR_STYLE = "bg-ink-600/[0.06] text-ink-500 border-ink-500/20";
// Real fetched-page evidence (source_url populated) vs plain LLM inference
// from Part_Desc text with no fetch at all (source_url=None). Both used to
// render as "web evidence" -- confirmed misleading (overstates reliability
// of a bare LLM guess as if it were verified from a live source), since
// origin="llm_extract" covers both paths indiscriminately. See orchestrator.py's
// docstring: source_url is attached ONLY to genuine web_evidence-origin values.
const SOURCED_STYLE = "bg-accent-50 text-accent-700 border-accent-400/40";
const INFERRED_STYLE = "bg-status-neutralBg text-status-neutral border-status-neutral/30";

export function OriginTag({ origin, sourceUrl }: { origin: AttributeOrigin; sourceUrl?: string | null }) {
  if (!origin) return null;
  if (origin === "rule_prior") {
    return (
      <span
        className={`inline-flex items-center rounded border px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide ${RULE_PRIOR_STYLE}`}
      >
        rule prior
      </span>
    );
  }
  // origin === "llm_extract"
  const sourced = Boolean(sourceUrl);
  return (
    <span
      className={`inline-flex items-center rounded border px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide ${
        sourced ? SOURCED_STYLE : INFERRED_STYLE
      }`}
    >
      {sourced ? "web evidence" : "llm inferred"}
    </span>
  );
}
