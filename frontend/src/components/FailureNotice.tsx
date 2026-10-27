import type { ScanFailure } from "../api/types";

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

/** Why a scan couldn't be completed, in one plain sentence, and what the person can do next. The technical error is not shown here: it is in the server's logs. */
export function FailureNotice({ failure, outcome }: { failure: ScanFailure | null | undefined; outcome?: string | null }) {
  if (!failure) return null;
  const partial = outcome === "PARTIALLY_COMPLETED" || failure.category === "PARTIAL_SCAN";
  const message = failure.user_message || "Something went wrong while scanning.";
  const next = failure.what_you_can_do || "Please try again. If it keeps happening, contact support.";
  return (
    <div className={partial ? "status-banner warn" : "error-box"} role={partial ? "status" : "alert"} data-testid="failure-notice">
      <strong>{message}</strong>
      <div style={{ marginTop: 6 }}><b>What you can do:</b> {next}</div>
      <div className="muted small" style={{ marginTop: 6 }}>This is about reaching the website, not about its privacy practices.</div>
    </div>
  );
}

/** The same box for an error that arrived as plain text. */
export function SimpleErrorNotice({ message, next }: { message: string; next: string }) {
  return (
    <div className="error-box" role="alert" data-testid="friendly-error">
      <strong>{message}</strong>
      <div style={{ marginTop: 6 }}><b>What you can do:</b> {next}</div>
    </div>
  );
}
