import uuid
from urllib.parse import urlparse

from fastapi import APIRouter, Depends
from pydantic import BaseModel, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

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
from app.services import analysis_service, scan_service

router = APIRouter(prefix="/api/v1/consent/scans", tags=["consent-scans"])


class CreateScanRequest(BaseModel):
    url: str
    authorized: bool = False

    @field_validator("url")
    @classmethod
    def _url_must_be_http_with_host(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("url must be an absolute http(s) URL, e.g. https://example.com")
        return value


class ScanResponse(BaseModel):
    id: uuid.UUID
    url: str
    status: str


class ScanStatusResponse(ScanResponse):
    error: str | None
    evidence_counts: dict


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


class EvidenceFormItem(BaseModel):
    id: str
    selector: str | None
    fields: list
    purpose_guess: str | None


class EvidenceCookieItem(BaseModel):
    id: str
    name: str
    domain: str | None
    category: str | None
    vendor: str | None
    is_first_party: bool | None
    source: str | None
    consent_states: list[str]


class EvidenceTrackerItem(BaseModel):
    id: str
    script_src: str
    vendor: str | None
    category: str | None
    source: str | None
    consent_states: list[str]


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
    return ScanResponse(id=scan.id, url=scan.url, status=scan.status)


@router.get("/{scan_id}", response_model=ScanStatusResponse)
async def get_scan(
    scan_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    scan = await scan_repository.get_scan(db, scan_id, uuid.UUID(user.org_id))
    if scan is None:
        raise NotFoundError(f"Scan {scan_id} not found")
    counts = await scan_repository.get_scan_counts(db, scan_id)
    return ScanStatusResponse(id=scan.id, url=scan.url, status=scan.status, error=scan.error, evidence_counts=counts)


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
    evidence = await scan_repository.get_scan_evidence_summary_concurrent(scan_id)
    return ScanEvidenceResponse(**evidence)


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
            stage=s.stage, status=s.status, duration_ms=s.duration_ms, error=s.error, metadata=s.stage_metadata
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
