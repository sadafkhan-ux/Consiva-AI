// Agent 3 (DSR Fulfillment) API client. Separate file from client.ts and ropa.ts so
// Agent 1's and Agent 2's surfaces stay exactly as they were; all three share the
// same bearer token.

import { getToken, logout } from "./auth";
import { ApiError } from "./types";

const BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "http://127.0.0.1:8000";

/** SLA figures come from the backend. The UI never computes its own deadline --
 *  two clocks disagreeing about whether a case is overdue is worse than one. */
export interface DsrSla {
  received_at: string | null;
  due_at: string;
  remaining_seconds: number;
  overdue: boolean;
  breached: boolean;
  escalated: boolean;
  closed: boolean;
}

export interface DsrCase {
  id: string;
  reference: string;
  status: string;
  request_type: string;
  classification_method: string | null;
  classification_confidence: number | null;
  raw_request: string;
  requester_email: string | null;
  requester_phone: string | null;
  requester_reference: string | null;
  error_code: string | null;
  error_detail: string | null;
  identity_status: string | null;
  /** Straight from the backend's state machine, so the UI greys out what is
   *  genuinely impossible rather than keeping its own copy of the rules. */
  allowed_transitions: string[];
  is_terminal: boolean;
  sla: DsrSla;
  created_at: string | null;
  closed_at: string | null;
}

export interface DsrSearchRun {
  id: string;
  source: string;
  status: string;
  matches: number;
  distinct_subjects: number;
  tables_searched: string[];
  error_code: string | null;
  error_detail: string | null;
  completed_at: string | null;
}

export interface DsrEvidence {
  id: string;
  source: string;
  table: string;
  matched_column: string;
  identifier_kind: string;
  match_type: string;
  confidence: number;
  record_reference: Record<string, unknown>;
  record_snapshot: Record<string, unknown> | null;
  observed_at: string | null;
}

export interface DsrAction {
  id: string;
  source: string;
  table: string;
  operation: string;
  payload: Record<string, unknown>;
  reason: string;
  expected_result: string;
  risk: string;
  requires_approval: boolean;
  status: string;
  /** Operator-facing: names tables, columns and configuration. Reviewer only. */
  blocked_reason: string | null;
  /** What the data subject is actually told. Never shows our internal state. */
  requester_explanation: string | null;
  record_reference: Record<string, unknown>;
}

/** What the requester chose for one discovered record. Mirrors the backend's
 *  closed vocabulary; the UI never invents a verb the engine cannot execute. */
export type DsrSelection = "delete" | "keep" | "correct" | "export" | "review";

export interface DsrPlan {
  plan: {
    id: string;
    version: number;
    status: string;
    summary: string;
    requires_approval: boolean;
    constraints_evaluated: Array<Record<string, unknown>>;
  } | null;
  actions: DsrAction[];
  decisions: {
    total: number;
    approved: number;
    rejected: number;
    blocked: number;
    awaiting_decision: number;
    no_approval_needed: number;
    ready_to_execute: boolean;
    nothing_to_execute: boolean;
  } | null;
}

export interface DsrExecution {
  id: string;
  action_id: string;
  status: string;
  rows_affected: number | null;
  /** Two separate fields on purpose: "the write ran" and "a read-back confirmed
   *  it" are different claims, and the UI shows both. */
  verification_status: string | null;
  verification_detail: Record<string, unknown> | null;
  verified_at: string | null;
  error_code: string | null;
  error_detail: string | null;
  attempts: number;
}

export interface DsrResponseDoc {
  id: string;
  version: number;
  status: string;
  body_text: string;
  grounded_facts: Array<Record<string, unknown>>;
  drafted_by_model: string | null;
  sent_at?: string | null;
}

export interface DsrAuditEntry {
  id: string;
  action: string;
  actor_user_id: string | null;
  before: Record<string, unknown> | null;
  after: Record<string, unknown> | null;
  created_at: string | null;
}

export interface IdentityChallenge {
  verification_id: string;
  status: string;
  expires_at: string;
  /** Returned exactly once. Stored only as a hash; never retrievable again. */
  challenge: string;
  delivery_note: string;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const token = getToken();
  if (!token) throw new ApiError(401, "Not signed in.");

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

  if (res.status === 401) logout();
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

export const dsrApi = {
  listCases(): Promise<DsrCase[]> {
    return request("/api/v1/dsr/requests");
  },
  getCase(id: string): Promise<DsrCase> {
    return request(`/api/v1/dsr/requests/${id}`);
  },
  createCase(payload: {
    raw_request: string;
    requester_email?: string | null;
    requester_phone?: string | null;
    requester_reference?: string | null;
  }): Promise<DsrCase> {
    return request("/api/v1/dsr/requests", { method: "POST", body: JSON.stringify(payload) });
  },
  classify(id: string, overrideType?: string): Promise<DsrCase> {
    return request(`/api/v1/dsr/requests/${id}/classify`, {
      method: "POST",
      body: JSON.stringify({ override_type: overrideType ?? null }),
    });
  },
  startChallenge(id: string): Promise<IdentityChallenge> {
    return request(`/api/v1/dsr/requests/${id}/identity/challenge`, { method: "POST" });
  },
  verifyIdentity(
    id: string,
    payload: { challenge?: string; manual?: boolean; evidence_note?: string },
  ): Promise<{ status: string; satisfied: boolean; attempts: number; case: DsrCase }> {
    return request(`/api/v1/dsr/requests/${id}/identity/verify`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },
  startSearch(id: string): Promise<{ job_id: string; case: DsrCase }> {
    return request(`/api/v1/dsr/requests/${id}/search`, { method: "POST" });
  },
  getResults(id: string): Promise<{ search_runs: DsrSearchRun[]; evidence: DsrEvidence[] }> {
    return request(`/api/v1/dsr/requests/${id}/results`);
  },
  getPlan(id: string): Promise<DsrPlan> {
    return request(`/api/v1/dsr/requests/${id}/plan`);
  },
  decideAction(
    caseId: string,
    actionId: string,
    decision: string,
    reason: string | null,
  ): Promise<{ decisions: DsrPlan["decisions"]; case: DsrCase }> {
    return request(`/api/v1/dsr/requests/${caseId}/actions/${actionId}/decision`, {
      method: "POST",
      body: JSON.stringify({ decision, reason }),
    });
  },
  /** Build the plan from per-record choices. The counts in the response come
   *  from the backend, so the review screen never tallies its own numbers. */
  buildPlan(
    id: string,
    selections: Array<{ evidence_id: string; action: DsrSelection; corrections?: Record<string, unknown> }>,
  ): Promise<DsrPlan> {
    return request(`/api/v1/dsr/requests/${id}/plan`, {
      method: "POST",
      body: JSON.stringify({ selections }),
    });
  },
  execute(id: string): Promise<{ job_id: string; case: DsrCase }> {
    return request(`/api/v1/dsr/requests/${id}/execute`, { method: "POST" });
  },
  getExecutions(id: string): Promise<DsrExecution[]> {
    return request(`/api/v1/dsr/requests/${id}/executions`);
  },
  generateResponse(id: string): Promise<DsrResponseDoc> {
    return request(`/api/v1/dsr/requests/${id}/response`, { method: "POST" });
  },
  getResponse(id: string): Promise<DsrResponseDoc | null> {
    return request(`/api/v1/dsr/requests/${id}/response`);
  },
  complete(id: string): Promise<DsrCase> {
    return request(`/api/v1/dsr/requests/${id}/complete`, { method: "POST" });
  },
  getAudit(id: string): Promise<DsrAuditEntry[]> {
    return request(`/api/v1/dsr/requests/${id}/audit`);
  },
};
