// Enterprise scanner API client (/api/v1/enterprise). Its own file, like regwatch.ts and dsr.ts, so no other agent's surface changes.
// The Enterprise product is separate from the 5-page consent scanner: nothing here touches its scan, score or report.

import { getToken } from "./auth";
import { handleUnauthorized } from "./client";
import { ApiError } from "./types";

const BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "http://127.0.0.1:8000";

export type EnterpriseScanState = "queued" | "running" | "completed" | "failed" | "cancelled";
export type FrontierState = "pending" | "in_progress" | "done" | "failed" | "excluded";

export interface UrlCounts {
  pending: number;
  in_progress: number;
  done: number;
  failed: number;
  excluded: number;
  total: number;
}

export interface EnterpriseScanStatus {
  scan_id: string;
  website_url: string;
  status: EnterpriseScanState;
  urls: UrlCounts;
  progress: number;
  stop_reason: string | null;
  error: string | null;
  options: Record<string, unknown>;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
}

export interface CoverageUrl {
  url: string;
  depth: number;
  status: FrontierState;
  discovery_sources: string[];
  http_status: number | null;
  status_reason: string | null;
  last_error: string | null;
}

export interface CoverageResponse {
  scan_id: string;
  urls: UrlCounts;
  items: CoverageUrl[];
  total: number;
  limit: number;
  offset: number;
  note: string;
}

export interface CreateEnterpriseScanResponse {
  scan_id: string;
  status: EnterpriseScanState;
  message: string;
  options: Record<string, unknown>;
}

/** A scan in one of these states will not change again. */
export const FINAL_STATES: ReadonlySet<EnterpriseScanState> = new Set(["completed", "failed", "cancelled"]);

export /** What a gateway answers with no JSON body (an HTML error page from nginx): said in words instead of "Request failed with status 502". */
const GATEWAY_MESSAGES: Record<number, string> = {
  502: "The service is temporarily unreachable (bad gateway). It has been recorded; please try again in a moment.",
  503: "The service is temporarily unavailable. It has been recorded; please try again in a moment.",
  504: "The service took too long to answer (timeout). Please try again.",
};

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
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}`, ...(init?.headers ?? {}) },
    });
  } catch {
    throw new ApiError(0, `Network error -- could not reach the backend at ${BASE_URL}.`);
  }
  const body = await res.json().catch(() => null);
  if (res.status === 401) handleUnauthorized();
  if (!res.ok) {
    const detail =
      body && typeof body === "object" && "detail" in body
        ? typeof body.detail === "string"
          ? body.detail
          : JSON.stringify(body.detail)
        : GATEWAY_MESSAGES[res.status] ?? `Request failed with status ${res.status}`;
    const ref = res.headers.get("X-Error-ID") || res.headers.get("X-Request-ID");
    throw new ApiError(res.status, ref && res.status >= 500 && !detail.includes(ref) ? `${detail} (reference ${ref})` : detail);
  }
  return body as T;
}

export interface VerificationToken {
  domain: string;
  txt_record: string;
  instructions: string;
}

/** The host a scan is filed under: lower-case, no scheme, path or port, and without a leading "www." (the same rule the server uses, so verifying this host is what the scan looks for). */
export function siteHost(input: string): string {
  let v = input.trim().toLowerCase();
  v = v.replace(/^[a-z][a-z0-9+.-]*:\/\//, "");
  v = v.split(/[/?#]/)[0].split(":")[0];
  return v.replace(/^www\./, "");
}

export const enterpriseApi = {
  verificationToken: (domain: string) => request<VerificationToken>(`/api/v1/consent-agent/websites/verification-token?domain=${encodeURIComponent(domain)}`),
  verifyDomain: (domain: string) => request<{ domain: string; verified_at: string }>("/api/v1/consent-agent/websites/verify", { method: "POST", body: JSON.stringify({ domain }) }),
  createScan: (websiteUrl: string, authorized: boolean) =>
    request<CreateEnterpriseScanResponse>("/api/v1/enterprise/scans", {
      method: "POST",
      body: JSON.stringify({ website_url: websiteUrl, authorized }),
    }),
  status: (scanId: string) => request<EnterpriseScanStatus>(`/api/v1/enterprise/scans/${encodeURIComponent(scanId)}/status`),
  coverage: (scanId: string, opts: { status?: FrontierState | ""; limit?: number; offset?: number } = {}) => {
    const q = new URLSearchParams();
    if (opts.status) q.set("status", opts.status);
    q.set("limit", String(opts.limit ?? 50));
    q.set("offset", String(opts.offset ?? 0));
    return request<CoverageResponse>(`/api/v1/enterprise/scans/${encodeURIComponent(scanId)}/coverage?${q.toString()}`);
  },
  cancel: (scanId: string) => request<{ scan_id: string; status: string; message: string }>(`/api/v1/enterprise/scans/${encodeURIComponent(scanId)}/cancel`, { method: "POST" }),
  resume: (scanId: string) => request<{ scan_id: string; status: string; message: string }>(`/api/v1/enterprise/scans/${encodeURIComponent(scanId)}/resume`, { method: "POST" }),
};

/** A validation error arrives as a JSON list of {msg}; show the messages, not the JSON. */
export function readableDetail(detail: string): string {
  if (!detail.startsWith("[")) return detail;
  try {
    const items = JSON.parse(detail) as { msg?: string }[];
    const msgs = items.map((i) => (i.msg ?? "").replace(/^Value error,\s*/i, "")).filter(Boolean);
    return msgs.length ? msgs.join(" ") : detail;
  } catch {
    return detail;
  }
}

/** What to tell a person when a request fails. A 404 on any enterprise route means the product is switched off on this server. */
export function enterpriseErrorMessage(e: unknown): string {
  if (e instanceof ApiError) {
    if (e.status === 404 && /not found/i.test(e.detail) && !/scan/i.test(e.detail)) return "The Enterprise scanner is not enabled on this server (ENTERPRISE_ENABLED is false).";
    if (e.status === 403) return e.detail || "You are not allowed to do this. Full-site scans need an organisation administrator and a verified domain.";
    return readableDetail(e.detail) || e.message;
  }
  return e instanceof Error ? e.message : String(e);
}

// ---------------------------------------------------------------------------------------------------------------------------------------------------------------
// Enterprise workspace (docs/ENTERPRISE_API_CONTRACT.md). Every number here comes from the server; `null` means "not recorded yet".

export type Completion = "queued" | "crawling" | "crawled" | "analysing" | "partial" | "complete" | "failed" | "cancelled";
export type AnalysisState = "not_started" | "running" | "completed" | "partial" | "failed";
export type ParameterState = "met" | "partly_met" | "not_met" | "not_assessed" | "not_applicable" | "needs_records" | "human_review";
export type Severity = "high" | "medium" | "low" | "info";

export interface Page<T> {
  items: T[];
  total: number;
  offset: number;
  limit: number;
}

export interface ScanListItem {
  scan_id: string;
  website_url: string;
  status: string;
  analysis_status: string | null;
  completion: Completion | null;
  /** The official score: the 96-parameter (method 3) score the report prints. */
  score: number | null;
  score_version?: string | null;
  coverage: number | null;
  created_at: string;
  completed_at: string | null;
}

export interface GroupScore {
  label: string;
  points_available: number | null;
  points_in_play: number | null;
  earned_points: number | null;
}

export interface EnterpriseScore {
  score: number | null;
  name: string;
  version: string;
  basis: "readiness" | "assessment";
  coverage: number | null;
  reason_no_score: string | null;
  earned_points: number | null;
  points_in_play: number | null;
  points_available?: number | null;
  not_yet_in_force: string[];
  by_group: Record<string, GroupScore>;
  needs_attention: Record<string, string[]>;
  as_of: string | null;
  calculated_at: string | null;
}

export interface UrlDeclaration {
  discovered: number | null;
  eligible: number | null;
  crawled: number | null;
  analysed: number | null;
  pending: number | null;
  excluded: number | null;
  failed: number | null;
  redirected?: number | null;
  inaccessible?: number | null;
  unsupported?: number | null;
  duplicates_merged?: number | null;
  awaiting_analysis?: number | null;
  analysis_failed?: number | null;
  excluded_by_reason?: Record<string, number>;
  not_recorded?: number | null;
  pages_with_limits?: number | null;
  pages_not_fully_analysed?: number | null;
  pages_not_sent_to_model?: number | null;
}

export interface Declaration {
  crawl_status: string;
  analysis_status: string;
  completion: Completion;
  urls: UrlDeclaration;
  documents: { discovered: number | null; extracted: number | null; partly_extracted: number | null; unreadable: number | null; extraction_not_recorded: number | null };
  evidence: { records: number | null; sources: number | null; analysed: number | null };
  analysis_tasks?: { done: number | null; failed: number | null; pending: number | null };
  parameters: {
    total: number | null; assessed: number | null; not_assessed: number | null; not_applicable: number | null; awaiting_human_review: number | null; needs_records: number | null; recorded: number | null;
    /** Method version 2: the scored result of every parameter. Absent on a scan scored before it. */
    pass?: number; partially_pass?: number; fail?: number; fail_not_verified?: number;
  };
}

export interface ScanSummary {
  scan_id: string;
  website_url: string;
  domain: string;
  crawl_status: EnterpriseScanState;
  analysis_status: AnalysisState;
  /** Why the crawl stopped (a limit, a block, a cancel); null when it ran to its end. */
  stop_reason?: string | null;
  completion: Completion;
  /** The earlier 51-parameter result, kept for compatibility; not the official score. */
  score: EnterpriseScore | null;
  /** The official score: the 96-parameter (method 3) result, as the report prints it. */
  official_score?: { score: number | null; marks_earned: number; applicable_max: number; version: string; basis: string; as_of: string; calculated_at: string } | null;
  declaration: Declaration | null;
  legal_review_status: string | null;
  report_available: boolean;
  options: Record<string, unknown>;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
  error: string | null;
}

export interface ParameterView {
  id: string;
  name: string;
  group: string;
  group_label: string;
  kind: string;
  points: number | null;
  partial_allowed: boolean;
  human_review_required: boolean;
  conditional: boolean;
  legal_refs: string[];
  in_force: string | null;
  in_force_on_as_of: boolean;
  penalty_ceiling_cr: number | null;
  schedule_item: number | null;
  state: ParameterState;
  earned: number | null;
  in_play: number | null;
  note: string | null;
  confidence: string | number | null;
  assessor: string | null;
  model: string | null;
  reviewed_by: string | null;
  review_reason: string | null;
  evidence_refs: string[];
  adjustment: unknown;
  assessed_at: string | null;
  /** Method version 2: "Pass", "Partially Pass" or "Fail" for `state`. */
  status?: string;
  /** What the assessor recorded before it was resolved to one of the three (for example "not_applicable"). */
  assessed_state?: string;
  /** False when the result rests on "could not be verified" or "needs records", not on collected evidence. */
  on_evidence?: boolean;
}

export interface ParameterHistoryItem {
  state: ParameterState;
  note?: string | null;
  assessor?: string | null;
  model?: string | null;
  reviewed_by?: string | null;
  review_reason?: string | null;
  assessed_at?: string | null;
  [key: string]: unknown;
}

export interface FindingView {
  id: string;
  finding_key: string;
  parameter_id: string;
  title: string;
  description: string | null;
  severity: Severity;
  state: string;
  legal_refs: string[];
  affected_urls: string[];
  affected_count: number | null;
  evidence_refs: string[];
  expected: string | null;
  observed: string | null;
  reason: string | null;
  confidence: string | null;
  recommendation: string | null;
  human_review: boolean;
  verification_status: string | null;
  schedule_item: number | null;
  penalty_ceiling_cr: number | null;
}

export interface UrlItem {
  url: string;
  final_url: string | null;
  depth: number | null;
  status: FrontierState;
  analysis_status: string | null;
  http_status: number | null;
  status_reason: string | null;
  last_error: string | null;
  discovery_sources: string[];
  is_document: boolean;
  extraction_status: string | null;
  // What was extracted from the page and how much of it was analysed (migration 0057). Absent on a page that was never read.
  title?: string | null;
  text_chars?: number | null;
  content_hash?: string | null;
  extracted_at?: string | null;
  evidence_note?: string | null;
  category?: string | null;
  category_reason?: string | null;
  also_categories?: string[];
  slug_kinds?: string[];
  chunks_created?: number | null;
  chunks_analysed?: number | null;
  analysis_note?: string | null;
}

export interface DocumentItem {
  url: string;
  final_url: string | null;
  content_type: string | null;
  status: string | null;
  extraction_status: string | null;
  extraction_method: string | null;
  pages_read: number | null;
  pages_needing_ocr: number | null;
  analysis_status: string | null;
  status_reason: string | null;
}

export interface EvidenceItem {
  id?: string;
  evidence_id?: string;
  kind: string;
  source_url: string | null;
  excerpt?: string | null;
  [key: string]: unknown;
}

export interface EvidenceRecord extends EvidenceItem {
  content: string | null;
  truncated: boolean;
}

export interface ReportVersion {
  version: number;
  status: string;
  page_count: number | null;
  byte_size: number | null;
  sha256: string | null;
  generated_at: string | null;
  score_result_id: string | null;
}

export interface ChangesView {
  scan_id: string;
  against: string;
  comparable: boolean;
  urls?: { added: number | null; removed: number | null; added_examples?: string[]; removed_examples?: string[] };
  policies?: { changed: { url: string; previous_hash: string | null; current_hash: string | null }[] };
  cookies?: { added: string[]; removed: string[] };
  trackers?: { added: string[]; removed: string[] };
  forms?: { added: number | null; removed: number | null };
  consent?: { changes: { test: string; from: string | null; to: string | null }[] };
  findings?: { new: unknown[]; resolved: unknown[]; changed: { finding_key: string; from: string | null; to: string | null }[] };
  coverage?: { previous: Record<string, unknown> | null; current: Record<string, unknown> | null };
  score?: { previous: number | null; current: number | null; delta: number | null };
}

const SCANS = "/api/v1/enterprise/scans";
const sid = (id: string) => `${SCANS}/${encodeURIComponent(id)}`;

function query(params: Record<string, string | number | undefined | null>): string {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null && v !== "") q.set(k, String(v));
  const s = q.toString();
  return s ? `?${s}` : "";
}

export interface PageOpts {
  offset?: number;
  limit?: number;
}

const DASH = "/api/v1/enterprise/dashboard";

export type DashboardAgent = "five_page" | "enterprise";

export interface DashboardScan {
  agent: DashboardAgent;
  scan_id: string;
  website_url: string;
  outcome: "completed" | "failed" | "pending" | "blocked";
  detail: string;
  reason: string | null;
  started_at: string | null;
  finished_at: string | null;
  data_cleared: boolean;
}

export interface DashboardSummary {
  kpis: { scans_total: number; scans_completed: number; scans_failed: number; scans_pending: number; scans_blocked: number; scans_five_page: number; scans_enterprise: number };
  last_scan: DashboardScan | null;
  history: { items: DashboardScan[]; total: number; offset: number; limit: number };
  definitions: { completed: string; failed: string; pending: string; blocked: string };
  cached: boolean;
}

export interface RetentionStatus {
  grace_minutes: number;
  keeps: string;
  clears: string;
  when: string;
}

export const enterpriseDashboard = {
  summary: (o: { outcome?: string; agent?: string; q?: string; offset?: number; limit?: number; refresh?: boolean } = {}) =>
    request<DashboardSummary>(`${DASH}/summary${query({ outcome: o.outcome && o.outcome !== "all" ? o.outcome : undefined, agent: o.agent && o.agent !== "all" ? o.agent : undefined, q: o.q, offset: o.offset ?? 0, limit: o.limit ?? 10, refresh: o.refresh ? "true" : undefined })}`),
  retention: () => request<RetentionStatus>(`${DASH}/retention`),
};

const KB = "/api/v1/enterprise/policy-kb";

export interface PolicyKbMetrics {
  websites_monitored: number;
  documents: { active: number; removed: number; stale: number; versions: number };
  sync: { new: number; changed: number; unchanged: number; removed: number; url_changed: number; extraction_failed: number; sync_failed: number; marked_stale: number; awaiting_processing: number; failed_versions: number; partial_extractions: number; last_successful_sync: string | null };
  embeddings: { chunks: number; active_chunks: number; generated: number; reused: number; missing: number };
  retrieval: { queries: number; keyword_only_fallbacks: number };
  llm: { passages_sent_to_model: number; calls_saved: number; characters_sent: number; characters_not_sent: number; tokens_sent_estimate: number; tokens_saved_estimate: number; token_figures_are: string };
}

export interface PolicyDocument {
  id: string;
  domain: string;
  url: string;
  doc_type: string;
  status: "active" | "removed";
  stale: boolean;
  stale_reason: string | null;
  current_version: number | null;
  versions: number;
  title: string | null;
  text_chars: number | null;
  chunk_count: number | null;
  extraction_status: string | null;
  extraction_note: string | null;
  sync_status: string | null;
  last_verified_at: string | null;
  extracted_at: string | null;
}

export const policyKb = {
  metrics: (domain?: string) => request<PolicyKbMetrics>(`${KB}/metrics${query({ domain })}`),
  documents: (domain?: string) => request<{ items: PolicyDocument[]; total: number }>(`${KB}/documents${query({ domain })}`),
};

export const enterpriseScans = {
  list: (o: PageOpts = {}) => request<Page<ScanListItem>>(`${SCANS}${query({ offset: o.offset ?? 0, limit: o.limit ?? 25 })}`),
  summary: (id: string) => request<ScanSummary>(`${sid(id)}/summary`),
  declaration: (id: string) => request<Declaration>(`${sid(id)}/declaration`),
  parameters: (id: string) => request<{ items: ParameterView[] } | ParameterView[]>(`${sid(id)}/parameters`),
  parameterHistory: (id: string, pid: string) => request<{ items: ParameterHistoryItem[] } | ParameterHistoryItem[]>(`${sid(id)}/parameters/${encodeURIComponent(pid)}/history`),
  review: (id: string, pid: string, state: ParameterState, reason: string) =>
    request<ParameterView>(`${sid(id)}/parameters/${encodeURIComponent(pid)}/review`, { method: "POST", body: JSON.stringify({ state, reason }) }),
  findings: (id: string, o: PageOpts & { severity?: string } = {}) => request<Page<FindingView>>(`${sid(id)}/findings${query({ severity: o.severity, offset: o.offset ?? 0, limit: o.limit ?? 25 })}`),
  evidence: (id: string, o: PageOpts & { kind?: string; source_url?: string } = {}) =>
    request<Page<EvidenceItem>>(`${sid(id)}/evidence${query({ kind: o.kind, source_url: o.source_url, offset: o.offset ?? 0, limit: o.limit ?? 25 })}`),
  evidenceRecord: (id: string, evidenceId: string) => request<EvidenceRecord>(`${sid(id)}/evidence/${encodeURIComponent(evidenceId)}`),
  urls: (id: string, o: PageOpts & { status?: string; analysis_status?: string; q?: string } = {}) =>
    request<Page<UrlItem>>(`${sid(id)}/urls${query({ status: o.status, analysis_status: o.analysis_status, q: o.q?.trim(), offset: o.offset ?? 0, limit: o.limit ?? 25 })}`),
  documents: (id: string, o: PageOpts = {}) => request<Page<DocumentItem>>(`${sid(id)}/documents${query({ offset: o.offset ?? 0, limit: o.limit ?? 25 })}`),
  changes: (id: string, against: string) => request<ChangesView>(`${sid(id)}/changes${query({ against })}`),
  analyze: (id: string) => request<{ scan_id: string; analysis_status: string; message: string }>(`${sid(id)}/analyze`, { method: "POST" }),
  generateReport: (id: string) => request<{ scan_id: string; version: number; status: string }>(`${sid(id)}/report`, { method: "POST" }),
  reports: (id: string) => request<{ items: ReportVersion[] }>(`${sid(id)}/reports`),
  cancel: (id: string) => request<{ scan_id: string; status: string; message: string }>(`${sid(id)}/cancel`, { method: "POST" }),
  resume: (id: string) => request<{ scan_id: string; status: string; message: string }>(`${sid(id)}/resume`, { method: "POST" }),
  /** The method 3 (96-parameter) result: the score the PDF report prints. */
  method3: (id: string) => request<Method3Result>(`${sid(id)}/method3`),
};

export type Method3Status = "pass" | "partial" | "fail" | "not_applicable" | "information";

export interface Method3Line {
  parameter_id: string;
  category: string;
  name: string;
  rule: string | null;
  max: number;
  earned: number | null;
  status: Method3Status;
  status_label: string;
  severity: string | null;
  explanation: string;
  applicability: string | null;
}

export interface Method3Category {
  label: string;
  parameters: number;
  applicable: number;
  max: number;
  /** The category's marks before Not applicable parameters are taken out. */
  max_all?: number;
  earned: number;
  pct: number | null;
  counts: Partial<Record<Method3Status, number>>;
}

/**
 * The method 3.0.0 result (96 parameters) as stored by the backend, from GET /scans/{id}/method3. This is the official Enterprise score: the PDF
 * report prints this same stored row, so the screen and the report always agree. Score = marks earned / applicable maximum x 100, one decimal.
 */
export interface Method3Result {
  id?: string;
  name?: string;
  version: string;
  as_of: string;
  score: number | null;
  marks_earned: number;
  applicable_max: number;
  not_applicable_max?: number;
  max_total?: number;
  basis?: "readiness" | "assessment";
  not_yet_in_force?: string[];
  counts: Partial<Record<Method3Status, number>>;
  parameters: number;
  scored_parameters?: number;
  /** Parameters whose result a person should confirm. */
  review?: string[];
  categories: Record<string, Method3Category>;
  lines: Method3Line[];
  human_review_pending?: string[];
  human_review_max?: number;
  legal_review_status?: string | null;
  created_at: string;
}

/** The report PDF needs the bearer token, so it is fetched to a blob (a plain link would be refused). */
export async function fetchEnterpriseReportPdf(scanId: string, version?: number): Promise<{ blob: Blob; filename: string }> {
  const token = getToken();
  if (!token) {
    handleUnauthorized();
    throw new ApiError(401, "Not signed in.");
  }
  let res: Response;
  try {
    res = await fetch(`${BASE_URL}${sid(scanId)}/report.pdf${query({ version })}`, { headers: { Authorization: `Bearer ${token}`, Accept: "application/pdf" } });
  } catch {
    throw new ApiError(0, `Network error -- could not reach the backend at ${BASE_URL}.`);
  }
  if (res.status === 401) handleUnauthorized();
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    const detail = body && typeof body === "object" && "detail" in body && typeof body.detail === "string" ? body.detail : res.status === 404 ? "No report has been generated for this scan yet." : `Request failed with status ${res.status}`;
    throw new ApiError(res.status, detail);
  }
  const blob = await res.blob();
  const named = /filename="?([^";]+)"?/i.exec(res.headers.get("content-disposition") ?? "")?.[1];
  return { blob, filename: named ?? `consiva-enterprise-report-${scanId.slice(0, 8)}${version ? `-v${version}` : ""}.pdf` };
}

/** The official (method 3.0.0) score of a scan, or null when there is none: not calculated yet (404) or the scan's results were cleared (410). Any other error is thrown. */
export async function officialScore(scanId: string): Promise<Method3Result | null> {
  try {
    return await enterpriseScans.method3(scanId);
  } catch (e) {
    if (e instanceof ApiError && (e.status === 404 || e.status === 410)) return null;
    throw e;
  }
}
