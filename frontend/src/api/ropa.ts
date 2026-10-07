// Agent 2 (Data Discovery / ROPA) API client. Separate file from client.ts so
// Agent 1's surface stays exactly as it was; both share the same bearer token.

import { getToken } from "./auth";
import { handleUnauthorized } from "./client";
import { ApiError } from "./types";

const BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "http://127.0.0.1:8000";

export interface RopaRun {
  id: string;
  data_source_id: string | null;
  source_name: string;
  ingest_mode: string;
  status: string;
  tables_scanned: number;
  columns_scanned: number;
  personal_data_elements: number;
  overall_confidence: number | null;
  summary: Record<string, unknown>;
  error: string | null;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
}

/** Statuses the backend still considers in-flight (migrations/0007's CHECK
 * constraint on ropa_discovery_runs.status) -- a run in one of these is
 * worth polling; one in any other status is done. */
export const ACTIVE_RUN_STATUSES = new Set(["pending", "discovering", "analyzing"]);

export interface RopaDataSource {
  id: string;
  name: string;
  connector: string;
  source_type: string;
  config: Record<string, unknown>;
  credential_ref: string | null;
  has_stored_credential: boolean;
  enabled: boolean;
  last_verified_at: string | null;
}

export interface CreateSourceInput {
  name: string;
  connector: string;
  source_type: string;
  config: Record<string, unknown>;
  credential_ref?: string | null;
}

export interface TestConnectionInput {
  connector: string;
  config: Record<string, unknown>;
  credential_ref?: string | null;
  secret?: string | null;
}

export interface TestConnectionResult {
  ok: boolean;
  message: string;
}

export interface RopaDataFlowStep {
  from_node: string;
  to_node: string;
  evidence: string[];
}

export interface RopaVendorProcessor {
  name: string;
  role: string | null;
  purpose: string | null;
  data_shared: string[];
  location: string | null;
  dpa_status: string;
  evidence: string[];
}

export interface RopaTransferInfo {
  is_international_transfer: boolean | null;
  destination_country: string | null;
  evidence: string[];
  review_required: boolean;
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
    storage_locations: string[];
    processors: RopaVendorProcessor[];
    recipients: string[];
    data_flows: RopaDataFlowStep[];
    retention: string;
    business_owner: string;
    access_roles: string[];
    transfer_information: RopaTransferInfo | null;
    evidence: string[];
    confidence: number;
    review_required: boolean;
    version: number;
  };
}

/** The per-column classification trail (migration 0029): why one column got
 * the category it did, not just the activity it rolled up into. */
export interface RopaClassification {
  id: string;
  source: string;
  schema: string | null;
  table: string;
  column: string;
  classification: string;
  data_subject: string;
  confidence: number;
  evidence: string[];
  review_required: boolean;
  review_reason: string | null;
}

export interface IntegrationKeySummary {
  id: string;
  name: string;
  key_prefix: string;
  scopes: string[];
  enabled: boolean;
  created_at: string;
  last_used_at: string | null;
  expires_at: string | null;
  revoked_at: string | null;
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

export const ropaApi = {
  listConnectors(): Promise<{ connectors: string[] }> {
    return request("/api/v1/ropa/connectors");
  },
  listSources(): Promise<RopaDataSource[]> {
    return request("/api/v1/ropa/sources");
  },
  createSource(input: CreateSourceInput): Promise<RopaDataSource> {
    return request("/api/v1/ropa/sources", { method: "POST", body: JSON.stringify(input) });
  },
  testConnection(input: TestConnectionInput): Promise<TestConnectionResult> {
    return request("/api/v1/ropa/sources/test-connection", {
      method: "POST",
      body: JSON.stringify(input),
    });
  },
  setSourceCredential(sourceId: string, secret: string): Promise<void> {
    return request(`/api/v1/ropa/sources/${sourceId}/credential`, {
      method: "POST",
      body: JSON.stringify({ secret }),
    });
  },
  setSourceEnabled(sourceId: string, enabled: boolean): Promise<RopaDataSource> {
    return request(`/api/v1/ropa/sources/${sourceId}/enabled`, {
      method: "POST",
      body: JSON.stringify({ enabled }),
    });
  },
  discoverSource(sourceId: string, idempotencyKey?: string): Promise<RopaRun> {
    const query = idempotencyKey ? `?idempotency_key=${encodeURIComponent(idempotencyKey)}` : "";
    return request(`/api/v1/ropa/sources/${sourceId}/discover${query}`, { method: "POST" });
  },
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
  getClassifications(runId: string): Promise<RopaClassification[]> {
    return request(`/api/v1/ropa/runs/${runId}/classifications`);
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
  listIntegrationKeys(): Promise<IntegrationKeySummary[]> {
    return request("/api/v1/ropa/integration-keys");
  },
  revokeIntegrationKey(keyId: string): Promise<IntegrationKeySummary> {
    return request(`/api/v1/ropa/integration-keys/${keyId}/revoke`, { method: "POST" });
  },
};
