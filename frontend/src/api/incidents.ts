// Agent 4 (Breach Response) API client. Its own file, like dsr.ts and ropa.ts, so
// the other three agents' surfaces stay exactly as they were; all four share the
// same bearer token.
//
// The types here mirror the backend's vocabulary rather than simplifying it. In
// particular every substantive claim carries a CONFIDENCE alongside it, and the UI
// is expected to show that confidence -- an incident console that renders
// "personal data involved: yes" when the backend said "probable" is the exact
// failure this agent is built to avoid.

import { getToken } from "./auth";
import { handleUnauthorized } from "./client";
import { ApiError } from "./types";

const BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "http://127.0.0.1:8000";

/** The four-level vocabulary. `confirmed` is never produced by a rule or a model --
 *  only by a person, through the finding endpoint, with a reason. */
export type Confidence = "confirmed" | "probable" | "possible" | "unknown";

/** Internal response target, not a statutory deadline. The note travels with it so
 *  the UI never has to invent the caveat itself. */
export interface IncidentSla {
  detected_at: string | null;
  reported_at: string | null;
  due_at: string | null;
  remaining_seconds: number | null;
  overdue: boolean;
  breached: boolean;
  escalated: boolean;
  closed: boolean;
  note: string;
}

export interface Incident {
  id: string;
  reference: string;
  title: string;
  description: string;
  source: string;
  reported_by: string | null;
  incident_type: string | null;
  classification_method: string | null;
  classification_confidence: number | null;
  initial_severity: string | null;
  severity: string | null;
  severity_score: number | null;
  severity_confidence: Confidence | null;
  /** The two questions the agent exists to answer carefully. */
  personal_data_involved: Confidence;
  breach_confirmed: Confidence;
  status: string;
  error_code: string | null;
  error_detail: string | null;
  occurred_at: string | null;
  detected_at: string | null;
  closure_summary: string | null;
  /** Straight from the backend's state machine, so the UI greys out what is
   *  genuinely impossible rather than keeping its own copy of the rules. */
  allowed_transitions: string[];
  is_terminal: boolean;
  sla: IncidentSla;
  created_at: string | null;
  closed_at: string | null;
}

export interface IncidentEvidence {
  id: string;
  kind: string;
  source_system: string;
  summary: string;
  observed_at: string | null;
  is_derived: boolean;
  /** True where secret-shaped fields were stripped before storage. Surfaced so a
   *  reviewer knows something was removed rather than assuming the row was empty. */
  contains_secrets: boolean;
  /** The list route deliberately does not return detail -- only whether there is
   *  any. Reading it is a separate, audited request. */
  has_detail: boolean;
  supersedes_id: string | null;
  detail?: Record<string, unknown> | null;
}

export interface TimelineEntry {
  id: string;
  occurred_at: string;
  event: string;
  actor: string | null;
  source_system: string | null;
  confidence: Confidence;
  evidence_id: string | null;
}

export interface AffectedSystem {
  id: string;
  system_name: string;
  system_kind: string;
  component: string | null;
  confidence: Confidence;
  /** False for a system Consiva has never connected to. Its contents are then
   *  unknown, which is a gap rather than an absence of personal data. */
  known_to_consiva: boolean;
  notes: string | null;
}

export interface AffectedCategory {
  category: string;
  confidence: Confidence;
  derived_from: string[];
  columns: string[];
}

export interface AffectedSubjects {
  group: string;
  record_count: number | null;
  count_basis: "counted" | "estimated" | "unknown";
  basis_note: string | null;
  confidence: Confidence;
}

export interface Impact {
  systems: AffectedSystem[];
  data_categories: AffectedCategory[];
  subjects: AffectedSubjects[];
  /** Never a bare number: the basis travels with the total, always. A null count
   *  with basis "unknown" means the total is genuinely unknowable, not zero. */
  total: { record_count: number | null; count_basis: string };
}

export interface RiskFactor {
  code: string;
  label: string;
  contribution: number;
  detail: string;
  evidenced: boolean;
}

export interface RiskAssessment {
  id: string;
  version: number;
  risk_level: string;
  risk_score: number;
  confidence: Confidence;
  reason: string;
  factors: RiskFactor[];
  /** Material for a person to read, in its own field. NOT a determination that an
   *  obligation applies. */
  regulatory_context: Array<Record<string, unknown>>;
  review_status: string;
}

export interface RiskView {
  current: RiskAssessment | null;
  history: Array<{ version: number; risk_level: string; confidence: Confidence; review_status: string }>;
}

export interface IncidentExecution {
  id: string;
  status: string;
  /** "attested" means a person said they did it. Consiva did not observe it and
   *  never upgrades this to "read_back" on its own. */
  verification_status: string | null;
  performed_by: string | null;
  attestation: string | null;
  rows_affected: number | null;
}

export interface IncidentAction {
  id: string;
  action_kind: string;
  title: string;
  rationale: string;
  expected_result: string;
  target: string | null;
  risk: string;
  status: string;
  requires_approval: boolean;
  blocked_reason: string | null;
  assignee_label: string | null;
  /** "tracked" means a person performs this outside Consiva. */
  execution_mode: string;
  executions: IncidentExecution[];
}

export interface PlanSummary {
  total: number;
  approved: number;
  rejected: number;
  blocked: number;
  completed: number;
  failed: number;
  awaiting_decision: number;
  no_approval_needed: number;
  ready_to_respond: boolean;
  nothing_to_do: boolean;
}

export interface PlanView {
  actions: IncidentAction[];
  summary: PlanSummary;
}

export interface Communication {
  id: string;
  audience: string;
  subject: string;
  body: string | null;
  status: string;
  external: boolean;
  drafted_by_model: string | null;
  approved_at: string | null;
  sent_at: string | null;
}

export interface IncidentReport {
  id: string;
  version: number;
  body_text: string;
  grounded_facts: Array<Record<string, unknown>>;
  status?: string;
  drafted_by_model: string | null;
}

export interface IncidentAuditEntry {
  id: string;
  action: string;
  actor_user_id: string | null;
  before: Record<string, unknown> | null;
  after: Record<string, unknown> | null;
  created_at: string | null;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const token = getToken();
  if (!token) {
    // Already signed out -- tell the shell, or it keeps rendering a console for a
    // session that is gone and every request fails here without ever being sent.
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

  // Not just logout(): the shell has to hear about it, or the console keeps
  // rendering for a session that no longer exists.
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

export const incidentsApi = {
  list(): Promise<Incident[]> {
    return request("/api/v1/incidents");
  },
  get(id: string): Promise<Incident> {
    return request(`/api/v1/incidents/${id}`);
  },
  create(payload: {
    title: string;
    description: string;
    source: string;
    detected_at: string;
    reported_by?: string | null;
    occurred_at?: string | null;
  }): Promise<Incident> {
    return request("/api/v1/incidents", { method: "POST", body: JSON.stringify(payload) });
  },

  // -- investigation --
  listEvidence(id: string): Promise<IncidentEvidence[]> {
    return request(`/api/v1/incidents/${id}/evidence`);
  },
  /** A separate, audited request: who looked at incident evidence is itself
   *  something an investigation may need to answer. */
  getEvidenceDetail(id: string, evidenceId: string): Promise<IncidentEvidence> {
    return request(`/api/v1/incidents/${id}/evidence/${evidenceId}`);
  },
  addEvidence(
    id: string,
    payload: { kind: string; source_system: string; summary: string; observed_at?: string | null },
  ): Promise<IncidentEvidence> {
    return request(`/api/v1/incidents/${id}/evidence`, { method: "POST", body: JSON.stringify(payload) });
  },
  getTimeline(id: string): Promise<TimelineEntry[]> {
    return request(`/api/v1/incidents/${id}/timeline`);
  },
  addSystem(
    id: string,
    payload: { system_name: string; system_kind: string; confidence?: string; notes?: string | null },
  ): Promise<AffectedSystem> {
    return request(`/api/v1/incidents/${id}/systems`, { method: "POST", body: JSON.stringify(payload) });
  },
  addSubjects(
    id: string,
    payload: {
      subject_group: string;
      record_count: number | null;
      count_basis: string;
      basis_note?: string | null;
      confidence?: string;
    },
  ): Promise<AffectedSubjects> {
    return request(`/api/v1/incidents/${id}/subjects`, { method: "POST", body: JSON.stringify(payload) });
  },
  getImpact(id: string): Promise<Impact> {
    return request(`/api/v1/incidents/${id}/impact`);
  },
  getRisk(id: string): Promise<RiskView> {
    return request(`/api/v1/incidents/${id}/risk`);
  },
  /** Queues the derivation work. Containment is never queued -- a person performs
   *  a tracked action and attests to it. */
  analyse(id: string): Promise<{ job_id: string; incident: Incident }> {
    return request(`/api/v1/incidents/${id}/analyse`, { method: "POST" });
  },

  // -- findings, the only route to "confirmed" --
  setFinding(
    id: string,
    payload: { field: "personal_data_involved" | "breach_confirmed"; confidence: Confidence; reason: string },
  ): Promise<Incident> {
    return request(`/api/v1/incidents/${id}/finding`, { method: "POST", body: JSON.stringify(payload) });
  },
  transition(id: string, to_status: string, reason?: string, error_code?: string): Promise<Incident> {
    return request(`/api/v1/incidents/${id}/transition`, {
      method: "POST",
      body: JSON.stringify({ to_status, reason, error_code }),
    });
  },

  // -- response --
  buildPlan(id: string): Promise<PlanView> {
    return request(`/api/v1/incidents/${id}/plan`, { method: "POST" });
  },
  getPlan(id: string): Promise<PlanView> {
    return request(`/api/v1/incidents/${id}/actions`);
  },
  decideAction(
    id: string,
    actionId: string,
    decision: string,
    reason?: string,
  ): Promise<{ summary: PlanSummary; incident: Incident }> {
    return request(`/api/v1/incidents/${id}/actions/${actionId}/decision`, {
      method: "POST",
      body: JSON.stringify({ decision, reason }),
    });
  },
  /** Records what a PERSON did. Not an execution by Consiva. */
  attest(
    id: string,
    actionId: string,
    payload: { performed_by: string; attestation: string },
  ): Promise<IncidentExecution & { note: string | null }> {
    return request(`/api/v1/incidents/${id}/actions/${actionId}/attest`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },
  failAction(id: string, actionId: string, reason: string): Promise<IncidentAction> {
    return request(`/api/v1/incidents/${id}/actions/${actionId}/failed`, {
      method: "POST",
      body: JSON.stringify({ decision: "rejected", reason }),
    });
  },

  // -- communications and report --
  listCommunications(id: string): Promise<Communication[]> {
    return request(`/api/v1/incidents/${id}/communications`);
  },
  draftCommunication(id: string, payload: { audience: string; subject: string }): Promise<Communication> {
    return request(`/api/v1/incidents/${id}/communications`, { method: "POST", body: JSON.stringify(payload) });
  },
  decideCommunication(id: string, commId: string, decision: string, reason?: string): Promise<Communication> {
    return request(`/api/v1/incidents/${id}/communications/${commId}/decision`, {
      method: "POST",
      body: JSON.stringify({ decision, reason }),
    });
  },
  /** Records that a person sent it. Consiva has no outbound provider. */
  markSent(id: string, commId: string): Promise<Communication & { note: string }> {
    return request(`/api/v1/incidents/${id}/communications/${commId}/sent`, { method: "POST" });
  },
  generateReport(id: string): Promise<IncidentReport> {
    return request(`/api/v1/incidents/${id}/report`, { method: "POST" });
  },
  getReport(id: string): Promise<IncidentReport | null> {
    return request(`/api/v1/incidents/${id}/report`);
  },
  close(id: string, summary: string): Promise<Incident> {
    return request(`/api/v1/incidents/${id}/close`, {
      method: "POST",
      body: JSON.stringify({ decision: "approved", reason: summary }),
    });
  },
  getAudit(id: string): Promise<IncidentAuditEntry[]> {
    return request(`/api/v1/incidents/${id}/audit`);
  },
};

/** The intake vocabularies, mirrored from the backend so the form offers only
 *  values the server accepts. Kept in one place rather than inline in the JSX. */
export const INCIDENT_SOURCES = [
  "manual",
  "security_alert",
  "siem",
  "application_monitoring",
  "database_monitoring",
  "access_anomaly",
  "employee_report",
  "vendor_notification",
  "customer_complaint",
  "security_team",
  "authorized_integration",
] as const;

export const EVIDENCE_KINDS = [
  "security_log",
  "access_log",
  "authentication_event",
  "database_event",
  "application_log",
  "system_alert",
  "incident_report",
  "external_evidence",
  "human_observation",
] as const;

export const SYSTEM_KINDS = [
  "application",
  "database",
  "api",
  "cloud_service",
  "storage",
  "server",
  "vendor",
  "other",
] as const;

/** Error codes, UPPERCASE exactly as the backend declares them in
 *  app/agents/breach/schemas/incident.py. Spelled lowercase here once, the
 *  "Not an incident" button 409'd every time it was pressed. */
export const INCIDENT_ERROR_CODES = {
  INVALID_INCIDENT: "INVALID_INCIDENT",
  UNAUTHORIZED_ACCESS: "UNAUTHORIZED_ACCESS",
  EVIDENCE_UNAVAILABLE: "EVIDENCE_UNAVAILABLE",
  INVESTIGATION_FAILED: "INVESTIGATION_FAILED",
  IMPACT_UNKNOWN: "IMPACT_UNKNOWN",
  RISK_ASSESSMENT_FAILED: "RISK_ASSESSMENT_FAILED",
  APPROVAL_REQUIRED: "APPROVAL_REQUIRED",
  ACTION_BLOCKED: "ACTION_BLOCKED",
  ACTION_FAILED: "ACTION_FAILED",
  VERIFICATION_FAILED: "VERIFICATION_FAILED",
  NOTIFICATION_FAILED: "NOTIFICATION_FAILED",
} as const;

export const CONFIDENCE_LEVELS: Confidence[] = ["confirmed", "probable", "possible", "unknown"];

export const COMMUNICATION_AUDIENCES = [
  "internal",
  "management",
  "privacy_team",
  "affected_individual",
  "customer",
  "vendor",
  "regulator",
] as const;
