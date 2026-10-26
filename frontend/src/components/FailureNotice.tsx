import type { ScanFailure } from "../api/types";

const WHO: Record<string, string> = {
  website_owner: "The website's owner has to act",
  scanner_operator: "The scanner's operator has to act",
  none: "Nobody has to change anything",
};

export const OUTCOME_LABELS: Record<string, string> = {
  QUEUED: "Queued",
  RUNNING: "Running",
  COMPLETED: "Completed",
  COMPLETED_WITH_WARNINGS: "Completed with warnings",
  PARTIALLY_COMPLETED: "Partially completed",
  BLOCKED: "Blocked",
  FAILED: "Failed",
  CANCELLED: "Cancelled",
};

/** Why a scan could not be completed, what that does and does not mean, and the next step. A technical failure is never shown as a compliance result. */
export function FailureNotice({ failure, outcome }: { failure: ScanFailure | null | undefined; outcome?: string | null }) {
  if (!failure) return null;
  const partial = outcome === "PARTIALLY_COMPLETED" || failure.category === "PARTIAL_SCAN";
  return (
    <div className={partial ? "status-banner warn" : "error-box"} role={partial ? "status" : "alert"} data-testid="failure-notice">
      <strong>{outcome && OUTCOME_LABELS[outcome] ? `${OUTCOME_LABELS[outcome]}: ` : ""}{failure.title}.</strong> {failure.explanation}
      <div style={{ marginTop: 6 }}><b>Next step:</b> {failure.next_action}</div>
      <div className="muted small" style={{ marginTop: 6 }}>
        {WHO[failure.owner] ?? failure.owner} · {failure.retryable ? "Scanning again can help" : "Scanning again without a change will not help"}
        {failure.detail ? <> · Error: <code>{failure.detail}</code></> : null}
      </div>
      <div className="muted small" style={{ marginTop: 4 }}>This is a technical result. It says nothing about the site's consent practices, and no compliance conclusion is drawn from it.</div>
    </div>
  );
}
