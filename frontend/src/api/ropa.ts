// Agent 2 (Data Discovery / ROPA) API client. Separate file from client.ts so
// Agent 1's surface stays exactly as it was; both share the same bearer token.

import { getToken, logout } from "./auth";
import { ApiError } from "./types";

const BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "http://127.0.0.1:8000";

export interface RopaRun {
  id: string;
  source_name: string;
  ingest_mode: string;
  status: string;
  tables_scanned: number;
  columns_scanned: number;
  personal_data_elements: number;
  overall_confidence: number | null;
  summary: Record<string, unknown>;
  error: string | null;
}

export interface RopaRecord {
  id: string;
  processing_activity: string;
  version: number;
  status: string;
  review_required: boolean;
  confidence: number | null;
  supersedes_id: string | null;
  payload: {
    processing_activity: string;
    purpose: string;
    data_subjects: string[];
    personal_data_categories: string[];
    data_elements: string[];
    source_systems: string[];
    retention: string;
    business_owner: string;
    access_roles: string[];
    evidence: string[];
    confidence: number;
    review_required: boolean;
    version: number;
  };
}

export interface RopaFinding {
  id: string;
  finding: string;
  gap_status: string;
  severity: string;
  severity_factors: string[];
  confidence: number | null;
  recommendation: string | null;
  review_status: string;
}

export interface RopaChange {
  id: string;
  change_type: string;
  target: string;
  previous_value: string | null;
  current_value: string | null;
  is_material: boolean;
  review_required: boolean;
}

export interface IntegrationKeyCreated {
  id: string;
  name: string;
  key_prefix: string;
  /** Returned exactly once, at creation. Never retrievable again. */
  api_key: string;
  scopes: string[];
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

export const ropaApi = {
  listRuns(): Promise<RopaRun[]> {
    return request("/api/v1/ropa/runs");
  },
  getRun(runId: string): Promise<RopaRun> {
    return request(`/api/v1/ropa/runs/${runId}`);
  },
  getRecords(runId: string): Promise<RopaRecord[]> {
    return request(`/api/v1/ropa/runs/${runId}/records`);
  },
  getFindings(runId: string): Promise<RopaFinding[]> {
    return request(`/api/v1/ropa/runs/${runId}/findings`);
  },
  getChanges(runId: string): Promise<RopaChange[]> {
    return request(`/api/v1/ropa/runs/${runId}/changes`);
  },
  decideRecord(recordId: string, decision: "approved" | "rejected", reason: string) {
    return request<{ id: string; status: string }>(`/api/v1/ropa/records/${recordId}/decision`, {
      method: "POST",
      body: JSON.stringify({ decision, reason }),
    });
  },
  decideFinding(findingId: string, decision: "approved" | "rejected", reason: string) {
    return request<{ id: string; review_status: string }>(`/api/v1/ropa/findings/${findingId}/decision`, {
      method: "POST",
      body: JSON.stringify({ decision, reason }),
    });
  },
  promoteBaseline(runId: string) {
    return request<{ baseline_id: string; source_name: string }>(
      `/api/v1/ropa/runs/${runId}/promote-baseline`,
      { method: "POST" }
    );
  },
  createIntegrationKey(name: string): Promise<IntegrationKeyCreated> {
    return request("/api/v1/ropa/integration-keys", {
      method: "POST",
      body: JSON.stringify({ name }),
    });
  },
};
