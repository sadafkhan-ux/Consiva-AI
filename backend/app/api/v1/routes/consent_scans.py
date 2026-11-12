import base64
import logging
import uuid
from dataclasses import asdict
from datetime import date, datetime
from types import SimpleNamespace
from typing import Any, Literal
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.exceptions import (
    ConsivaError,
    NotFoundError,
    ReviewerRequiredError,
    ScanNotReadyError,
)
from app.core.security import CurrentUser, get_current_user
from app.db.repositories import (
    audit_repository,
    finding_repository,
    scan_repository,
    stage_repository,
)
from app.db.session import get_db
from app.llm.schemas import DpdpReference
from app.scoring import domain_score
from app.scoring import requirements as requirements_reading
from app.scoring.sector import SECTORS
from app.services import (
    analysis_service,
    assessment_service,
    consent_agent_api_service,
    report_approval_service,
    report_cache_service,
    report_pdf_service,  # noqa: F401  (tests patch it through this module)
    scan_service,
    usage_service,
)

router = APIRouter(prefix="/api/v1/consent/scans", tags=["consent-scans"])


class CreateScanRequest(BaseModel):
    url: str
    authorized: bool = False

    @field_validator("url")
    @classmethod
    def _url_must_be_http_with_host(cls, value: str) -> str:
        """A bare domain is accepted and promoted to https://. People type the thing
        they say out loud -- "gignaati.com" -- and this used to reject it outright,
        because urlparse() reads a schemeless string as a PATH and leaves netloc empty.
        What the scan actually needs is a seed URL, so the host is normalised INTO one
        here rather than the requirement being pushed back onto whoever is typing.

        http:// is still accepted when given explicitly; only the unspecified case
        defaults, and it defaults to https. The host must still contain a dot, so a
        typo like "gignaati" stays a clean 422 at the edge instead of travelling down
        to a DNS failure mid-scan. None of this widens what may be scanned: the SSRF
        check in scan_service (assert_safe_url) runs afterwards either way and still
        rejects private, loopback and link-local addresses.
        """
        candidate = value.strip()
        if not candidate:
            raise ValueError("url must not be empty")
        if "://" not in candidate:
            candidate = f"https://{candidate}"

        parsed = urlparse(candidate)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("url must be an absolute http(s) URL, e.g. https://example.com")
        if not parsed.hostname or "." not in parsed.hostname:
            raise ValueError("url must include a full hostname, e.g. https://example.com")
        return candidate


class ScanResponse(BaseModel):
    id: uuid.UUID
    url: str
    # Both, because they answer different questions and neither is derivable from the
    # other without repeating this service's own normalisation rules in the client.
    # `url` is the exact page this scan crawled -- the seed, with scheme and path, and
    # the thing to show next to the evidence. `domain` is the site the scan is FILED
    # under (websites.domain): what ownership verification, the scan history and the
    # monitoring baseline are keyed on, after "www." and the port are folded away. A
    # caller that posted "www.example.com/pricing" gets back both the URL it asked for
    # and "example.com", which is the identity everything else in the platform uses.
    domain: str
    status: str


class ScanStatusResponse(ScanResponse):
    error: str | None
    evidence_counts: dict
    # `status` above is the SCAN. These describe the ANALYSIS, which is a separate
    # step that may not have run, may still be running, or may have failed -- and
    # without them all three look identical to a clean result: status `completed`,
    # zero findings. A consumer reading that as a clean bill of health is the exact
    # false-negative the rest of this platform is built to refuse.
    analysis_status: str | None = None
    findings_complete: bool = False
    # The lifecycle word and the shared description of why a scan could not be completed (scanner/failure_catalog.py): derived from what is stored, so older scans carry them too.
    outcome: str | None = None
    failure: dict | None = None


class AgentRunResponse(BaseModel):
    id: uuid.UUID
    status: str


class RecommendationResponse(BaseModel):
    id: uuid.UUID
    recommendation_text: str
    priority: str | None


class FindingResponse(BaseModel):
    id: uuid.UUID
    category: str
    risk_level: str
    priority: str
    finding_text: str
    evidence: list
    dpdp_reference: list[DpdpReference]
    requires_human_review: bool
    status: str
    recommendations: list[RecommendationResponse]


class StageResponse(BaseModel):
    stage: str
    status: str
    duration_ms: int | None
    error: str | None
    metadata: dict
    # When the server started and finished the stage. The page used to time the whole scan with the BROWSER's clock,
    # which keeps running while a laptop sleeps or a tab is frozen: a 21 s pipeline was reported as 8,413 s.
    started_at: datetime | None = None
    completed_at: datetime | None = None


class AuditLogResponse(BaseModel):
    id: uuid.UUID
    action: str
    entity_type: str
    entity_id: uuid.UUID
    actor_user_id: uuid.UUID | None
    agent_run_id: uuid.UUID | None
    model_name: str | None
    # Real value from audit_logs.after["provider"] (set in
    # agents/consent_agent/nodes/audit.py from the actual agent_runs.llm_provider
    # column) -- was already persisted, just not previously surfaced via this route.
    # None for every audit event that isn't an agent_run.completed/failed entry.
    provider: str | None = None


# --- Per-item evidence detail (frontend tracker/cookie/form/policy tables) ---------
# scan.evidence_counts above only ever returns aggregate numbers. These mirror the
# exact dict shape scan_repository._serialize_evidence() already produces internally
# for the agent's own normalize node -- no new data, no new query logic, just a
# read-only view onto evidence that already exists, exposed because a real frontend
# table needs actual rows, not just a count.
class EvidencePageItem(BaseModel):
    id: str
    url: str
    title: str | None
    http_status: int | None = None
    discovered_via: str | None = None
    # Where the page ended up after redirects and the Content-Type it was served with; null on scans made before they were recorded.
    final_url: str | None = None
    content_type: str | None = None
    # Why the page was chosen (its kind and where it was found), worked out when the evidence is read; null for a page that did not load.
    selection_reason: str | None = None


class UrlInventorySummary(BaseModel):
    total: int = 0
    pending: int = 0
    in_progress: int = 0
    completed: int = 0
    failed: int = 0
    partially_scanned: int = 0
    excluded: int = 0


class UrlInventoryItem(BaseModel):
    id: uuid.UUID
    domain: str
    original_url: str
    normalized_url: str
    final_url: str | None = None
    page_title: str | None = None
    http_status: int | None = None
    content_type: str | None = None
    discovery_source: str
    discovery_sources: list[str] = []
    crawl_depth: int
    scan_status: str
    status_reason: str | None = None
    failure_reason: str | None = None
    discovered_at: datetime
    scan_started_at: datetime | None = None
    scan_completed_at: datetime | None = None
    last_updated_at: datetime


class UrlInventoryResponse(BaseModel):
    """Every URL a scan discovered and what became of it. `summary` counts the whole inventory; `urls` is one page of what matches the filters. A scan made
    before the inventory existed returns zero counts and no rows, never invented ones."""

    summary: UrlInventorySummary
    urls: list[UrlInventoryItem]
    total: int
    limit: int
    offset: int


class AssessmentCheck(BaseModel):
    id: int
    ref: str | None = None
    category: str | None = None          # the scorecard's category (Cookie Consent, Privacy Notice, Forms & Security); None for the other engines
    name: str
    kind: Literal["dpdp", "practice", "technical", "transparency"]
    result: Literal["Passed", "Partially Passed", "Failed", "Not Assessed", "Not Applicable"]
    awarded: int | float | None = None     # whole numbers from the Scanner Score; halves only under the ten-check rollback
    max: int | float | None = None
    marks_text: str
    evidence: str
    source_url: str | None = None
    explanation: str
    legal: str | None = None
    completed: bool
    limitation: str | None = None
    basis: str
    quality: str
    coverage: str | None = None


class AssessmentCounts(BaseModel):
    passed: int
    partial: int
    failed: int
    not_assessed: int
    not_applicable: int
    # Parameters a Quick Scan cannot reach: listed, never scored, and not counted as "could not be assessed" (they are the scan type's limit, said once).
    beyond_scan_type: int = 0


class AssessmentRecommendation(BaseModel):
    id: int
    text: str


class AssessmentPages(BaseModel):
    discovered: int | None = None
    scanned: int
    failed: int
    skipped: int


class AssessmentResponse(BaseModel):
    """The assessment of one scan and its score: THE score. The web page, the email, the dashboard and every PDF print this result and nothing else. It is the Scanner Score
    (scoring/domain_score.py: seven domains, 100 points, every result out of 100) unless SCORING_ENGINE=ten_check is set for rollback. `score` is a whole number 0 to 100 (null when
    nothing could be assessed); `score_text` is how it is written everywhere ("20/100"); `scanner` carries the band, coverage and evidence confidence that must be shown with it.
    Show a score as final only when `final` is true: it is false while the scan is still running or the policy reading has not finished, because the checks that read the policy are then
    left out and the number would change."""

    scan_id: uuid.UUID
    methodology_version: str
    scan_status: str
    policy_analysis: Literal["done", "running", "failed", "not_run"]
    final: bool
    score: int | float | None
    score_text: str
    total_text: str
    awarded: int | float
    applicable_max: int | float
    counts: AssessmentCounts
    incomplete: int
    limited_basis: bool
    summary: str
    formula: str
    checks: list[AssessmentCheck]
    recommendations: list[AssessmentRecommendation]
    pages: AssessmentPages
    limitations: list[str]
    # Which engine produced this ("scanner", or "ten_check" after a rollback) and, for the Scanner Score, the whole result: band, coverage, evidence confidence,
    # every parameter with its state and evidence, findings, removals and what a Quick Scan cannot reach. None under the ten-check engine.
    engine: Literal["scorecard", "scanner", "ten_check"] = "ten_check"
    scanner: dict[str, Any] | None = None


class EvidenceFormItem(BaseModel):
    id: str
    selector: str | None
    fields: list
    purpose_guess: str | None
    # Where the form posts to; only present on the console's evidence (used to flag a form that posts over plain http).
    submit_url: str | None = None
    # The page the form was on; None when not recorded.
    page_url: str | None = None


class EvidenceCookieObservation(BaseModel):
    consent_state: str
    page_url: str | None = None
    observed_at: str | None = None
    method: str
    source_request_url: str | None = None
    candidate_page_urls: list[str] = []


class EvidenceCookieItem(BaseModel):
    id: str
    name: str
    domain: str | None
    category: str | None
    vendor: str | None
    is_first_party: bool | None
    source: str | None
    consent_states: list[str]
    # NULL means "not recorded" (a scan from before these were captured), never a default.
    path: str | None = None
    expiry: str | None = None
    secure: bool | None = None
    http_only: bool | None = None
    same_site: str | None = None
    observations: list[EvidenceCookieObservation] = []
    # Worked out from the cookie's name and attributes; purpose_source says how far to trust the purpose
    # ("catalogue" = a recognised cookie, "category" = only its classification, "unknown" = could not be identified).
    provider: str | None = None
    purpose: str | None = None
    purpose_source: str | None = None
    is_session: bool | None = None
    lifetime_days: float | None = None


class EvidenceTrackerItem(BaseModel):
    id: str
    script_src: str
    vendor: str | None
    category: str | None
    source: str | None
    consent_states: list[str]
    # The page this was observed on; None when the scan did not record one.
    page_url: str | None = None


class EvidenceThirdPartyItem(BaseModel):
    id: str
    service_name: str
    category: str | None
    domains: list


class EvidencePolicyItem(BaseModel):
    id: str
    url: str
    policy_type: str
    # How much of the page's text the scan holds: "extracted", "partial" (cut at the storage limit) or "empty" (nothing usable was captured). None on an older response.
    text_state: str | None = None
    text_characters: int | None = None


class EvidenceConsentSignalItem(BaseModel):
    mechanism_type: str
    cmp_vendor: str | None
    has_reject_all: bool | None
    has_granular_choices: bool | None
    evidence: dict
    # False when the consent tool's banner was never displayed to the scanner (what it "offers" is then null, not false); True when it was; null when unknown or no consent tool.
    banner_displayed: bool | None = None


class ScanEvidenceResponse(BaseModel):
    pages: list[EvidencePageItem]
    forms: list[EvidenceFormItem]
    cookies: list[EvidenceCookieItem]
    trackers: list[EvidenceTrackerItem]
    third_party_services: list[EvidenceThirdPartyItem]
    policies: list[EvidencePolicyItem]
    consent_signals: list[EvidenceConsentSignalItem]


# --- DPDP requirements reading (statuses and evidence only; app/scoring/requirements.py). The score is the ten-check assessment above. -------------
class DpdpRequirementResult(BaseModel):
    id: str
    category: str
    title: str
    provisions: list[str]
    effective: date
    in_force: bool
    status: str
    observations: list[str]
    evidence_ids: list[str]
    rule_ids: list[str]
    exclusion_reason: str | None = None
    severity: str
    description: str
    criteria: str
    expected_evidence: str
    pages: list[str]
    sufficient: bool | None
    sufficiency_reason: str
    limitation: str
    timing: str
    duplicate_of: str | None = None
    why_it_matters: str
    remediation: str
    verification: str
    check_type: str
    applicability: str
    confidence: str
    legal_note: str
    # How the check was (or could be) answered: website_scan, policy_text or organisation_records; and, while it is unverified, the records that would settle it.
    method: str = "website_scan"
    needed_evidence: list[str] = []


class DpdpStateCounts(BaseModel):
    pre_consent: int
    post_accept: int
    post_reject: int


class DpdpEvidenceSummary(BaseModel):
    network_requests: int
    known_tracker_requests: int
    unclassified_requests: int
    cookies: int
    known_tracker_cookies: int
    unclassified_cookies: int
    network_requests_by_state: DpdpStateCounts
    known_tracker_requests_by_state: DpdpStateCounts
    cookies_by_state: DpdpStateCounts
    pages_scanned: int
    note: str


class DpdpGapItem(BaseModel):
    requirement_id: str
    title: str
    status: str
    timing: str
    applies_from: date
    provisions: list[str]
    what_the_scan_observed: list[str]
    pages: list[str]
    evidence_ids: list[str]
    rule_ids: list[str]
    why_it_matters: str
    recommended_action: str
    how_to_verify: str


class DpdpMissingEvidenceItem(BaseModel):
    requirement_id: str
    title: str
    provisions: list[str]
    reason: str
    kind: str


class DpdpBreakdown(BaseModel):
    verified_gaps: list[DpdpGapItem]
    readiness_gaps: list[DpdpGapItem]
    partially_satisfied: list[DpdpGapItem]
    missing_evidence: list[DpdpMissingEvidenceItem]
    uncertain_count: int


class DpdpReviewItem(BaseModel):
    id: str
    category: str
    topic: str
    needs: list[str]
    known: list[str]
    missing: list[str]
    decision: str
    provisions: list[str]
    evidence_ids: list[str]
    requirement_ids: list[str]


class DpdpHumanReview(BaseModel):
    statement: str
    items: list[DpdpReviewItem]


class DpdpFindingEntry(BaseModel):
    id: str
    group: str
    title: str
    requirement_ids: list[str]
    legal_references: list[str]
    outcome: str
    severity: str | None
    severity_note: str
    pages: list[str]
    consent_states: list[str]
    observed: list[str]
    evidence_ids: list[str]
    rule_ids: list[str]
    effective_date: date | None
    timing_note: str
    business_impact: str
    explanation: str
    what_to_review: list[str]
    missing: list[str]
    how_to_verify: str
    remediation: str
    signal_caveat: str
    confidence: str
    related_ids: list[str]


class DpdpUpcomingDate(BaseModel):
    date: date
    requirement_ids: list[str]


class DpdpFindingGroups(BaseModel):
    confirmed: list[DpdpFindingEntry]
    needs_review: list[DpdpFindingEntry]
    unable_to_verify: list[DpdpFindingEntry]
    future_readiness: list[DpdpFindingEntry]
    upcoming: list[DpdpUpcomingDate]
    notes: dict[str, str]
    counts: dict[str, int]


class DpdpMethodDedup(BaseModel):
    group: str
    requirement_ids: list[str]
    explanation: str


class DpdpLegalSource(BaseModel):
    name: str
    reference: str


class DpdpAssessment(BaseModel):
    scan_url: str | None
    main_domain: str | None
    scanned_at: str | None
    assessed_on: date
    pages_scanned: list[str]
    page_limit: int
    states_tested: list[str]
    states_not_tested: list[str]
    nature_note: str


class DpdpRemediationStep(BaseModel):
    rank: int
    requirement_id: str
    title: str
    outcome: str
    timing: str
    priority: str
    priority_reason: str
    action: str
    how_to_verify: str
    legal_references: list[str]
    # What the gap means for the organisation, in plain words ("" when there is no stated impact for this check).
    impact: str = ""


class DpdpMethodology(BaseModel):
    """How the requirements were read, in words. Nothing numeric."""
    duplicate_rule: str
    timing_rule: str
    scope_rule: str
    legal_mapping_version: str
    legal_sources: list[DpdpLegalSource]
    dedup: list[DpdpMethodDedup]


class DpdpStatusRule(BaseModel):
    status: str
    label: str
    rule: str
    count: int
    check_ids: list[str]


class DpdpLedger(BaseModel):
    """How many checks are in each state. Counts only: the one score is the ten-check assessment."""
    total_checks: int
    applicable_checks: int
    assessed_checks: int
    passed_checks: int
    partial_checks: int
    failed_checks: int
    unable_checks: int
    not_applicable_checks: int
    status_rules: list[DpdpStatusRule]
    not_applicable: list[dict]


class SectorSignalModel(BaseModel):
    sector: str
    source: str
    match: str
    seen_in: str
    points: int


class SectorNoteModel(BaseModel):
    id: str
    title: str
    provisions: list[str]
    text: str
    observed: list[str]
    action: str


class SectorDeclarationModel(BaseModel):
    flag: str
    requirement_id: str
    label: str
    why: str
    declared: bool | None = None


class SectorAssessmentModel(BaseModel):
    sector: str
    label: str
    confidence: str
    summary: str
    signals: list[SectorSignalModel]
    also_possible: list[str]
    notes: list[SectorNoteModel]
    declarations: list[SectorDeclarationModel]
    limitations: list[str]
    sector_scores: dict[str, int] = {}
    declared: bool = False
    detected_sector: str | None = None
    agrees_with_scan: bool | None = None


class DpdpRequirementsResponse(BaseModel):
    """The DPDP requirements reading of one scan: statuses, evidence, findings, review items and the remediation plan. It carries no score, weight, point,
    percentage or coverage figure. The one score is the ten-check assessment (GET /assessment)."""
    requirements_version: str
    as_of: date
    requirements: list[DpdpRequirementResult]
    disclaimer: str
    status_counts: dict[str, int]
    evidence_summary: DpdpEvidenceSummary
    breakdown: DpdpBreakdown
    human_review: DpdpHumanReview
    finding_groups: DpdpFindingGroups
    methodology: DpdpMethodology
    assessment: DpdpAssessment
    remediation_plan: list[DpdpRemediationStep]
    ledger: DpdpLedger
    sector: SectorAssessmentModel | None = None
    out_of_scope: list[dict]


class UsageTotals(BaseModel):
    calls: int
    failed_calls: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    total_tokens: int
    reasoning_tokens: int | None = None
    priced_calls: int
    unpriced_calls: int
    cost: float | None = None
    embedding_tokens: int | None = None


class UsageCost(BaseModel):
    currency: str
    total: float | None = None
    input: float | None = None
    cache_read: float | None = None
    cache_write: float | None = None
    output: float | None = None
    priced_calls: int
    unpriced_calls: int
    complete: bool
    price_source: str | None = None


class UsageStage(BaseModel):
    """One pipeline stage that ran. `kind`: llm (calls a language model), embedding (embedding model, no LLM tokens), non_llm (ordinary code: zero LLM tokens).
    A token field is null when the stage calls a model but no usage was recorded: unavailable, never zero."""
    stage: str
    label: str
    kind: str
    status: str
    runs: int
    duration_ms: int | None = None
    models: list[str] = []
    note: str | None = None
    calls: int
    failed_calls: int
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    total_tokens: int | None = None
    reasoning_tokens: int | None = None
    priced_calls: int = 0
    unpriced_calls: int = 0
    cost: float | None = None
    embedding_requests: int | None = None
    embedding_tokens: int | None = None
    reused: bool = False
    reused_from_scan: str | None = None
    error: str | None = None


class UsageCall(BaseModel):
    """One recorded model call: the breakdown behind the panel's numbers. Counts and identifiers only, never a prompt or a response."""
    stage: str
    label: str
    agent: str | None = None
    provider: str
    model: str
    status: str
    attempts: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    reasoning_tokens: int | None = None
    total_tokens: int
    duration_ms: int | None = None
    cost: float | None = None
    page_urls: list[str] = []
    error_type: str | None = None
    at: datetime | None = None


class UsageError(BaseModel):
    where: str
    stage: str | None = None
    message: str


class UsageResults(BaseModel):
    """What was saved for the scan, read from its stage records: whether its evidence, analysis, findings and audit can be opened."""
    evidence: bool = False
    analysis: bool = False
    findings: bool = False
    audit: bool = False


class UsagePageRow(BaseModel):
    url: str
    calls: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cost: float | None = None


class UsageSharedRow(BaseModel):
    label: str
    reason: str
    stage: str | None = None
    page_urls: list[str] = []
    calls: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cost: float | None = None


class UsagePages(BaseModel):
    attributed: list[UsagePageRow]
    shared: list[UsageSharedRow]
    unattributed: UsageSharedRow | None = None


class UsageResponse(BaseModel):
    """The model usage of one scan, for the usage panel in the console. Never part of the report or the PDF. `source`: measured (per-call records),
    stage_records (a scan made before per-call tracking: the usage its stage records kept) or none."""
    scan_id: str
    url: str
    domain: str
    scan_status: str
    state: str
    current_stage: str | None = None
    created_at: datetime | None = None
    completed_at: datetime | None = None
    pages_scanned: int | None = None
    source: str
    analysis_run: bool = True
    totals: UsageTotals
    cost: UsageCost
    stages: list[UsageStage]
    pages: UsagePages
    notes: list[str]
    calls: list[UsageCall] = []
    errors: list[UsageError] = []
    results: UsageResults = UsageResults()


class UsageHistoryItem(BaseModel):
    scan_id: str
    url: str
    domain: str
    status: str
    created_at: datetime | None = None
    completed_at: datetime | None = None
    pages_scanned: int | None = None
    calls: int
    failed_calls: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cost: float | None = None
    cost_complete: bool
    currency: str
    source: str
    analysis_run: bool = True
    errors: list[UsageError] = []
    results: UsageResults = UsageResults()
    reused_analysis: bool = False


class ScanListItem(BaseModel):
    id: uuid.UUID
    url: str
    status: str
    created_at: datetime | None = None
    completed_at: datetime | None = None


@router.get("", response_model=list[ScanListItem])
async def list_scans(
    limit: int = 20,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The caller's organisation's recent scans, newest first, so the console can reopen one after a reload."""
    rows = await scan_repository.list_recent_scans(db, uuid.UUID(user.org_id), max(1, min(limit, 50)))
    return [ScanListItem(id=s.id, url=s.url, status=s.status, created_at=s.created_at, completed_at=s.completed_at) for s in rows]


@router.get("/usage/history", response_model=list[UsageHistoryItem])
async def get_usage_history(
    limit: int = 30,
    offset: int = 0,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The model usage of the organisation's scans, newest first, for the usage panel's history; `offset` pages back through all of them. A scan made before
    usage was recorded per call shows what its stage records kept, marked `stage_records`; a scan with neither shows source `none`. Read-only: it never
    regenerates anything."""
    scans = await scan_repository.list_recent_scans(db, uuid.UUID(user.org_id), max(1, min(limit, 100)), max(0, offset))
    return await usage_service.load_history(db, scans)


@router.get("/{scan_id}/usage", response_model=UsageResponse)
async def get_scan_usage(
    scan_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The model usage of one scan so far: totals, by stage, by page where a call read exactly one page, and the cost, read from the per-call records as each
    response arrives (so it moves while the scan runs). The caller's organisation must own the scan."""
    scan = await scan_repository.get_scan(db, scan_id, uuid.UUID(user.org_id))
    if scan is None:
        raise NotFoundError(f"Scan {scan_id} not found")
    return await usage_service.load_scan_usage(db, scan)


@router.post("", response_model=ScanResponse, status_code=202)
async def create_scan(
    body: CreateScanRequest,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    scan = await scan_service.request_scan(
        db, org_id=uuid.UUID(user.org_id), user_id=uuid.UUID(user.user_id),
        url=body.url, authorized=body.authorized,
    )
    website = await scan_repository.get_website_by_id(db, scan.website_id)
    return ScanResponse(
        id=scan.id, url=scan.url, domain=website.domain if website else "", status=scan.status,
    )


@router.get("/{scan_id}", response_model=ScanStatusResponse)
async def get_scan(
    scan_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    scan = await scan_repository.get_scan(db, scan_id, uuid.UUID(user.org_id))
    if scan is None:
        raise NotFoundError(f"Scan {scan_id} not found")
    website = await scan_repository.get_website_by_id(db, scan.website_id)
    counts = await scan_repository.get_scan_counts(db, scan_id)
    run = await scan_repository.latest_agent_run(db, scan_id)
    derived = await consent_agent_api_service.describe_scan(db, scan)
    # `findings_complete` is true ONLY when analysis reached a state where the finding
    # list is the whole answer. `paused` counts: that is the human-review gate, and the
    # findings are all present and waiting on a decision. Everything else -- never run,
    # still running, failed -- means the list is not yet the answer.
    return ScanStatusResponse(
        id=scan.id, url=scan.url, domain=website.domain if website else "",
        status=scan.status, error=scan.error,
        evidence_counts=counts,
        analysis_status=run.status if run else None,
        findings_complete=bool(run and run.status in ("completed", "paused")),
        **derived,
    )


@router.post("/{scan_id}/analyze", response_model=AgentRunResponse, status_code=202)
async def analyze_scan(
    scan_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    agent_run = await analysis_service.trigger_analysis(
        db, scan_id=scan_id, org_id=uuid.UUID(user.org_id), user_id=uuid.UUID(user.user_id)
    )
    return AgentRunResponse(id=agent_run.id, status=agent_run.status)


@router.get("/{scan_id}/findings", response_model=list[FindingResponse])
async def list_findings(
    scan_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    scan = await scan_repository.get_scan(db, scan_id, uuid.UUID(user.org_id))
    if scan is None:
        raise NotFoundError(f"Scan {scan_id} not found")
    findings = await finding_repository.list_findings_for_scan(db, scan_id)
    recs_by_finding = await finding_repository.list_recommendations_for_findings(db, [f.id for f in findings])
    response = []
    for f in findings:
        recs = recs_by_finding[f.id]
        response.append(FindingResponse(
            id=f.id, category=f.category, risk_level=f.risk_level, priority=f.priority, finding_text=f.finding_text,
            evidence=f.evidence, dpdp_reference=f.dpdp_reference, requires_human_review=f.requires_human_review,
            status=f.status,
            recommendations=[
                RecommendationResponse(id=r.id, recommendation_text=r.recommendation_text, priority=r.priority)
                for r in recs
            ],
        ))
    return response


@router.get("/{scan_id}/evidence", response_model=ScanEvidenceResponse)
async def get_scan_evidence(
    scan_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Per-item evidence (real cookie/tracker/form/policy rows), for a frontend table
    view -- GET /{scan_id} above only ever returns aggregate counts. Org-scoped the
    same way every other scan-scoped read is; the underlying data comes from
    scan_repository.get_scan_evidence_summary_concurrent(), the same function the
    agent's own normalize node already uses -- nothing new is computed here."""
    scan = await scan_repository.get_scan(db, scan_id, uuid.UUID(user.org_id))
    if scan is None:
        raise NotFoundError(f"Scan {scan_id} not found")
    await db.rollback()   # give this request's connection back before the evidence is loaded on others (see scan_repository._evidence_slot)
    # With the cookie audit columns (expiry, flags, where/when seen) the agent's own
    # copy leaves out -- see scan_repository._cookie_detail.
    evidence = await scan_repository.get_scan_evidence_summary_concurrent(scan_id, include_cookie_detail=True)
    return ScanEvidenceResponse(**evidence)



@router.get("/{scan_id}/url-inventory", response_model=UrlInventoryResponse)
async def get_scan_url_inventory(
    scan_id: uuid.UUID,
    status: Literal["PENDING", "IN_PROGRESS", "COMPLETED", "FAILED", "PARTIALLY_SCANNED", "EXCLUDED"] | None = Query(default=None),
    http_status: int | None = Query(default=None, ge=100, le=599),
    source: str | None = Query(default=None, max_length=40, description="A discovery source: seed, link or sitemap."),
    depth: int | None = Query(default=None, ge=0, le=1000),
    q: str | None = Query(default=None, max_length=300, description="Part of the URL."),
    failure: str | None = Query(default=None, max_length=300, description="Part of the failure or exclusion reason."),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The URL inventory of a scan: counts by status, and a filtered, paged list. Org-scoped like every scan read (another organisation's scan is a 404)."""
    scan = await scan_repository.get_scan(db, scan_id, uuid.UUID(user.org_id))
    if scan is None:
        raise NotFoundError(f"Scan {scan_id} not found")
    found = await scan_repository.get_url_inventory(
        db, scan_id, status=status, http_status=http_status, source=source, depth=depth, q=q, failure=failure, limit=limit, offset=offset,
    )
    return UrlInventoryResponse(
        summary=UrlInventorySummary(**found["summary"]), urls=[UrlInventoryItem.model_validate(r, from_attributes=True) for r in found["rows"]],
        total=found["total"], limit=limit, offset=offset,
    )


@router.get("/{scan_id}/assessment", response_model=AssessmentResponse)
async def get_scan_assessment(
    scan_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The ten checks and the Privacy Readiness Score for one scan, worked out on request from its stored evidence and policy analysis (nothing new is stored). Same scan, same answer,
    for every caller: this is the one score. Org-scoped like every scan read."""
    scan = await scan_repository.get_scan(db, scan_id, uuid.UUID(user.org_id))
    if scan is None:
        raise NotFoundError(f"Scan {scan_id} not found")
    # Plain copies of what is needed, taken before the connection is given back (the rows expire when it is).
    stages = [
        SimpleNamespace(stage=st.stage, status=st.status, started_at=st.started_at, completed_at=st.completed_at, stage_metadata=dict(st.stage_metadata or {}))
        for st in await stage_repository.list_stages(db, scan_id, uuid.UUID(user.org_id))
    ]
    shot = await scan_repository.get_screenshot(db, scan_id)
    screenshot_url = shot.page_url if shot is not None else None
    scan_status = scan.status
    mode = assessment_service.scan_mode(scan.scan_options)
    scanned_at = (scan.completed_at or scan.started_at or scan.created_at).isoformat() if (scan.completed_at or scan.started_at or scan.created_at) else None
    await db.rollback()   # give this request's connection back before the evidence is loaded on others (see scan_repository._evidence_slot)
    evidence = ScanEvidenceResponse(**await scan_repository.get_scan_evidence_summary_concurrent(scan_id, include_cookie_detail=True)).model_dump()
    done = [st for st in stages if st.stage == "policy_analysis" and st.status == "completed" and (st.stage_metadata or {}).get("result")]
    policy = PolicyAnalysisResponse(**max(done, key=lambda st: st.started_at or st.completed_at).stage_metadata["result"]).model_dump() if done else None
    try:
        result = assessment_service.assess_parts(evidence, policy, assessment_service.website_scan_meta(stages), screenshot_url, mode=mode, scanned_at=scanned_at)
    except domain_score.ResultRefused:
        # EX-10: a result that would assert what the scan cannot back is never served in a weaker form; the failure is loud in the log and the caller gets a plain error with a reference.
        logging.getLogger(__name__).exception("assessment refused for scan %s", scan_id)
        raise ConsivaError(f"The assessment for this scan could not be produced. Please quote the scan reference {scan_id} to support.") from None
    state = assessment_service.policy_stage_state(stages)
    return AssessmentResponse(
        scan_id=scan_id, methodology_version=assessment_service.active_version(), scan_status=scan_status, policy_analysis=state,
        final=scan_status == "completed" and state in ("done", "failed"), **result,
    )


@router.get("/{scan_id}/requirements", response_model=DpdpRequirementsResponse)
async def get_scan_requirements(
    scan_id: uuid.UUID,
    as_of: date | None = None,
    processes_children_data: bool | None = None,
    significant_data_fiduciary: bool | None = None,
    consent_manager: bool | None = None,
    erasure_class: bool | None = None,
    business_sector: str | None = Query(None, pattern="^(" + "|".join(k for k in SECTORS if k != "general") + ")$"),
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The DPDP requirements reading of one scan (statuses, evidence, findings, remediation), computed on demand from its persisted evidence (nothing new is
    stored). It has no score: the one score is GET /assessment. `as_of` defaults to today (India) and decides which obligations are already in force. The four boolean
    parameters declare entity-specific applicability; left out, those requirements are excluded, never guessed.
    `business_sector` is the sector the organisation declares for itself (business context the scan cannot see); the scan's own reading
    is returned beside it."""
    scan = await scan_repository.get_scan(db, scan_id, uuid.UUID(user.org_id))
    if scan is None:
        raise NotFoundError(f"Scan {scan_id} not found")
    # Read what is needed from the scan, then give this request's connection back BEFORE the evidence is loaded on others. A request that kept its connection
    # while it opened seven more deadlocked the pool as soon as a handful ran together: every holder waited for connections the other holders were keeping.
    scan_url = scan.url
    when = scan.completed_at or scan.started_at or scan.created_at
    await db.rollback()
    evidence = await scan_repository.get_scan_evidence_for_score(scan_id)
    result = requirements_reading.assess(
        evidence, as_of,
        requirements_reading.Applicability(processes_children_data, significant_data_fiduciary, consent_manager, erasure_class),
        scan_url=scan_url,
        scanned_at=when.isoformat() if when else None,
        business_sector=business_sector,
    )
    return DpdpRequirementsResponse(**asdict(result))


class ScreenshotResponse(BaseModel):
    """The homepage preview of one scan, as the dashboard and every PDF show it. It carries the scan id and the domain it belongs to so a
    reader of the response can see it is the right site. `status` is ok, blocked, timeout, failed, or not_captured (a scan made before previews
    existed); `message` says in plain words why there is no picture, and never carries an internal error or a path. `data` is the JPEG as base64."""
    scan_id: uuid.UUID
    domain: str
    status: str
    available: bool
    message: str | None = None
    page_url: str | None = None
    mime: str | None = None
    width: int | None = None
    height: int | None = None
    captured_at: datetime | None = None
    data: str | None = None


_NO_PREVIEW = {
    "not_captured": "No homepage preview was taken for this scan: it was scanned before previews were added.",
    "scan_not_completed": "No homepage preview was taken because the scan did not complete (for example the site refused the scanner or could not be reached).",
    "blocked": "The site did not show its homepage to the scanner (it answered with an error or a security check), so no preview is shown.",
    "timeout": "The homepage preview could not be taken in time.",
    "failed": "A preview of the homepage could not be taken for this scan.",
    "blank": "The homepage was blank when the scanner looked, so no preview is shown.",
}


@router.get("/{scan_id}/screenshot", response_model=ScreenshotResponse)
async def get_scan_screenshot(
    scan_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    scan = await scan_repository.get_scan(db, scan_id, uuid.UUID(user.org_id))
    if scan is None:
        raise NotFoundError(f"Scan {scan_id} not found")
    website = await scan_repository.get_website_by_id(db, scan.website_id)
    domain = website.domain if website else ""
    row = await scan_repository.get_screenshot(db, scan_id)
    if row is None:
        # A scan that did not complete (the site refused the scanner, was unreachable, ...) took no preview because it never got that far; "scanned before previews were added" would be untrue.
        message = _NO_PREVIEW["scan_not_completed"] if scan.status in ("failed", "cancelled") else _NO_PREVIEW["not_captured"]
        return ScreenshotResponse(scan_id=scan_id, domain=domain, status="not_captured", available=False, message=message)
    if row.status == "ok" and row.image:
        return ScreenshotResponse(
            scan_id=scan_id, domain=domain, status="ok", available=True, page_url=row.page_url, mime=row.mime or "image/jpeg",
            width=row.width, height=row.height, captured_at=row.captured_at, data=base64.b64encode(row.image).decode("ascii"),
        )
    key = "blank" if row.reason == "blank" else row.status
    return ScreenshotResponse(
        scan_id=scan_id, domain=domain, status=row.status if row.status in ("blocked", "timeout", "failed") else "failed", available=False,
        message=_NO_PREVIEW.get(key, _NO_PREVIEW["failed"]), page_url=row.page_url, captured_at=row.captured_at,
    )


class PolicyRequirementModel(BaseModel):
    id: str
    title: str
    group: str
    basis: str
    provisions: list[str]
    status: str
    quote: str = ""
    source_url: str | None = None
    detail: str = ""
    fix: str = ""
    # For the contact and response-period points: what was found beside the quoted passage (never inferred), else None.
    contacts: dict[str, list[str]] | None = None
    response_days: float | None = None


class PolicySummaryModel(BaseModel):
    applicable: int
    verified: int
    partial: int
    not_found: int
    not_applicable: int
    quotes_rejected: int
    completeness_percent: float | None = None


class PolicyDocumentModel(BaseModel):
    url: str
    policy_type: str
    characters: int
    source: str
    truncated: bool = False


class PolicyAnalysisResponse(BaseModel):
    """What the scan's privacy and cookie policies say, with exact quotations and the DPDP provisions each point rests on
    (app/policy). `status` is analysed, unavailable (with a reason) or not_run. Informational: it never changes the score."""
    status: str
    reason: str | None = None
    sector: str | None = None
    documents: list[PolicyDocumentModel] = []
    notes: list[str] = []
    summary: PolicySummaryModel | None = None
    requirements: list[PolicyRequirementModel] = []
    limitations: list[str] = []
    model: dict | None = None


@router.get(
    "/{scan_id}/report.pdf",
    response_class=Response,
    responses={200: {"content": {"application/pdf": {}}, "description": "The PDF report for the scan."}},
)
async def get_scan_report_pdf(
    scan_id: uuid.UUID,
    request: Request,
    variant: Literal["free", "full", "long", "summary"] = Query(
        "summary", description="summary (default): the two-page report; long: the full evidence report, opening with the Free report's front page; free: the two-page Free Scan report; full: the eight-page Full Scan report with its evidence appendix.",
    ),
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The PDF report of one completed scan: the same report the web page /?report=<scan id> shows, printed by headless Chromium
    (services/report_pdf_service.py). The caller must belong to the organisation that owns the scan and its website; a scan of another organisation is
    "not found", exactly as everywhere else in this API. A scan that has not completed has no report (409). If the PDF cannot be made, the answer is
    503 with a plain message (the cause is in the server log), and the scan and its data are unaffected."""
    org_id = uuid.UUID(user.org_id)
    scan = await scan_repository.get_scan(db, scan_id, org_id)
    if scan is None:
        raise NotFoundError(f"Scan {scan_id} not found")
    website = await scan_repository.get_website_by_id(db, scan.website_id)
    if website is None or website.org_id != scan.org_id:
        raise NotFoundError(f"Scan {scan_id} not found")
    if scan.status != "completed":
        raise ScanNotReadyError(f"Scan {scan_id} is not completed yet (status={scan.status}), so it has no report")
    # Release gate: with REPORT_REQUIRE_APPROVAL on, only an approved report is handed to a customer; an administrator may always download it to review it.
    release = await report_approval_service.require_release(db, scan_id, user.role, get_settings())
    auth = request.headers.get("authorization", "")
    token = auth.split(" ", 1)[1].strip() if auth.lower().startswith("bearer ") else ""
    # The stored copy when it is still exactly what would be printed now; otherwise printed once (however many ask together) and stored.
    rendered, source = await report_cache_service.get_or_render(
        db, scan, variant, token=token, user_id=user.user_id, org_id=user.org_id, role=user.role, settings=get_settings(),
    )
    return Response(
        content=rendered.pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{rendered.filename}"',
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "X-Report-Source": source,
            "X-Report-Release": release,
        },
    )


class ReportApprovalRequest(BaseModel):
    note: str | None = None


class ReportApprovalResponse(BaseModel):
    state: str
    approved_by_user_id: str | None = None
    approved_at: str | None = None
    report_version: str | None = None
    note: str | None = None
    current_version: str
    required: bool
    quality_owner: str = ""


async def _approval_scan(db: AsyncSession, scan_id: uuid.UUID, user: CurrentUser):
    scan = await scan_repository.get_scan(db, scan_id, uuid.UUID(user.org_id))
    if scan is None:
        raise NotFoundError(f"Scan {scan_id} not found")
    return scan


def _approval_body(state) -> ReportApprovalResponse:
    return ReportApprovalResponse(**{k: getattr(state, k) for k in ("state", "approved_by_user_id", "approved_at", "report_version", "note", "current_version")}, required=get_settings().report_require_approval, quality_owner=get_settings().report_quality_owner)


@router.get("/{scan_id}/report-approval", response_model=ReportApprovalResponse)
async def get_report_approval(scan_id: uuid.UUID, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Whether this scan's report has been approved for release, by whom and when."""
    await _approval_scan(db, scan_id, user)
    return _approval_body(await report_approval_service.get_state(db, scan_id))


@router.post("/{scan_id}/report-approval", response_model=ReportApprovalResponse)
async def approve_report(
    scan_id: uuid.UUID, request: Request, body: ReportApprovalRequest | None = None,
    user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db),
):
    """Approve the report for release. Administrators only. The report is printed first and must pass the quality checks (page count, no internal codes, one score, correct links and metadata)
    before the approval is recorded, so a report that fails them cannot be approved."""
    if not report_approval_service.is_reviewer(user.role):
        raise ReviewerRequiredError("Only an administrator can approve a report for release.")
    scan = await _approval_scan(db, scan_id, user)
    if scan.status != "completed":
        raise ScanNotReadyError(f"Scan {scan_id} is not completed yet (status={scan.status}), so it has no report")
    auth = request.headers.get("authorization", "")
    token = auth.split(" ", 1)[1].strip() if auth.lower().startswith("bearer ") else ""
    await report_cache_service.get_or_render(db, scan, "summary", token=token, user_id=user.user_id, org_id=user.org_id, role=user.role, settings=get_settings())
    return _approval_body(await report_approval_service.approve(db, scan_id, user.user_id, (body.note if body else None)))


@router.delete("/{scan_id}/report-approval", response_model=ReportApprovalResponse)
async def revoke_report_approval(scan_id: uuid.UUID, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Withdraw an approval: the report is no longer released to customers. Administrators only."""
    if not report_approval_service.is_reviewer(user.role):
        raise ReviewerRequiredError("Only an administrator can withdraw a report's approval.")
    await _approval_scan(db, scan_id, user)
    return _approval_body(await report_approval_service.revoke(db, scan_id, user.user_id))


@router.get("/{scan_id}/policy-analysis", response_model=PolicyAnalysisResponse)
async def get_scan_policy_analysis(
    scan_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    scan = await scan_repository.get_scan(db, scan_id, uuid.UUID(user.org_id))
    if scan is None:
        raise NotFoundError(f"Scan {scan_id} not found")
    stages = await stage_repository.list_stages(db, scan_id, uuid.UUID(user.org_id))
    done = [s for s in stages if s.stage == "policy_analysis" and s.status == "completed" and (s.stage_metadata or {}).get("result")]
    if not done:
        return PolicyAnalysisResponse(status="not_run", reason="The policy analysis has not run for this scan.")
    latest = max(done, key=lambda s: s.started_at or s.completed_at)
    return PolicyAnalysisResponse(**latest.stage_metadata["result"])


@router.get("/{scan_id}/stages", response_model=list[StageResponse])
async def get_scan_stages(
    scan_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Live pipeline view for the demo UI — one row per stage per attempt (a retried
    stage, e.g. llm_analysis after a validation failure, produces more than one row)."""
    scan = await scan_repository.get_scan(db, scan_id, uuid.UUID(user.org_id))
    if scan is None:
        raise NotFoundError(f"Scan {scan_id} not found")
    stages = await stage_repository.list_stages(db, scan_id, uuid.UUID(user.org_id))
    return [
        StageResponse(
            stage=s.stage, status=s.status, duration_ms=s.duration_ms, error=s.error, metadata=s.stage_metadata,
            started_at=s.started_at, completed_at=s.completed_at,
        )
        for s in stages
    ]


@router.get("/{scan_id}/audit", response_model=list[AuditLogResponse])
async def get_scan_audit(
    scan_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    scan = await scan_repository.get_scan(db, scan_id, uuid.UUID(user.org_id))
    if scan is None:
        raise NotFoundError(f"Scan {scan_id} not found")
    logs = await audit_repository.list_for_scan(db, scan_id, uuid.UUID(user.org_id))
    return [
        AuditLogResponse(
            id=log.id, action=log.action, entity_type=log.entity_type, entity_id=log.entity_id,
            actor_user_id=log.actor_user_id, agent_run_id=log.agent_run_id, model_name=log.model_name,
            provider=(log.after or {}).get("provider"),
        )
        for log in logs
    ]
