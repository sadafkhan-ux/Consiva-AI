// Shared label/tone mappings. Every key here is a REAL value the backend actually
// returns (see backend/app/scanner/crawler.py's _final_interaction_status and
// backend/app/scanner/consent_signal_detector.py) -- this only maps them to a
// human-readable label + color, it never invents a new state.
const INTERACTION_LABELS: Record<string, [string, string]> = {
  clicked: ["ok", "Success"],
  click_failed: ["bad", "Click Failed"],
  cmp_not_automatable: ["warn", "CMP detected but could not be automated"],
  cmp_not_found: ["neutral", "Not Tested (no banner found)"],
  page_unreachable: ["bad", "Page Unreachable"],
};

export function InteractionPill({ status }: { status?: string | null }) {
  const [tone, label] = status ? INTERACTION_LABELS[status] ?? ["neutral", status] : ["neutral", "Not Tested"];
  return <span className={`badge ${tone}`}>{label}</span>;
}

export function riskTone(level: string): string {
  return level === "high" ? "high" : level === "medium" ? "medium" : "low";
}

export function RiskBadge({ level }: { level: string }) {
  return <span className={`badge ${riskTone(level)}`}>{level}</span>;
}

export function StatusBadge({ status }: { status: string }) {
  const tone = status === "approved" ? "ok" : status === "rejected" ? "bad" : status === "edited" ? "info" : "neutral";
  return <span className={`badge ${tone}`}>{status}</span>;
}

export function formatMs(ms: number | null | undefined): string {
  if (ms == null) return "—";
  return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(2)} s`;
}

export function formatTimestamp(ms: number | null): string {
  if (ms == null) return "—";
  return new Date(ms).toLocaleTimeString();
}
