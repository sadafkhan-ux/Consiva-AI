// Agent 5 (Regulatory Watch) API client. Its own file, like dsr.ts, ropa.ts and
// incidents.ts, so the other four agents' surfaces stay exactly as they were; all
// five share the same bearer token.
//
// TWO THINGS THE TYPES HERE ENFORCE
// ---------------------------------
// 1. A source is never `SourceHealth`-less. The health block is required on the
//    type, not optional, so a component physically cannot render a source without
//    also having the information that it is NOT being watched.
//
// 2. Every substantive claim carries a confidence, and `confirmed` is not something
//    a rule or model produces -- only a person, by editing a finding. A console that
//    renders "this applies to you" when the backend said "possible" is the exact
//    failure this agent exists to avoid.

import { getToken } from "./auth";
import { handleUnauthorized } from "./client";
import { ApiError } from "./types";

const BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "http://127.0.0.1:8000";

export type Confidence = "confirmed" | "probable" | "possible" | "unknown";
export type Relevance = "relevant" | "not_relevant" | "undetermined";
export type Priority = "low" | "medium" | "high" | "critical";

/** UPPERCASE, matching the backend. A lowercase code was a real bug in Agent 4, so
 *  the spelling lives in one place here. */
export const REGWATCH_ERROR_CODES = {
  SOURCE_NOT_AUTHORIZED: "SOURCE_NOT_AUTHORIZED",
  SOURCE_UNREACHABLE: "SOURCE_UNREACHABLE",
  COLLECTION_FAILED: "COLLECTION_FAILED",
  CONTENT_UNUSABLE: "CONTENT_UNUSABLE",
  NO_BASELINE: "NO_BASELINE",
  ASSESSMENT_FAILED: "ASSESSMENT_FAILED",
  RELEVANCE_UNDETERMINED: "RELEVANCE_UNDETERMINED",
  APPROVAL_REQUIRED: "APPROVAL_REQUIRED",
  INVALID_SOURCE: "INVALID_SOURCE",
} as const;

/** Computed in ONE place on the backend so no surface can disagree about whether a
 *  source is being watched. `is_current` is deliberately narrow: true only when the
 *  source is ENABLED and its last attempt succeeded recently. A disabled source is
 *  never current, however fresh its last collection. */
export interface SourceHealth {
  state: "not_monitored" | "never_collected" | "failing" | "stale" | "current";
  is_current: boolean;
  note: string;
  enabled: boolean;
  last_checked_at: string | null;
  last_success_at: string | null;
  consecutive_failures: number;
  alarming: boolean;
}

export interface WatchSource {
  id: string;
  name: string;
  url: string;
  connector: string;
  jurisdiction: string;
  topic: string | null;
  authority: string | null;
  check_interval_minutes: number;
  /** The NAME of an environment variable. There is no endpoint anywhere that
   *  returns the value, and nothing in this file ever asks for one. */
  credential_ref: string | null;
  requires_credential: boolean;
  created_at: string | null;
  /** Required, not optional. See the header. */
  health: SourceHealth;
}

export interface WatchCollection {
  id: string;
  status: string;
  http_status: number | null;
  content_hash: string | null;
  content_bytes: number | null;
  retrieved_at: string | null;
  error_code: string | null;
  error_detail: string | null;
  is_current: boolean;
  created_at: string | null;
}

export interface WatchImpact {
  id: string;
  target_kind: string;
  target_id: string | null;
  target_label: string;
  confidence: Confidence;
  derived_from: string;
  rationale: string | null;
}

export interface WatchAction {
  id: string;
  title: string;
  rationale: string;
  expected_result: string;
  owner_label: string | null;
  status: string;
  due_at: string | null;
  completed_by: string | null;
  completion_note: string | null;
  completed_at: string | null;
  /** The platform did not do this work and does not claim to have verified it. */
  attested_not_verified: boolean;
  /** Four-valued, not a boolean. "No date was set" and "on track" are different
   *  facts, and `overdue: false` would collapse them into one. */
  due: ActionDue;
}

export interface ActionDue {
  state: "no_date" | "settled" | "overdue" | "due_soon" | "on_track";
  overdue: boolean;
  due_at: string | null;
  remaining_seconds: number | null;
  note: string;
  /** Always false. This is the organisation's own target, never a legal deadline,
   *  and the flag is on the payload so no screen has to remember the caveat. */
  is_statutory_deadline: boolean;
}

export interface WatchCitation {
  chunk_id: string;
  document_id: string;
  document_title: string;
  document_version: string | null;
  section: string | null;
}

export interface WatchFinding {
  id: string;
  reference: string;
  status: string;
  summary: string | null;
  jurisdiction: string | null;
  relevance: Relevance;
  relevance_confidence: Confidence;
  relevance_reason: string | null;
  impact_summary: string | null;
  priority: Priority | null;
  priority_confidence: Confidence;
  citations: WatchCitation[];
  open_questions: string[];
  requires_human_review: boolean;
  drafted_by_model: string | null;
  error_code: string | null;
  error_detail: string | null;
  source_id: string;
  change_id: string;
  reviewed_at: string | null;
  closed_at: string | null;
  created_at: string | null;
  impacts?: WatchImpact[];
  actions?: WatchAction[];
  approvals?: { id: string; decision: string; subject: string; reason: string | null; created_at: string | null }[];
}

export interface WatchFindingDetail extends WatchFinding {
  change: {
    id: string;
    change_kind: string;
    added_lines: number;
    removed_lines: number;
    diff_excerpt: string | null;
    detected_at: string | null;
  } | null;
  grounded_facts: { chunk_id: string; excerpt: string }[];
}

export interface WatchSummary {
  sources: number;
  sources_enabled: number;
  sources_not_currently_watched: number;
  unwatched: (SourceHealth & { id: string; name: string; url: string })[];
  findings_by_status: Record<string, number>;
  awaiting_review: number;
  open_actions: number;
  overdue_actions: number;
  overdue_note: string;
  coverage_note: string;
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

export const regwatchApi = {
  summary: () => request<WatchSummary>("/api/v1/regwatch/summary"),

  listSources: () =>
    request<{ sources: WatchSource[]; not_currently_watched: number; unwatched: unknown[] }>(
      "/api/v1/regwatch/sources",
    ),

  getSource: (id: string) =>
    request<WatchSource & { collections: WatchCollection[]; baseline: { id: string; version: number; content_hash: string; approved_at: string | null; note: string | null } | null }>(
      `/api/v1/regwatch/sources/${id}`,
    ),

  registerSource: (payload: {
    name: string;
    url: string;
    jurisdiction: string;
    /** http, rss, or manual_upload. A manual source is never fetched; a feed is
     *  parsed as items so a new entry is a one-line diff. */
    connector?: string;
    topic?: string | null;
    authority?: string | null;
    check_interval_minutes?: number;
    credential_ref?: string | null;
  }) =>
    request<WatchSource>("/api/v1/regwatch/sources", {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  setSourceEnabled: (id: string, enabled: boolean, reason?: string) =>
    request<WatchSource>(`/api/v1/regwatch/sources/${id}`, {
      method: "PATCH",
      body: JSON.stringify({ enabled, reason }),
    }),

  /** Queued, not performed inline -- the fetch goes out to a third-party site. */
  collectNow: (id: string) =>
    request<{ job_id: string; status: string; source_id: string }>(
      `/api/v1/regwatch/sources/${id}/collect`,
      { method: "POST" },
    ),

  acceptBaseline: (id: string, collectionId: string, note?: string) =>
    request<{ id: string; version: number; content_hash: string }>(
      `/api/v1/regwatch/sources/${id}/baseline`,
      { method: "POST", body: JSON.stringify({ collection_id: collectionId, note }) },
    ),

  getJurisdictions: () =>
    request<{ jurisdictions: string[]; note: string }>("/api/v1/regwatch/jurisdictions"),

  setJurisdictions: (jurisdictions: string[]) =>
    request<{ jurisdictions: string[] }>("/api/v1/regwatch/jurisdictions", {
      method: "PUT",
      body: JSON.stringify({ jurisdictions }),
    }),

  listFindings: (status?: string) =>
    request<{ findings: WatchFinding[]; awaiting_review: number }>(
      `/api/v1/regwatch/findings${status ? `?finding_status=${encodeURIComponent(status)}` : ""}`,
    ),

  getFinding: (id: string) =>
    request<WatchFindingDetail>(`/api/v1/regwatch/findings/${id}`),

  decide: (id: string, decision: string, reason?: string, editedPayload?: Record<string, unknown>) =>
    request<WatchFinding>(`/api/v1/regwatch/findings/${id}/decision`, {
      method: "POST",
      body: JSON.stringify({ decision, reason, edited_payload: editedPayload }),
    }),

  openActions: (
    id: string,
    actions: { title: string; rationale: string; expected_result: string; owner_label?: string }[],
  ) =>
    request<WatchFinding>(`/api/v1/regwatch/findings/${id}/actions`, {
      method: "POST",
      body: JSON.stringify({ actions }),
    }),

  completeAction: (actionId: string, completedBy: string, note: string) =>
    request<WatchAction>(`/api/v1/regwatch/actions/${actionId}/complete`, {
      method: "POST",
      body: JSON.stringify({ completed_by: completedBy, note }),
    }),

  /** Open / in progress / blocked / cancelled. Completion is deliberately NOT here:
   *  it goes through completeAction, which requires a name and a description of the
   *  work, because the platform did not do it and cannot verify it. */
  setActionStatus: (actionId: string, status: string, reason?: string) =>
    request<WatchAction>(`/api/v1/regwatch/actions/${actionId}`, {
      method: "PATCH",
      body: JSON.stringify({ status, reason }),
    }),

  overdueActions: () =>
    request<{ count: number; actions: (ActionDue & { id: string; finding_id: string; finding_reference: string; title: string; owner_label: string | null; status: string })[]; note: string }>(
      "/api/v1/regwatch/actions/overdue",
    ),

  /** Adopt the snapshot a finding was raised from as the source's baseline. One act
   *  with two consequences -- the baseline moves and the finding closes -- so it is
   *  one call, not a button for each. */
  acceptBaselineFromFinding: (findingId: string, note?: string) =>
    request<{ baseline: { id: string; version: number; content_hash: string }; finding: WatchFinding }>(
      `/api/v1/regwatch/findings/${findingId}/accept-baseline`,
      { method: "POST", body: JSON.stringify({ note }) },
    ),

  /** Content for a manual-upload source, which is never fetched. */
  uploadManualContent: (sourceId: string, content: string, note?: string) =>
    request<{ collection: WatchCollection; change_kind: string; finding: WatchFinding | null }>(
      `/api/v1/regwatch/sources/${sourceId}/upload`,
      { method: "POST", body: JSON.stringify({ content, note }) },
    ),

  closeFinding: (id: string, reason?: string) =>
    request<WatchFinding>(`/api/v1/regwatch/findings/${id}/close`, {
      method: "POST",
      body: JSON.stringify({ enabled: false, reason }),
    }),

  audit: (id: string) =>
    request<{ entries: { id: string; action: string; before: unknown; after: unknown; created_at: string | null }[] }>(
      `/api/v1/regwatch/findings/${id}/audit`,
    ),
};
