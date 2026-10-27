// Types mirror the EXISTING backend response models exactly (see
// backend/app/api/v1/routes/consent_scans.py, consent_findings.py, actions.py).
// No field here was invented -- every one traces to a real Pydantic response model.

export interface ScanResponse {
  id: string;
  url: string;
  status: string;
}

/** One row of the recent-scans list (GET /api/v1/consent/scans). */
export interface ScanListItem {
  id: string;
  url: string;
  status: string;
  created_at: string | null;
  completed_at: string | null;
}

// ── Model usage of a scan (GET /api/v1/consent/scans/{id}/usage and .../usage/history; backend app/services/usage_service.py). Shown only in the console's
// usage panel, never in the report or the PDF. A token field that is null is UNAVAILABLE (a model stage with no recorded usage), never zero.
export interface UsageTotals {
  calls: number;
  failed_calls: number;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  total_tokens: number;
  /** Part of the output tokens the provider says was reasoning (already inside output_tokens). Null when no call reported it. */
  reasoning_tokens: number | null;
  priced_calls: number;
  unpriced_calls: number;
  cost: number | null;
  embedding_tokens: number | null;
}

export interface UsageCost {
  currency: string;
  total: number | null;
  input: number | null;
  cache_read: number | null;
  cache_write: number | null;
  output: number | null;
  priced_calls: number;
  unpriced_calls: number;
  complete: boolean;
  price_source: string | null;
}

export interface UsageStage {
  stage: string;
  label: string;
  /** "llm" calls a language model; "embedding" uses the embedding model (no LLM tokens); "non_llm" is ordinary code (zero LLM tokens). */
  kind: "llm" | "embedding" | "non_llm" | string;
  status: "running" | "completed" | "failed" | string;
  runs: number;
  duration_ms: number | null;
  models: string[];
  note: string | null;
  calls: number;
  failed_calls: number;
  input_tokens: number | null;
  output_tokens: number | null;
  cache_read_tokens: number | null;
  cache_write_tokens: number | null;
  total_tokens: number | null;
  reasoning_tokens: number | null;
  priced_calls: number;
  unpriced_calls: number;
  cost: number | null;
  embedding_requests: number | null;
  embedding_tokens: number | null;
}

export interface UsagePageRow {
  url: string;
  calls: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cost: number | null;
}

export interface UsageSharedRow {
  label: string;
  reason: string;
  stage: string | null;
  page_urls: string[];
  calls: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cost: number | null;
}

export interface UsagePages {
  attributed: UsagePageRow[];
  shared: UsageSharedRow[];
  unattributed: UsageSharedRow | null;
}

export interface ScanUsage {
  scan_id: string;
  url: string;
  domain: string;
  scan_status: string;
  state: "running" | "completed" | "failed" | string;
  current_stage: string | null;
  created_at: string | null;
  completed_at: string | null;
  pages_scanned: number | null;
  /** "measured" (per-call records), "stage_records" (a scan made before per-call tracking) or "none". */
  source: "measured" | "stage_records" | "none" | string;
  /** False for a scan that was only crawled: its AI analysis never started, so there is no usage to record (not the same as usage that went missing). */
  analysis_run: boolean;
  totals: UsageTotals;
  cost: UsageCost;
  stages: UsageStage[];
  pages: UsagePages;
  notes: string[];
}

export interface UsageHistoryItem {
  scan_id: string;
  url: string;
  domain: string;
  status: string;
  created_at: string | null;
  completed_at: string | null;
  pages_scanned: number | null;
  calls: number;
  failed_calls: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cost: number | null;
  cost_complete: boolean;
  currency: string;
  source: string;
  analysis_run: boolean;
}

// ── Why two scans of one website scored differently (GET /api/v1/consent/scans/{id}/dpdp-score/compare; backend app/scoring/comparison.py).
export interface ScoreComparisonSide {
  scan_id: string;
  url?: string;
  scanned_at: string | null;
  score_percent: number | null;
  status: "scored" | "insufficient_evidence" | string;
  coverage_percent: number | null;
  applicable_weight: number;
  assessed_weight: number;
  insufficient_reason: string | null;
}

export interface ScoreComparisonRequirement {
  status: string;
  status_label: string;
  points_earned: number;
  weight: number;
  counts_in_score: boolean;
  evidence: string[];
  evidence_ids: string[];
}

export interface ScoreComparisonChange {
  id: string;
  title: string;
  category: string;
  previous: ScoreComparisonRequirement | null;
  latest: ScoreComparisonRequirement | null;
  /** "scan_coverage", "website_behaviour", "scoring_configuration" or "unexplained". */
  cause: string;
  explanation: string;
  score_before: number | null;
  score_after: number | null;
  score_change: number | null;
  score_becomes_available: boolean;
  score_becomes_withheld: boolean;
}

export interface ScoreComparison {
  previous: ScoreComparisonSide;
  latest: ScoreComparisonSide;
  score_change: number | null;
  summary: string;
  causes: string[];
  changes: ScoreComparisonChange[];
  changed_inputs: { group: string; name: string; previous: unknown; latest: unknown }[];
  denominators: { previous: { applicable_weight: number; assessed_weight: number }; latest: { applicable_weight: number; assessed_weight: number }; changed: boolean };
  requirements_missing_from_one_scan: string[];
  attribution: { method: string; sums_to_total: boolean };
  methodology_version: string;
  assessed_on: string;
  methodology_note: string;
}

/** Why a scan could not be completed, in one vocabulary for every site (backend scanner/failure_catalog.py). Derived by the server, so older scans carry it too. */
export interface ScanFailure {
  category: string;
  code: string;
  title: string;
  explanation: string;
  retryable: boolean;
  transient: boolean;
  owner: "website_owner" | "scanner_operator" | "none" | string;
  next_action: string;
  detail: string;
  url: string | null;
  /** In plain words, for a person: what happened, what they can do, and a few words for a label. The fields above are for developers. */
  user_message?: string;
  what_you_can_do?: string;
  short?: string;
}

export interface ScanStatusResponse extends ScanResponse {
  error: string | null;
  /** QUEUED, RUNNING, COMPLETED, COMPLETED_WITH_WARNINGS, PARTIALLY_COMPLETED, BLOCKED, FAILED or CANCELLED. Absent on an older API. */
  outcome?: string | null;
  failure?: ScanFailure | null;
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
  /** The server's own timestamps (ISO 8601); absent on older responses. */
  started_at?: string | null;
  completed_at?: string | null;
}

// The website_scan stage's metadata dict, as populated by
// backend/app/services/scan_service.py -- every key here is real and already
// returned today, nothing added on the frontend side.

export interface LanguageSignals {
  pages_checked: number;
  pages_with_indic_script: number;
  indic_scripts: Record<string, number>;
  switcher_scripts: string[];
  hreflang: string[];
  html_lang: string | null;
}

export interface SecuritySignals {
  pages_total: number;
  pages_https: number;
  homepage_https?: boolean;
  homepage_headers?: Record<"hsts" | "csp" | "x_frame_options" | "x_content_type_options" | "referrer_policy" | "permissions_policy", string | null>;
}

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
  /** Why discovered pages were not scanned: one entry per page (capped at 50) and the full count per reason. */
  skipped_pages?: { url: string; reason: string }[];
  skipped_by_reason?: Record<string, number>;
  framed_sites?: string[];
  total_scroll_steps?: number;
  accept_scroll_steps?: number;
  reject_scroll_steps?: number;
  policies_found?: number;
  third_party_services_found?: number;
  /** What the pages showed about language (backend scanner/page_signals.py): an observation, never a finding. Absent on a scan made before this was recorded. */
  language_signals?: LanguageSignals;
  /** What the pages and the homepage's response showed about transport and browser safeguards. Absent on an older scan. */
  security_signals?: SecuritySignals;
  // The crawl's own page accounting (crawler._crawl_full_site diagnostics), merged into this stage.
  page_limit?: number | null;
  stop_reason?: string | null; // "queue_exhausted" | "page_limit" | "deadline" | "cancelled"
  pages_discovered?: number;
  pages_attempted?: number;
  pages_scanned?: number;
  pages_not_attempted_budget_exhausted?: number;
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
  /** The HTTP status of a page that loaded; null for an attempt that did not. Absent on scans made before it was returned. */
  http_status?: number | null;
  /** How the page was found: "seed" (the entered address), "link", "sitemap" -- or "failed" / "timeout" for an attempt that did not load. */
  discovered_via?: string | null;
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
  /** Where the form posts to (null when it posts to its own page). Absent on an older response. */
  submit_url?: string | null;
  /** The page the form was on (null when not recorded). Absent on an older response. */
  page_url?: string | null;
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
  // Absent or null means the scan did not record it (scans made before these fields
  // existed) -- shown as "Not available", never defaulted.
  path?: string | null;
  expiry?: string | null;
  /** The provider of a recognised cookie, what it is for (from the provider's own description), and how that was known: "catalogue", "category" or "unknown". */
  provider?: string | null;
  purpose?: string;
  purpose_source?: "catalogue" | "category" | "unknown" | string;
  /** True for a session cookie. `lifetime_days` is days from when it was first seen to its expiry; null for a session cookie or when the sighting time was not recorded. */
  is_session?: boolean;
  lifetime_days?: number | null;
  secure?: boolean | null;
  http_only?: boolean | null;
  same_site?: string | null;
  observations?: CookieObservation[];
}

/** Where and when one consent-state pass first saw a cookie (backend
 *  scanner/schemas.py CookieObservation). `page_url` is null when the scan could not
 *  name the page exactly; `candidate_page_urls` then lists the pages it came from. */
export interface CookieObservation {
  consent_state: string;
  page_url: string | null;
  observed_at: string | null;
  method: "set_cookie_header" | "single_page_load" | "concurrent_batch" | string;
  source_request_url: string | null;
  candidate_page_urls: string[];
}

export interface EvidenceTrackerItem {
  id: string;
  script_src: string;
  vendor: string | null;
  category: string | null;
  source: string | null;
  consent_states: string[];
  /** Page the request/script was observed on; absent/null when the scan did not record it. */
  page_url?: string | null;
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
  /** The backend's own reading (rules/consent_observation.py): false when the consent tool's banner was never displayed to the scanner (what it offers is then null), true when it was, null when unknown or no tool. Absent on an older API. */
  banner_displayed?: boolean | null;
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

/** The homepage preview of a scan (GET .../screenshot; backend app/api/v1/routes/consent_scans.py). `data` is the JPEG as base64; without a picture,
 *  `message` says why in client-safe words. It carries the scan id and the domain it belongs to. */
export interface ScanScreenshot {
  scan_id: string;
  domain: string;
  status: "ok" | "blocked" | "timeout" | "failed" | "not_captured" | string;
  available: boolean;
  message: string | null;
  page_url: string | null;
  mime: string | null;
  width: number | null;
  height: number | null;
  captured_at: string | null;
  data: string | null;
}

/** What the scan's privacy and cookie policies say (GET .../policy-analysis; backend app/policy). Informational: it never changes the score. */
export interface PolicyRequirementResult {
  id: string;
  title: string;
  group: string;
  /** "required" (the Act or Rules require it to be given or published) or "practice" (a duty stands behind it, but the policy is not stated to have to carry it). */
  basis: "required" | "practice" | string;
  provisions: string[];
  status: "verified" | "partial" | "not_found" | "not_applicable" | string;
  quote: string;
  source_url: string | null;
  detail: string;
  fix: string;
  /** Found beside the quoted passage (emails, phone numbers, links), for the contact points; null otherwise. */
  contacts?: { emails: string[]; phones: string[]; urls: string[] } | null;
  /** The grievance response period in days, when a period was quoted. */
  response_days?: number | null;
}
export interface PolicyAnalysis {
  status: "analysed" | "unavailable" | "not_run" | string;
  reason: string | null;
  sector: string | null;
  documents: { url: string; policy_type: string; characters: number; source: string; truncated: boolean }[];
  notes: string[];
  summary: { applicable: number; verified: number; partial: number; not_found: number; not_applicable: number; quotes_rejected: number; completeness_percent: number | null } | null;
  requirements: PolicyRequirementResult[];
  limitations: string[];
  model: Record<string, unknown> | null;
}

/** The proposed DPDP Compliance Score for one scan (backend app/scoring/dpdp_score.py; docs/DPDP_COMPLIANCE_SCORE.md). */
export type DpdpStatus = "met" | "partially_met" | "not_met" | "unable_to_verify" | "not_applicable";

export interface DpdpRequirementResult {
  id: string;
  category: string;
  title: string;
  provisions: string[];
  weight: number;
  effective: string;
  in_force: boolean;
  status: DpdpStatus;
  points_earned: number;
  counts_in_score: boolean;
  observations: string[];
  evidence_ids: string[];
  rule_ids: string[];
  exclusion_reason: string | null;
  default_weight: number;
  description: string;
  criteria: string;
  expected_evidence: string;
  pages: string[];
  sufficient: boolean | null;
  sufficiency_reason: string;
  limitation: string;
  timing: "in_force" | "future";
  duplicate_of: string | null;
  why_it_matters: string;
  remediation: string;
  verification: string;
  check_type: "website" | "business" | "policy";
  applicability: string;
  confidence: "high" | "medium" | "none";
  scoring_rationale: string;
  legal_note: string;
  penalty_points: number | null;
  score_contribution_percent: number | null;
  coverage_share_percent: number | null;
  /** How the check was (or could be) answered; absent on a score from an older server. */
  method?: "website_scan" | "policy_text" | "organisation_records" | string;
  /** The records that would settle a check left unverified; absent on a score from an older server. */
  needed_evidence?: string[];
}

export interface DpdpLedgerMetric {
  key: string;
  name: string;
  numerator: number;
  denominator: number;
  percent: number | null;
  formula: string;
  meaning: string;
}

export interface DpdpStatusRule {
  status: DpdpStatus;
  label: string;
  rule: string;
  in_numerator: string;
  in_denominator: string;
  on_coverage: string;
  count: number;
  weight: number;
  check_ids: string[];
}

export interface DpdpLedger {
  total_checks: number;
  applicable_checks: number;
  assessed_checks: number;
  passed_checks: number;
  partial_checks: number;
  failed_checks: number;
  unable_checks: number;
  not_applicable_checks: number;
  total_weight: number;
  applicable_weight: number;
  assessed_weight: number;
  earned_weight: number;
  penalty_weight: number;
  unable_weight: number;
  not_applicable_weight: number;
  metrics: DpdpLedgerMetric[];
  category_metrics: DpdpLedgerMetric[];
  status_rules: DpdpStatusRule[];
  penalty_note: string;
  security_score_note: string;
  not_applicable: { id: string; reason: string; duplicate_of: string | null }[];
}

export interface DpdpCategoryScore {
  category: string;
  applicable_weight: number;
  assessed_weight: number;
  points_earned: number;
  score_percent: number | null;
  coverage_percent: number | null;
  status: "scored" | "not_assessed";
}

export interface DpdpScoreBlock {
  basis: "readiness" | "in_force";
  status: "scored" | "insufficient_evidence";
  score_percent: number | null;
  points_earned: number;
  points_assessed: number;
  applicable_weight: number;
  assessed_weight: number;
  unable_to_verify_weight: number;
  coverage_percent: number | null;
  insufficient_reason: string | null;
  caution: string | null;
  categories: DpdpCategoryScore[];
}

export interface DpdpStateCounts {
  pre_consent: number;
  post_accept: number;
  post_reject: number;
}

/** The three kinds of technical signal, counted apart. None of them is a finding on its own. */
export interface DpdpEvidenceSummary {
  network_requests: number;
  known_tracker_requests: number;
  unclassified_requests: number;
  cookies: number;
  known_tracker_cookies: number;
  unclassified_cookies: number;
  network_requests_by_state: DpdpStateCounts;
  known_tracker_requests_by_state: DpdpStateCounts;
  cookies_by_state: DpdpStateCounts;
  pages_scanned: number;
  note: string;
}

export interface DpdpGapItem {
  requirement_id: string;
  title: string;
  status: "not_met" | "partially_met";
  timing: "in_force" | "future";
  applies_from: string;
  provisions: string[];
  weight: number;
  points: number;
  what_the_scan_observed: string[];
  pages: string[];
  evidence_ids: string[];
  rule_ids: string[];
  why_it_matters: string;
  recommended_action: string;
  how_to_verify: string;
}

export interface DpdpMissingEvidenceItem {
  requirement_id: string;
  title: string;
  provisions: string[];
  weight: number;
  reason: string;
  kind: "scan_limit" | "organisational";
}

export interface DpdpBreakdown {
  /** Not Met, and the duty is in force now. */
  verified_gaps: DpdpGapItem[];
  /** Not Met, but the duty is not yet in force: a readiness gap, never a current violation. */
  readiness_gaps: DpdpGapItem[];
  partially_satisfied: DpdpGapItem[];
  missing_evidence: DpdpMissingEvidenceItem[];
  uncertain_count: number;
}

export type DpdpNeed = "business_context" | "legal_interpretation" | "more_evidence";

export interface DpdpReviewItem {
  id: string;
  category: "uncertain" | "missing_evidence";
  topic: string;
  needs: DpdpNeed[];
  known: string[];
  missing: string[];
  decision: string;
  provisions: string[];
  evidence_ids: string[];
  requirement_ids: string[];
}

export interface DpdpHumanReview {
  statement: string;
  items: DpdpReviewItem[];
}

export type DpdpGroupKey = "confirmed" | "needs_review" | "unable_to_verify" | "future_readiness";

export interface DpdpFindingEntry {
  id: string;
  group: DpdpGroupKey;
  title: string;
  requirement_ids: string[];
  legal_references: string[];
  outcome: string;
  severity: "High" | "Medium" | "Low" | null;
  severity_note: string;
  pages: string[];
  consent_states: string[];
  observed: string[];
  evidence_ids: string[];
  rule_ids: string[];
  effective_date: string | null;
  timing_note: string;
  business_impact: string;
  explanation: string;
  what_to_review: string[];
  missing: string[];
  how_to_verify: string;
  remediation: string;
  signal_caveat: string;
  confidence: string;
  related_ids: string[];
}

export interface DpdpFindingGroups {
  confirmed: DpdpFindingEntry[];
  needs_review: DpdpFindingEntry[];
  unable_to_verify: DpdpFindingEntry[];
  future_readiness: DpdpFindingEntry[];
  upcoming: { date: string; requirement_ids: string[] }[];
  notes: Record<DpdpGroupKey, string>;
  counts: Record<DpdpGroupKey, number>;
}

export interface DpdpMethodWeight {
  id: string;
  title: string;
  category: string;
  check_type: "website" | "business" | "policy";
  weight: number;
  default_weight: number;
  rationale: string;
  applicability: string;
  effective: string;
  applies_now: boolean;
}

export interface DpdpMethodCategory {
  name: string;
  rationale: string;
  weight: number;
  share_percent: number | null;
  requirement_ids: string[];
}

export interface DpdpAssessment {
  scan_url: string | null;
  main_domain: string | null;
  scanned_at: string | null;
  assessed_on: string;
  pages_scanned: string[];
  page_limit: number;
  states_tested: string[];
  states_not_tested: string[];
  nature_note: string;
}

export interface DpdpRemediationStep {
  rank: number;
  requirement_id: string;
  title: string;
  outcome: string;
  timing: string;
  priority: string;
  priority_reason: string;
  action: string;
  how_to_verify: string;
  legal_references: string[];
  /** What fixing this one gap does to the readiness score, from the score's own weights (null when the score is withheld), and what the gap means in plain words. */
  current_percent?: number | null;
  percent_if_fixed?: number | null;
  gain_percent?: number | null;
  impact?: string;
}

export interface DpdpMethodology {
  version: string;
  weights_source: "default" | "custom";
  weights_note: string;
  score_formula: string;
  coverage_formula: string;
  points_rules: string[];
  reliability_rule: string;
  duplicate_rule: string;
  timing_rule: string;
  scope_rule: string;
  legal_mapping_version: string;
  legal_sources: { name: string; reference: string }[];
  weights: DpdpMethodWeight[];
  categories: DpdpMethodCategory[];
  dedup: { group: string; requirement_ids: string[]; explanation: string }[];
}

/** Which kind of website this looks like and which provisions depend on it (backend app/scoring/sector.py). Informational: it never changes the score. */
export interface SectorSignal { sector: string; source: string; match: string; seen_in: string; points: number }
export interface SectorNote { id: string; title: string; provisions: string[]; text: string; observed: string[]; action: string }
export interface SectorDeclaration { flag: string; requirement_id: string; label: string; why: string; declared: boolean | null }
export interface SectorAssessment {
  sector: string;
  label: string;
  confidence: "high" | "medium" | "low" | "declared";
  summary: string;
  signals: SectorSignal[];
  also_possible: string[];
  notes: SectorNote[];
  declarations: SectorDeclaration[];
  limitations: string[];
  sector_scores?: Record<string, number>;
  /** True when the organisation declared its own sector; `detected_sector` is then what the scan alone made of the site. */
  declared?: boolean;
  detected_sector?: string | null;
  agrees_with_scan?: boolean | null;
}

export interface DpdpScoreResponse {
  methodology_version: string;
  as_of: string;
  requirements: DpdpRequirementResult[];
  readiness: DpdpScoreBlock;
  in_force: DpdpScoreBlock;
  disclaimer: string;
  status_counts: Record<DpdpStatus, number>;
  evidence_summary: DpdpEvidenceSummary;
  breakdown: DpdpBreakdown;
  human_review: DpdpHumanReview;
  finding_groups: DpdpFindingGroups;
  methodology: DpdpMethodology;
  assessment: DpdpAssessment;
  remediation_plan: DpdpRemediationStep[];
  ledger: DpdpLedger;
  /** Absent on a score from an older server. */
  sector?: SectorAssessment | null;
  thresholds: { min_coverage_percent: number; min_assessed_requirements: number };
  out_of_scope: { provision: string; reason: string }[];
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
