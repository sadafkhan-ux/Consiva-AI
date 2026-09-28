// Agent 6 (Purpose Classifier) API client. Its own file, like dsr.ts, ropa.ts,
// incidents.ts and regwatch.ts, so the other agents' surfaces stay exactly as they
// were; all six share the same bearer token.
//
// WHAT THE TYPES HERE ENFORCE
// ---------------------------
// 1. `undetermined` is a first-class alignment, not a missing value. It is what an
//    honest comparison returns when one side cannot be mapped, and the console has to
//    be able to render it as a real answer rather than as an absence.
//
// 2. `confidence` travels with every comparison, and a mismatch reported at 0.6 is a
//    different claim from one reported at 1.0. A surface that showed the verdict
//    without the confidence would be asserting more than the backend did.
//
// 3. A credential is an INPUT and never an output. No response type here carries a
//    dsn, a spec URL's auth header, or anything derived from them -- the backend does
//    not return them, and the types say so.

import { getToken } from "./auth";
import { handleUnauthorized } from "./client";
import { ApiError } from "./types";

const BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "http://127.0.0.1:8000";

export type Alignment = "aligned" | "mismatch" | "undetermined";
export type RunStatus = "queued" | "running" | "completed" | "failed" | "cancelled";
export type Severity = "high" | "medium" | "low";
export type FindingStatus = "pending" | "approved" | "rejected" | "dismissed";

/** What retention evidence supported, which in Phase 1 is usually "nothing".
 *  `not_evaluated` means the subject carries no lifetime to assess -- it is not a
 *  clean bill of health, and the console must not render it as one. */
export type RetentionStatus =
  | "not_evaluated"
  | "within_expectation"
  | "review_required"
  | "unknown";

/** The three input modes. `rest` reads an OpenAPI SPECIFICATION, never the API's
 *  records -- see backend connectors/rest.py for why that distinction is the whole
 *  design and not a limitation. */
export type Connector = "postgres" | "csv" | "rest";

export interface PurposeRun {
  id: string;
  status: RunStatus;
  scan_id: string | null;
  assessments_count: number;
  findings_count: number;
  /** Set when something LIMITED the run -- most often that no declared purposes were
   *  available, so comparisons came back undetermined for a reason that has nothing to
   *  do with the data. A completed run carrying a note is materially different from a
   *  clean one, so it is surfaced rather than hidden behind the status. */
  note: string | null;
  started_at: string | null;
  completed_at: string | null;
}

export interface PurposeAssessment {
  id: string;
  subject_type: "tracker" | "cookie" | "third_party_service" | "table";
  subject_ref: string;
  subject_label: string | null;
  declared_purpose: string | null;
  declared_source: string | null;
  observed_purpose: string | null;
  observed_source: string | null;
  alignment: Alignment;
  confidence: number;
  retention_status: RetentionStatus;
  retention_note: string | null;
  evidence_refs: string[];
  created_at: string;
}

export interface PurposeFinding {
  id: string;
  assessment_id: string;
  finding_type:
    | "purpose_mismatch"
    | "processing_without_consent"
    | "purpose_undeclared"
    | "retention_review";
  severity: Severity;
  title: string;
  description: string;
  review_required: boolean;
  status: FindingStatus;
  created_at: string;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const token = getToken();
  if (!token) {
    handleUnauthorized();
    throw new ApiError(401, "Not signed in.");
  }

  let res: Response;
  try {
    res = await fetch(`${BASE_URL}${path}`, {
      ...init,
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${token}`,
        ...(init?.headers ?? {}),
      },
    });
  } catch {
    throw new ApiError(0, `Network error -- could not reach the backend at ${BASE_URL}.`);
  }

  if (res.status === 204) return undefined as T;
  const body = await res.json().catch(() => null);

  if (res.status === 401) handleUnauthorized();
  if (!res.ok) {
    const detail =
      body && typeof body === "object" && "detail" in body
        ? typeof body.detail === "string"
          ? body.detail
          : JSON.stringify(body.detail)
        : `Request failed with status ${res.status}`;
    throw new ApiError(res.status, detail);
  }
  return body as T;
}

export const purposeApi = {
  listRuns: (limit = 25) => request<PurposeRun[]>(`/api/v1/purpose/assessments?limit=${limit}`),

  getRun: (id: string) => request<PurposeRun>(`/api/v1/purpose/assessments/${id}`),

  /** Every comparison the run made, including the unremarkable ones. `alignment`
   *  filters; omitting it is the default view on purpose, because a run that could not
   *  compare most of its subjects is a different result from one where everything
   *  agreed, and hiding the undetermined rows would make those look identical. */
  getResults: (id: string, alignment?: Alignment) =>
    request<PurposeAssessment[]>(
      `/api/v1/purpose/assessments/${id}/results` +
        (alignment ? `?alignment=${alignment}` : ""),
    ),

  listFindings: (params: { status?: FindingStatus; severity?: Severity; limit?: number } = {}) => {
    const q = new URLSearchParams();
    if (params.status) q.set("status", params.status);
    if (params.severity) q.set("severity", params.severity);
    q.set("limit", String(params.limit ?? 50));
    return request<PurposeFinding[]>(`/api/v1/purpose/findings?${q}`);
  },

  /** Assess a completed consent scan. */
  assessScan: (scan_id: string) =>
    request<PurposeRun>("/api/v1/purpose/assessments", {
      method: "POST",
      body: JSON.stringify({ scan_id }),
    }),

  /**
   * Assess a structured source.
   *
   * `dsn`, `csv_content` and `auth_header` are sent once and are never stored on the
   * run record -- a connection string or bearer token persisted on a row is a
   * credential in a table many people can read. Nothing returned by this call echoes
   * them back, which is why no response type above has a field for one.
   */
  assessSource: (payload: {
    source_name: string;
    connector: Connector;
    dsn?: string;
    csv_content?: string;
    table_name?: string;
    spec_url?: string;
    auth_header?: string;
  }) =>
    request<PurposeRun>("/api/v1/purpose/sources/assess", {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  /** A reason is REQUIRED to reject or dismiss. Setting a compliance finding aside is
   *  itself a compliance decision, and one with no recorded reasoning cannot be told
   *  apart later from one nobody looked at. The backend enforces this; the console
   *  should not let it get that far. */
  decide: (findingId: string, decision: "approved" | "rejected" | "dismissed", reason?: string) =>
    request<PurposeFinding>(`/api/v1/purpose/findings/${findingId}/decision`, {
      method: "POST",
      body: JSON.stringify({ decision, reason: reason ?? null }),
    }),
};
