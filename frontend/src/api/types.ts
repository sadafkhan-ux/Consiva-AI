// Types mirror the EXISTING backend response models exactly (see
// backend/app/api/v1/routes/consent_scans.py, consent_findings.py, actions.py).
// No field here was invented -- every one traces to a real Pydantic response model.

export interface ScanResponse {
  id: string;
  url: string;
  status: string;
}

export interface ScanStatusResponse extends ScanResponse {
  error: string | null;
  evidence_counts: {
    pages: number;
    forms: number;
    cookies: number;
    trackers: number;
    third_party_services: number;
    policies: number;
  };
}

export interface AgentRunResponse {
  id: string;
  status: string;
}

export interface DpdpReference {
  chunk_id: string;
  section: string | null;
  source_doc: string;
  version: string | null;
}

export interface RecommendationResponse {
  id: string;
  recommendation_text: string;
  priority: string | null;
}

export interface FindingResponse {
  id: string;
  category: string;
  risk_level: string;
  priority: string;
  finding_text: string;
  evidence: string[];
  dpdp_reference: DpdpReference[];
  requires_human_review: boolean;
  status: string; // "pending" | "approved" | "rejected" | "edited"
  recommendations: RecommendationResponse[];
}

export interface StageResponse {
  stage: string;
  status: string; // "pending" | "running" | "completed" | "failed"
  duration_ms: number | null;
  error: string | null;
  metadata: WebsiteScanMetadata | Record<string, unknown>;
}

// The website_scan stage's metadata dict, as populated by
// backend/app/services/scan_service.py -- every key here is real and already
// returned today, nothing added on the frontend side.
export interface WebsiteScanMetadata {
  pages_found?: number;
  cookies_found?: number;
  trackers_found?: number;
  forms_found?: number;
  consent_mechanism?: string; // "banner" | "cmp" | "none" | "unknown"
  cmp_vendor?: string | null;
  cmp_confidence?: number | null;
  cmp_detection_source?: string | null;
  accept_interaction?: string; // "clicked" | "click_failed" | "cmp_not_automatable" | "cmp_not_found" | "page_unreachable"
  reject_interaction?: string;
  cookies_by_consent_state?: Record<string, number>;
  trackers_by_consent_state?: Record<string, number>;
  pages_failed?: number;
  pages_timeout?: number;
  pages_blocked_robots?: number;
  total_scroll_steps?: number;
  accept_scroll_steps?: number;
  reject_scroll_steps?: number;
  policies_found?: number;
  third_party_services_found?: number;
}

export interface AuditLogResponse {
  id: string;
  action: string;
  entity_type: string;
  entity_id: string;
  actor_user_id: string | null;
  agent_run_id: string | null;
  model_name: string | null;
  provider: string | null; // "nvidia" | "groq" -- real value, present on agent_run.completed/failed entries
}

// GET /scans/{id}/evidence -- per-item evidence detail.
export interface EvidencePageItem {
  id: string;
  url: string;
  title: string | null;
}

export interface FormField {
  name: string;
  field_type: string;
  required: boolean;
}

export interface EvidenceFormItem {
  id: string;
  selector: string | null;
  fields: FormField[];
  purpose_guess: string | null;
}

export interface EvidenceCookieItem {
  id: string;
  name: string;
  domain: string | null;
  category: string | null;
  vendor: string | null;
  is_first_party: boolean | null;
  source: string | null;
  consent_states: string[];
}

export interface EvidenceTrackerItem {
  id: string;
  script_src: string;
  vendor: string | null;
  category: string | null;
  source: string | null;
  consent_states: string[];
}

export interface EvidenceThirdPartyItem {
  id: string;
  service_name: string;
  category: string | null;
  domains: string[];
}

export interface EvidencePolicyItem {
  id: string;
  url: string;
  policy_type: string;
}

export interface EvidenceConsentSignalItem {
  mechanism_type: string;
  cmp_vendor: string | null;
  has_reject_all: boolean | null;
  has_granular_choices: boolean | null;
  evidence: Record<string, unknown>;
}

export interface ScanEvidenceResponse {
  pages: EvidencePageItem[];
  forms: EvidenceFormItem[];
  cookies: EvidenceCookieItem[];
  trackers: EvidenceTrackerItem[];
  third_party_services: EvidenceThirdPartyItem[];
  policies: EvidencePolicyItem[];
  consent_signals: EvidenceConsentSignalItem[];
}

export interface ActionResponse {
  id: string;
  finding_id: string;
  action_type: string;
  title: string;
  description: string | null;
  assignee_label: string | null;
  config_payload: Record<string, unknown> | null;
  status: string;
  staged_at: string | null;
  deployed_at: string | null;
  created_at: string;
}

export class ApiError extends Error {
  status: number;
  detail: string;
  constructor(status: number, detail: string) {
    super(`${status}: ${detail}`);
    this.status = status;
    this.detail = detail;
  }
}
