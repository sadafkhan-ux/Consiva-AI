import type {
  AgentRunResponse,
  AuditLogResponse,
  FindingResponse,
  ScanEvidenceResponse,
  ScanResponse,
  ScanStatusResponse,
  StageResponse,
} from "./types";
import { ApiError } from "./types";

// Read from Vite env config, not hardcoded -- see .env.example / .env.development.
// Falls back to the standard local backend address only if no env var is set at all,
// so this still works out of the box for `npm run dev` without extra setup, while
// staying overridable per-environment through the real Vite env mechanism.
const BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "http://127.0.0.1:8000";

let token: string | null = null;
let orgId: string | null = null;

// This backend's ONLY local-dev auth path today is its own existing
// POST /api/v1/dev/demo-token endpoint (hard-gated server-side to
// APP_ENV=development, see backend/app/api/v1/routes/dev.py) -- the same mechanism
// the project's existing static demo page already uses. This is not a bypass and not
// a new insecure endpoint; it is the existing mechanism, reused as instructed.
export async function ensureAuth(): Promise<void> {
  if (token) return;
  const res = await fetch(`${BASE_URL}/api/v1/dev/demo-token`, { method: "POST" });
  if (!res.ok) {
    throw new ApiError(
      res.status,
      "Could not obtain a local dev auth token -- is the backend running with APP_ENV=development?"
    );
  }
  const data = await res.json();
  token = data.token;
  orgId = data.org_id;
}

export function currentOrgId(): string | null {
  return orgId;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  await ensureAuth();
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
    // A network-level failure (backend down, DNS, CORS rejection) never reaches the
    // server at all -- fetch() throws a generic TypeError with no useful detail, so
    // this is the one case where the message here is ours, not the backend's.
    throw new ApiError(0, "Network error -- could not reach the backend. Is it running and reachable at " + BASE_URL + "?");
  }

  if (res.status === 204) return undefined as T;

  const isJson = res.headers.get("content-type")?.includes("application/json");
  const body = isJson ? await res.json().catch(() => null) : await res.text().catch(() => "");

  if (!res.ok) {
    // FastAPI's own error shape is {"detail": "..."} for both our ConsivaError
    // handler and its built-in HTTPException/validation errors -- never surface a
    // raw stack trace, only ever this message.
    const detail =
      typeof body === "object" && body !== null && "detail" in body
        ? typeof body.detail === "string"
          ? body.detail
          : JSON.stringify(body.detail)
        : typeof body === "string" && body
          ? body
          : STATUS_FALLBACKS[res.status] ?? `Request failed with status ${res.status}`;
    throw new ApiError(res.status, detail);
  }

  return body as T;
}

const STATUS_FALLBACKS: Record<number, string> = {
  400: "The request was invalid.",
  401: "Not authenticated.",
  403: "Not authorized for this action.",
  404: "Not found.",
  409: "This conflicts with the current state (e.g. already decided, or analysis already running).",
  422: "The request did not pass validation.",
  429: "Rate limit exceeded.",
  500: "The server encountered an internal error.",
  502: "An upstream service (LLM or notification delivery) failed.",
  503: "The service is temporarily unavailable.",
  504: "The upstream request timed out.",
};

export const api = {
  createScan(url: string, authorized: boolean): Promise<ScanResponse> {
    return request("/api/v1/consent/scans", { method: "POST", body: JSON.stringify({ url, authorized }) });
  },
  getScan(scanId: string): Promise<ScanStatusResponse> {
    return request(`/api/v1/consent/scans/${scanId}`);
  },
  getStages(scanId: string): Promise<StageResponse[]> {
    return request(`/api/v1/consent/scans/${scanId}/stages`);
  },
  getEvidence(scanId: string): Promise<ScanEvidenceResponse> {
    return request(`/api/v1/consent/scans/${scanId}/evidence`);
  },
  analyze(scanId: string): Promise<AgentRunResponse> {
    return request(`/api/v1/consent/scans/${scanId}/analyze`, { method: "POST" });
  },
  getFindings(scanId: string): Promise<FindingResponse[]> {
    return request(`/api/v1/consent/scans/${scanId}/findings`);
  },
  getAudit(scanId: string): Promise<AuditLogResponse[]> {
    return request(`/api/v1/consent/scans/${scanId}/audit`);
  },
  approveFinding(findingId: string, reason: string | null): Promise<{ id: string; status: string }> {
    return request(`/api/v1/consent/findings/${findingId}/approve`, { method: "POST", body: JSON.stringify({ reason }) });
  },
  rejectFinding(findingId: string, reason: string): Promise<{ id: string; status: string }> {
    return request(`/api/v1/consent/findings/${findingId}/reject`, { method: "POST", body: JSON.stringify({ reason }) });
  },
  editFinding(findingId: string, editedPayload: Record<string, unknown>, reason: string): Promise<{ id: string; status: string }> {
    return request(`/api/v1/consent/findings/${findingId}/edit`, {
      method: "POST",
      body: JSON.stringify({ edited_payload: editedPayload, reason }),
    });
  },
};
