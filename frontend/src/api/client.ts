import type {
  AgentRunResponse,
  AuditLogResponse,
  FindingResponse,
  DpdpScoreResponse,
  ScanEvidenceResponse,
  PolicyAnalysis,
  ScanListItem,
  ScoreComparison,
  ScanUsage,
  UsageHistoryItem,
  ScanScreenshot,
  ScanResponse,
  ScanStatusResponse,
  StageResponse,
} from "./types";
import { getProfile, getToken, logout } from "./auth";
import { ApiError } from "./types";

// Read from Vite env config, not hardcoded -- see .env.example / .env.development.
// Falls back to the standard local backend address only if no env var is set at all,
// so this still works out of the box for `npm run dev` without extra setup, while
// staying overridable per-environment through the real Vite env mechanism.
const BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "http://127.0.0.1:8000";

// Auth now comes from a real login (api/auth.ts -> POST /api/v1/auth/login).
// This previously called POST /api/v1/dev/demo-token, which is hard-gated to
// APP_ENV=development server-side and so forced the entire deployment into
// development mode -- letting anyone who reached the URL mint a token. The
// dev endpoint still exists for local scripting; the app no longer depends on it.
export async function ensureAuth(): Promise<void> {
  if (!getToken()) {
    throw new ApiError(401, "Not signed in.");
  }
}

export function currentOrgId(): string | null {
  return getProfile()?.org_id ?? null;
}

/** Signed out because the token was rejected, so the shell can show the login screen. */
export type UnauthorizedHandler = () => void;

let onUnauthorized: UnauthorizedHandler | null = null;

export function setUnauthorizedHandler(handler: UnauthorizedHandler | null): void {
  onUnauthorized = handler;
}

/**
 * Drop the dead token AND tell the shell, so it returns to the login screen.
 *
 * Every agent's API client must call this on a 401, not just `logout()`. Clearing
 * the token without telling the shell leaves the app in a state that looks signed in
 * and is not: the header still shows your email, the console still renders, and every
 * request fails locally with "Not signed in." before it reaches the server -- with no
 * way back except finding the Sign out button. Agents 2, 3 and 4 all did exactly that
 * until a real session expired on the Breach tab and showed it.
 */
export function handleUnauthorized(): void {
  logout();
  onUnauthorized?.();
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
    // A network-level failure (backend down, DNS, CORS rejection) never reaches the
    // server at all -- fetch() throws a generic TypeError with no useful detail, so
    // this is the one case where the message here is ours, not the backend's.
    throw new ApiError(
      0,
      "Network error -- could not reach the backend" + (BASE_URL ? ` at ${BASE_URL}` : "") + ". Check your connection and that the service is running.",
    );
  }

  if (res.status === 204) return undefined as T;

  const isJson = res.headers.get("content-type")?.includes("application/json");
  const body = isJson ? await res.json().catch(() => null) : await res.text().catch(() => "");

  if (res.status === 401) {
    // The token expired or was rejected -- clear it and let the shell fall back
    // to the login screen rather than leaving a dead session in place.
    handleUnauthorized();
  }

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

/**
 * The PDF report the server prints for one completed scan (GET /api/v1/consent/scans/{id}/report.pdf), as the file's bytes and the name the
 * server gave it. The server renders the same report page the web shows, so the file and the page carry the same values.
 */
export async function fetchReportPdf(scanId: string, variant: "free" | "full" | "long" = "long"): Promise<{ blob: Blob; filename: string }> {
  const token = getToken();
  if (!token) {
    handleUnauthorized();
    throw new ApiError(401, "Not signed in.");
  }
  let res: Response;
  try {
    res = await fetch(`${BASE_URL}/api/v1/consent/scans/${encodeURIComponent(scanId)}/report.pdf?variant=${variant}`, { headers: { Authorization: `Bearer ${token}`, Accept: "application/pdf" } });
  } catch {
    throw new ApiError(0, "Network error -- could not reach the backend" + (BASE_URL ? ` at ${BASE_URL}` : "") + ". Check your connection and that the service is running.");
  }
  if (res.status === 401) handleUnauthorized();
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    const detail = body && typeof body === "object" && "detail" in body && typeof body.detail === "string" ? body.detail : STATUS_FALLBACKS[res.status] ?? `Request failed with status ${res.status}`;
    throw new ApiError(res.status, detail);
  }
  const blob = await res.blob();
  if (!(res.headers.get("content-type") ?? "").includes("application/pdf")) throw new ApiError(502, "The server did not return a PDF.");
  const named = /filename="?([^";]+)"?/i.exec(res.headers.get("content-disposition") ?? "")?.[1];
  return { blob, filename: named ?? `consiva-privacy-report-${variant}-${scanId.slice(0, 8)}.pdf` };
}

/** Saves a blob as a file through a temporary link, then releases it. */
export function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
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
  getScreenshot(scanId: string): Promise<ScanScreenshot> {
    return request(`/api/v1/consent/scans/${scanId}/screenshot`);
  },
  getPolicyAnalysis(scanId: string): Promise<PolicyAnalysis> {
    return request(`/api/v1/consent/scans/${scanId}/policy-analysis`);
  },
  getScanUsage(scanId: string): Promise<ScanUsage> {
    return request(`/api/v1/consent/scans/${scanId}/usage`);
  },
  getUsageHistory(limit = 20): Promise<UsageHistoryItem[]> {
    return request(`/api/v1/consent/scans/usage/history?limit=${limit}`);
  },
  listScans(limit = 15): Promise<ScanListItem[]> {
    return request(`/api/v1/consent/scans?limit=${limit}`);
  },
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
  compareDpdpScore(scanId: string, against?: string): Promise<ScoreComparison> {
    const query = against ? `?against=${encodeURIComponent(against)}` : "";
    return request(`/api/v1/consent/scans/${scanId}/dpdp-score/compare` + query);
  },
  getDpdpScore(scanId: string): Promise<DpdpScoreResponse> {
    return request(`/api/v1/consent/scans/${scanId}/dpdp-score`);
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
