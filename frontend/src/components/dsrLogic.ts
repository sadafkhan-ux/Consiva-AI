import { useEffect, useState } from "react";
import { dsrApi, type DsrCase, type DsrEvidence, type DsrSearchRun } from "../api/dsr";

/**
 * What the New request flow and the All cases view have in common, so the two entry points
 * say the same thing about the same case: what a search outcome is, what a status is called,
 * which request types exist, and the small panels (code, manual verification, request type,
 * duplicate warning) both screens use.
 *
 * Nothing here decides anything the backend decides. The outcome of a search is read from the
 * search runs the backend recorded; an empty evidence list alone is never taken as "searched".
 */

// ── Search outcome ───────────────────────────────────────────────────────────────

/** A run in one of these states finished and says something true about its source. */
export const SEARCH_FINISHED = new Set(["completed", "no_match", "multiple_matches"]);

export type SearchState =
  | "not_run" // no search has run for this case
  | "running" // a search is in progress
  | "failed" // no source was searched successfully
  | "partial" // some sources were searched, others failed -- the result is incomplete
  | "no_match" // every search finished and found nothing
  | "found"; // every search finished and found at least one record

/** Evidence shown only under a search that ran: with no runs there is nothing to attribute it to,
 *  and a record that names a different run than any this case has is not this case's result. */
export function visibleEvidence(runs: DsrSearchRun[], evidence: DsrEvidence[]): DsrEvidence[] {
  if (runs.length === 0) return [];
  const ids = new Set(runs.map((r) => r.id));
  return evidence.filter((e) => !e.search_run_id || ids.has(e.search_run_id));
}

/** The one place "did a search happen, and what did it find" is decided. An empty evidence list
 *  is "no match" ONLY when at least one search run actually finished and none failed. */
export function searchState(
  runs: DsrSearchRun[],
  evidence: DsrEvidence[],
  caseStatus: string,
): SearchState {
  if (caseStatus === "searching") return "running";
  if (runs.length === 0) return caseStatus === "failed" ? "failed" : "not_run";
  if (runs.some((r) => r.status === "pending" || r.status === "running")) return "running";
  const finished = runs.filter((r) => SEARCH_FINISHED.has(r.status));
  const failed = runs.filter((r) => r.status === "failed");
  if (finished.length === 0) return "failed";
  if (failed.length > 0) return "partial";
  return visibleEvidence(runs, evidence).length > 0 ? "found" : "no_match";
}

/** Mirrors the backend rule (response_service.assert_ready_to_draft) so the button says why it
 *  is off rather than being pressed and refused. The backend still enforces it. */
export function canDraftResponse(kase: DsrCase, runs: DsrSearchRun[]): boolean {
  if (kase.request_type === "grievance") return true;
  return identityVerified(kase) && runs.some((r) => SEARCH_FINISHED.has(r.status));
}

export function identityVerified(kase: Pick<DsrCase, "identity_status"> | null | undefined): boolean {
  return kase?.identity_status === "verified" || kase?.identity_status === "manually_verified";
}

// ── Plain labels ─────────────────────────────────────────────────────────────────

/** The types the backend implements end to end (classification, planning, response). */
export const REQUEST_TYPE_OPTIONS: { value: string; label: string }[] = [
  { value: "access", label: "Access — see what data is held" },
  { value: "export", label: "Export — a copy of their data" },
  { value: "information", label: "Information — how their data is used" },
  { value: "correction", label: "Correction — fix their data" },
  { value: "deletion", label: "Deletion — erase their data" },
  { value: "withdraw_consent", label: "Withdraw consent" },
  { value: "grievance", label: "Grievance — a complaint" },
];

export function requestTypeLabel(type: string): string {
  const found = REQUEST_TYPE_OPTIONS.find((o) => o.value === type);
  if (found) return found.label.split(" — ")[0];
  if (type === "other" || type === "unclassified") return "Type not set";
  return type;
}

/** True when a person still has to say what this request is. A type a person chose is final. */
export function needsTypeSelection(
  kase: Pick<DsrCase, "request_type" | "classification_method" | "classification_confidence" | "is_terminal">,
): boolean {
  if (kase.is_terminal) return false;
  if (kase.classification_method === "manual") return false;
  return (
    kase.request_type === "other" ||
    kase.request_type === "unclassified" ||
    (kase.classification_confidence ?? 0) < 0.5
  );
}

const STATUS_LABELS: Record<string, string> = {
  received: "Received",
  classified: "Received — identity not verified",
  identity_pending: "Verifying identity",
  identity_verified: "Identity verified — ready to search",
  searching: "Searching",
  search_completed: "Search finished — review the results",
  review_required: "Needs review",
  approval_required: "Waiting for approval",
  approved: "Approved — ready to run",
  executing: "Running approved actions",
  execution_verified: "Actions verified — response next",
  response_pending: "Response ready to prepare",
  completed: "Completed",
  partially_completed: "Closed — partly completed",
  rejected: "Rejected",
  cancelled: "Cancelled",
  expired: "Deadline passed",
  escalated: "Escalated to a senior reviewer",
  failed: "Failed",
};

/** What a person should read instead of `review_required` or `failed`. The stored status is still
 *  what the backend acts on; this only names it. */
export function caseStatusLabel(
  kase: Pick<DsrCase, "status" | "request_type" | "error_code" | "classification_method" | "classification_confidence" | "is_terminal">,
): string {
  if (kase.status === "review_required") {
    if (kase.request_type === "grievance") return "Complaint — a person needs to respond";
    if (needsTypeSelection(kase)) return "Needs review — request type unclear";
    return "Needs review";
  }
  if (kase.status === "failed") {
    return kase.error_code === "SOURCE_NOT_AUTHORIZED"
      ? "Waiting for a data source to be connected"
      : "Failed — see the message below";
  }
  return STATUS_LABELS[kase.status] ?? kase.status.replace(/_/g, " ");
}

/** A stored status named in words, for filters and lists that have no whole case to hand. */
export function plainStatus(status: string): string {
  return STATUS_LABELS[status] ?? status.replace(/_/g, " ");
}

/** What to tell a person after a code was refused, with the attempts they have left. */
export function wrongCodeMessage(out: { status: string; attempts_remaining?: number | null }): string {
  if (out.status === "failed" || out.attempts_remaining === 0) {
    return "Too many wrong codes. This code is locked. Wait a few minutes before generating another, or verify the requester manually.";
  }
  if (out.status === "expired") return "That code has expired. Generate a new one.";
  const left = out.attempts_remaining;
  return `That code didn't match.${left != null ? ` ${left} attempt${left === 1 ? "" : "s"} left.` : ""}`;
}

export function statusTone(status: string, errorCode?: string | null): string {
  if (status === "failed") return errorCode === "SOURCE_NOT_AUTHORIZED" ? "warn" : "bad";
  if (["completed", "execution_verified", "identity_verified"].includes(status)) return "ok";
  if (["rejected", "expired"].includes(status)) return "bad";
  if (["partially_completed", "escalated", "review_required", "approval_required"].includes(status)) return "warn";
  return "muted";
}

export function identityLabel(status: string | null | undefined): string {
  switch (status) {
    case "verified": return "Verified";
    case "manually_verified": return "Verified by a person";
    case "in_progress": return "Code generated — not confirmed yet";
    case "failed": return "Locked — too many wrong codes";
    case "expired": return "Code expired";
    default: return "Not verified";
  }
}

export function maskEmail(email: string | null | undefined): string {
  if (!email) return "—";
  const [local, domain] = email.split("@");
  if (!domain) return "••••";
  return `${local.slice(0, 2)}${"•".repeat(Math.max(local.length - 2, 3))}@${domain}`;
}

export function formatDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
}

// ── Wizard position, recovered from the case ─────────────────────────────────────

export type WizardStep = "email" | "verify" | "searching" | "data" | "review" | "approval" | "result";

/** Where the New request flow should be for a case that already exists -- used after a reload, so
 *  the position comes from what the backend says about the case, never from remembered UI state. */
export function stepForCase(kase: DsrCase): WizardStep {
  if (!identityVerified(kase) && !kase.is_terminal) return "verify";
  if (kase.is_terminal) return "result";
  switch (kase.status) {
    case "searching":
      return "searching";
    case "approval_required":
    case "approved":
    case "executing":
      return "approval";
    case "execution_verified":
    case "response_pending":
      return "result";
    default:
      return "data";
  }
}

export type SourceStatus = "unknown" | "connected" | "none";

/** Whether any data source is authorised for DSR, read from the same configuration the
 *  Configuration tab shows. "unknown" (a failed read) shows nothing rather than a false alarm. */
export function useSourceStatus(): SourceStatus {
  const [status, setStatus] = useState<SourceStatus>("unknown");
  useEffect(() => {
    let alive = true;
    dsrApi
      .listSourceAuthorizations()
      .then((rows) => {
        if (alive) setStatus(rows.some((r) => r.enabled) ? "connected" : "none");
      })
      .catch(() => {
        if (alive) setStatus("unknown");
      });
    return () => {
      alive = false;
    };
  }, []);
  return status;
}

export const MIN_MANUAL_NOTE = 10;
