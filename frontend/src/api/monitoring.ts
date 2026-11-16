// System monitoring API client (/api/v1/system/monitoring). Administrators only: the server answers 403 to anyone else.
// Nothing returned here is a secret: keys, tokens and the mail password never leave the server.

import { getToken } from "./auth";
import { handleUnauthorized } from "./client";
import { ApiError } from "./types";

const BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "http://127.0.0.1:8000";

export interface EmailStatus {
  enabled: boolean;
  configured: boolean;
  problem: string | null;
  to: string[];
  cc: string[];
}

export interface HealthRow {
  source: string;
  label: string;
  status: "ok" | "failing" | "unknown";
  detail: string | null;
  latency_ms: number | null;
  checked_at: string;
  last_ok_at: string | null;
  consecutive_failures: number;
}

export interface AlertRow {
  id: string;
  source: string;
  severity: "warning" | "critical";
  status: "open" | "acknowledged" | "resolved";
  title: string;
  detail: string;
  action: string | null;
  first_seen_at: string;
  last_seen_at: string;
  occurrences: number;
  resolved_at: string | null;
  acknowledged_by: string | null;
  acknowledged_at: string | null;
  last_notified_at: string | null;
  notify_count: number;
  notify_status: string | null;
}

export interface MonitoringOverview {
  monitoring_enabled: boolean;
  check_interval_seconds: number;
  email: EmailStatus;
  health: HealthRow[];
  alerts: AlertRow[];
  open_count: number;
}

export interface TestEmailResult {
  status: "sent" | "not_configured" | "failed";
  reason: string | null;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const token = getToken();
  if (!token) {
    handleUnauthorized();
    throw new ApiError(401, "Not signed in.");
  }
  let res: Response;
  try {
    res = await fetch(`${BASE_URL}${path}`, { ...init, headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}`, ...(init?.headers ?? {}) } });
  } catch {
    throw new ApiError(0, `Network error: could not reach the backend at ${BASE_URL}.`);
  }
  const body = await res.json().catch(() => null);
  if (res.status === 401) handleUnauthorized();
  if (!res.ok) {
    const detail = body && typeof body === "object" && "detail" in body ? (typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail)) : `Request failed with status ${res.status}`;
    throw new ApiError(res.status, detail);
  }
  return body as T;
}

export const monitoringApi = {
  overview: () => request<MonitoringOverview>("/api/v1/system/monitoring"),
  runNow: () => request<MonitoringOverview>("/api/v1/system/monitoring/run", { method: "POST" }),
  testEmail: () => request<TestEmailResult>("/api/v1/system/monitoring/test-email", { method: "POST" }),
  acknowledge: (id: string) => request<AlertRow>(`/api/v1/system/monitoring/alerts/${encodeURIComponent(id)}/acknowledge`, { method: "POST" }),
};

/** "3 minutes ago", "2 hours ago", "4 days ago". Falls back to the date for anything that does not parse. */
export function ago(iso: string | null | undefined, now: number = Date.now()): string {
  if (!iso) return "never";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return iso;
  const minutes = Math.max(0, Math.round((now - t) / 60_000));
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes} minute${minutes === 1 ? "" : "s"} ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 48) return `${hours} hour${hours === 1 ? "" : "s"} ago`;
  const days = Math.round(hours / 24);
  return `${days} day${days === 1 ? "" : "s"} ago`;
}
