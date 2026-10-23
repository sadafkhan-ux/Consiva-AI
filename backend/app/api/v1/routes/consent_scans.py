import base64
import uuid
from dataclasses import asdict
from datetime import date, datetime
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.exceptions import NotFoundError
from app.core.security import CurrentUser, get_current_user
from app.db.repositories import (
    audit_repository,
    finding_repository,
    scan_repository,
    stage_repository,
)
from app.db.session import get_db
from app.llm.schemas import DpdpReference
from app.scoring import dpdp_score
from app.scoring.sector import SECTORS
from app.services import analysis_service, scan_service

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


class EvidenceConsentSignalItem(BaseModel):
    mechanism_type: str
    cmp_vendor: str | None
    has_reject_all: bool | None
    has_granular_choices: bool | None
    evidence: dict


class ScanEvidenceResponse(BaseModel):
    pages: list[EvidencePageItem]
    forms: list[EvidenceFormItem]
    cookies: list[EvidenceCookieItem]
    trackers: list[EvidenceTrackerItem]
    third_party_services: list[EvidenceThirdPartyItem]
    policies: list[EvidencePolicyItem]
    consent_signals: list[EvidenceConsentSignalItem]


# --- DPDP Compliance Score (proposed methodology; app/scoring/dpdp_score.py, docs/DPDP_COMPLIANCE_SCORE.md) -------------
class DpdpRequirementResult(BaseModel):
    id: str
    category: str
    title: str
    provisions: list[str]
    weight: int
    effective: date
    in_force: bool
    status: str
    points_earned: float
    counts_in_score: bool
    observations: list[str]
    evidence_ids: list[str]
    rule_ids: list[str]
    exclusion_reason: str | None = None
    default_weight: int
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
    scoring_rationale: str
    legal_note: str
    penalty_points: float | None = None
    score_contribution_percent: float | None = None
    coverage_share_percent: float | None = None


class DpdpCategoryScore(BaseModel):
    category: str
    applicable_weight: int
    assessed_weight: int
    points_earned: float
    score_percent: float | None
    coverage_percent: float | None
    status: str


class DpdpScoreBlock(BaseModel):
    basis: str
    status: str
    score_percent: float | None
    points_earned: float
    points_assessed: int
    applicable_weight: int
    assessed_weight: int
    unable_to_verify_weight: int
    coverage_percent: float | None
    insufficient_reason: str | None
    caution: str | None = None
    categories: list[DpdpCategoryScore]


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
    weight: int
    points: float
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
    weight: int
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


class DpdpMethodWeight(BaseModel):
    id: str
    title: str
    category: str
    check_type: str
    weight: int
    default_weight: int
    rationale: str
    applicability: str
    effective: date
    applies_now: bool


class DpdpMethodCategory(BaseModel):
    name: str
    rationale: str
    weight: int
    share_percent: float | None
    requirement_ids: list[str]


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
    # What fixing this one gap would do to the readiness score, from the same weights the score uses (None when the score is withheld).
    current_percent: float | None = None
    percent_if_fixed: float | None = None
    gain_percent: float | None = None
    # What the gap means for the organisation, in plain words ("" when there is no stated impact for this check).
    impact: str = ""


class DpdpMethodology(BaseModel):
    version: str
    weights_source: str
    weights_note: str
    score_formula: str
    coverage_formula: str
    points_rules: list[str]
    reliability_rule: str
    duplicate_rule: str
    timing_rule: str
    scope_rule: str
    legal_mapping_version: str
    legal_sources: list[DpdpLegalSource]
    weights: list[DpdpMethodWeight]
    categories: list[DpdpMethodCategory]
    dedup: list[DpdpMethodDedup]


class DpdpLedgerMetric(BaseModel):
    key: str
    name: str
    numerator: float
    denominator: float
    percent: float | None
    formula: str
    meaning: str


class DpdpStatusRule(BaseModel):
    status: str
    label: str
    rule: str
    in_numerator: str
    in_denominator: str
    on_coverage: str
    count: int
    weight: int
    check_ids: list[str]


class DpdpLedger(BaseModel):
    total_checks: int
    applicable_checks: int
    assessed_checks: int
    passed_checks: int
    partial_checks: int
    failed_checks: int
    unable_checks: int
    not_applicable_checks: int
    total_weight: int
    applicable_weight: int
    assessed_weight: int
    earned_weight: float
    penalty_weight: float
    unable_weight: int
    not_applicable_weight: int
    metrics: list[DpdpLedgerMetric]
    category_metrics: list[DpdpLedgerMetric]
    status_rules: list[DpdpStatusRule]
    penalty_note: str
    security_score_note: str
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


class DpdpScoreResponse(BaseModel):
    methodology_version: str
    as_of: date
    requirements: list[DpdpRequirementResult]
    readiness: DpdpScoreBlock
    in_force: DpdpScoreBlock
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
    thresholds: dict
    out_of_scope: list[dict]


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
    # With the cookie audit columns (expiry, flags, where/when seen) the agent's own
    # copy leaves out -- see scan_repository._cookie_detail.
    evidence = await scan_repository.get_scan_evidence_summary_concurrent(scan_id, include_cookie_detail=True)
    return ScanEvidenceResponse(**evidence)



@router.get("/{scan_id}/dpdp-score", response_model=DpdpScoreResponse)
async def get_scan_dpdp_score(
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
    """The proposed DPDP Compliance Score for one scan, computed on demand from its persisted evidence (nothing new is
    stored). `as_of` defaults to today (UTC) and decides which obligations are already in force. The four boolean
    parameters declare entity-specific applicability; left out, those requirements are excluded, never guessed.
    `business_sector` is the sector the organisation declares for itself (business context the scan cannot see); the scan's own reading
    is returned beside it. Neither changes the score."""
    scan = await scan_repository.get_scan(db, scan_id, uuid.UUID(user.org_id))
    if scan is None:
        raise NotFoundError(f"Scan {scan_id} not found")
    evidence = await scan_repository.get_scan_evidence_summary_concurrent(scan_id, include_cookie_detail=True)
    result = dpdp_score.assess(
        evidence, as_of,
        dpdp_score.Applicability(processes_children_data, significant_data_fiduciary, consent_manager, erasure_class),
        get_settings().dpdp_score_weights or None,
        scan_url=scan.url,
        scanned_at=(scan.completed_at or scan.started_at or scan.created_at).isoformat()
        if (scan.completed_at or scan.started_at or scan.created_at) else None,
        business_sector=business_sector,
    )
    return DpdpScoreResponse(**asdict(result))


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
        return ScreenshotResponse(scan_id=scan_id, domain=domain, status="not_captured", available=False, message=_NO_PREVIEW["not_captured"])
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
