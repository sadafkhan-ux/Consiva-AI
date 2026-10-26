import type { EvidenceStatus } from "../hooks/useConsentScan";

/** What a list of evidence says while it is not yet known: still loading, or could not be loaded. Never "none detected", which is only true once the evidence has been read. */
export function EvidenceNotice({ status, what }: { status: Exclude<EvidenceStatus, "loaded">; what: string }) {
  if (status === "loading") {
    return <p className="empty-note" role="status" data-testid="evidence-loading">Loading {what}…</p>;
  }
  return (
    <div className="error-box" role="alert" data-testid="evidence-unavailable">
      <strong>The {what} could not be loaded.</strong> Nothing is shown as "none detected", because the evidence was not read: this is not a clean result. Check the connection and reload the scan.
    </div>
  );
}
