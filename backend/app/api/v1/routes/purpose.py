"""Purpose Classifier API.

Answers a narrower question than the record of processing does: for data that already
exists, is the purpose it is being used for still the purpose it was collected for, and
should it still be kept?

Everything here is scoped to the caller's organisation by an explicit `org_id` filter,
matching how every other route on this branch enforces tenancy.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError
from app.core.security import CurrentUser, get_current_user
from app.db.models import PurposeAssessment, PurposeFinding, PurposeRun
from app.db.session import get_db
from app.services import audit_service, purpose_run_service

# No org-scope dependency here: app/api/v1/router.py binds it once for every v1
# route (bind_request_scope), so an endpoint added below cannot forget it and a
# second binding on this router would only be a second way to get it wrong.
router = APIRouter(prefix="/api/v1/purpose", tags=["purpose-classifier"])


class StartAssessmentRequest(BaseModel):
    scan_id: uuid.UUID = Field(
        ...,
        description="A completed consent scan. Its evidence is the observed-purpose "
                    "input for this assessment.",
    )


class AssessSourceRequest(BaseModel):
    """Assess a structured data source rather than a consent scan.

    The connection string is used once to read the schema and is never stored on the
    run record -- a DSN persisted on a row is a credential in a table many people read.
    """

    source_name: str = Field(..., min_length=1, description="A name you will recognise later.")
    connector: str = Field(..., description="postgres | csv | rest")
    dsn: str | None = Field(
        default=None,
        description="PostgreSQL connection string. Required for the postgres connector. "
                    "Only table and column NAMES are read -- never row data.",
    )
    csv_content: str | None = Field(
        default=None,
        description="CSV text for the csv connector. Only the header row is parsed; "
                    "the body is never read.",
    )
    table_name: str | None = Field(
        default=None, description="What the CSV represents. Defaults to the source name."
    )
    spec_url: str | None = Field(
        default=None,
        description="OpenAPI/Swagger specification URL. Required for the rest "
                    "connector. The SPECIFICATION is read, never the API's records -- "
                    "a spec is pure shape and contains no personal data, while a "
                    "response body is exactly the data this agent exists to ask "
                    "questions about.",
    )
    auth_header: str | None = Field(
        default=None,
        description="Optional Authorization header value for fetching the "
                    "specification, e.g. 'Bearer ...'. Sent once and never stored on "
                    "the run record.",
    )


class RunResponse(BaseModel):
    id: uuid.UUID
    status: str
    scan_id: uuid.UUID | None
    assessments_count: int
    findings_count: int
    note: str | None = Field(
        default=None,
        description="Set when something limited the run -- most often that no declared "
                    "purposes were available, so comparisons are undetermined rather "
                    "than aligned.",
    )
    started_at: datetime | None
    completed_at: datetime | None


class AssessmentResponse(BaseModel):
    id: uuid.UUID
    subject_type: str
    subject_ref: str
    subject_label: str | None
    declared_purpose: str | None
    declared_source: str | None
    observed_purpose: str | None
    observed_source: str | None
    alignment: str
    confidence: float
    retention_status: str
    retention_note: str | None
    evidence_refs: list
    created_at: datetime


class FindingResponse(BaseModel):
    id: uuid.UUID
    assessment_id: uuid.UUID
    finding_type: str
    severity: str
    title: str
    description: str
    review_required: bool
    status: str
    created_at: datetime


class DecisionRequest(BaseModel):
    decision: str = Field(..., description="approved | rejected | dismissed")
    reason: str | None = Field(
        default=None,
        description="Required when rejecting or dismissing -- a finding set aside "
                    "without a recorded reason is indistinguishable from one nobody read.",
    )


def _run_out(run: PurposeRun) -> RunResponse:
    return RunResponse(
        id=run.id, status=run.status, scan_id=run.scan_id,
        assessments_count=run.assessments_count, findings_count=run.findings_count,
        note=run.error, started_at=run.started_at, completed_at=run.completed_at,
    )


@router.post("/assessments", response_model=RunResponse, status_code=202)
async def start_assessment(
    body: StartAssessmentRequest,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RunResponse:
    """Queue a purpose assessment over a completed consent scan.

    Returns immediately; the work runs on the existing background worker. Poll the run
    by id.
    """
    run = await purpose_run_service.request_assessment(
        db, org_id=uuid.UUID(user.org_id), user_id=uuid.UUID(user.user_id),
        scan_id=body.scan_id,
    )
    return _run_out(run)


@router.post("/sources/assess", response_model=RunResponse, status_code=202)
async def assess_source(
    body: AssessSourceRequest,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RunResponse:
    """Queue a purpose assessment over a database, an uploaded file, or a REST API.

    Reads the SHAPE only -- table and column names, or schema and property names. No
    row data, no samples, no API responses. These are the input modes in which
    declared-vs-observed comparison can actually resolve: declarations describe tables
    and fields, and so do these observations.

    A credential supplied here is used once by the worker and is never written to the
    run record.
    """
    run = await purpose_run_service.request_source_assessment(
        db, org_id=uuid.UUID(user.org_id), user_id=uuid.UUID(user.user_id),
        source_name=body.source_name, connector=body.connector,
        dsn=body.dsn, csv_content=body.csv_content, table_name=body.table_name,
        spec_url=body.spec_url, auth_header=body.auth_header,
    )
    return _run_out(run)


@router.get("/assessments", response_model=list[RunResponse])
async def list_runs(
    limit: int = Query(default=25, ge=1, le=100),
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[RunResponse]:
    rows = (await db.execute(
        select(PurposeRun)
        .where(PurposeRun.org_id == uuid.UUID(user.org_id))
        .order_by(PurposeRun.created_at.desc())
        .limit(limit)
    )).scalars().all()
    return [_run_out(r) for r in rows]


@router.get("/assessments/{run_id}", response_model=RunResponse)
async def get_run(
    run_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RunResponse:
    run = (await db.execute(
        select(PurposeRun).where(
            PurposeRun.id == run_id, PurposeRun.org_id == uuid.UUID(user.org_id)
        )
    )).scalars().first()
    if run is None:
        raise NotFoundError(f"Purpose run {run_id} not found")
    return _run_out(run)


@router.get("/assessments/{run_id}/results", response_model=list[AssessmentResponse])
async def get_results(
    run_id: uuid.UUID,
    alignment: str | None = Query(
        default=None, description="Filter: aligned | mismatch | undetermined"
    ),
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[AssessmentResponse]:
    """Every comparison this run made, including the unremarkable ones.

    `undetermined` rows are returned rather than hidden: a run that could not compare
    most of its items is a materially different result from one where everything
    agreed, and filtering them out of the default view would make those look identical.
    """
    stmt = select(PurposeAssessment).where(
        PurposeAssessment.run_id == run_id,
        PurposeAssessment.org_id == uuid.UUID(user.org_id),
    )
    if alignment:
        stmt = stmt.where(PurposeAssessment.alignment == alignment)
    rows = (await db.execute(stmt.order_by(PurposeAssessment.created_at))).scalars().all()
    return [AssessmentResponse.model_validate(r, from_attributes=True) for r in rows]


@router.get("/findings", response_model=list[FindingResponse])
async def list_findings(
    status: str | None = Query(default=None, description="pending | approved | rejected | dismissed"),
    severity: str | None = Query(default=None, description="high | medium | low"),
    limit: int = Query(default=50, ge=1, le=200),
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[FindingResponse]:
    stmt = select(PurposeFinding).where(PurposeFinding.org_id == uuid.UUID(user.org_id))
    if status:
        stmt = stmt.where(PurposeFinding.status == status)
    if severity:
        stmt = stmt.where(PurposeFinding.severity == severity)
    rows = (await db.execute(
        stmt.order_by(PurposeFinding.created_at.desc()).limit(limit)
    )).scalars().all()
    return [FindingResponse.model_validate(r, from_attributes=True) for r in rows]


@router.post("/findings/{finding_id}/decision", response_model=FindingResponse)
async def decide_finding(
    finding_id: uuid.UUID,
    body: DecisionRequest,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FindingResponse:
    """Record a human decision on a finding.

    A reason is required for `rejected` and `dismissed`. Setting a compliance finding
    aside is itself a compliance decision, and one with no recorded reasoning cannot be
    told apart later from one nobody looked at.
    """
    if body.decision not in ("approved", "rejected", "dismissed"):
        raise NotFoundError(f"Unknown decision {body.decision!r}")
    if body.decision in ("rejected", "dismissed") and not (body.reason or "").strip():
        from app.core.exceptions import ConsivaError

        raise ConsivaError(
            f"A reason is required to {body.decision.rstrip('ed')} a finding."
        )

    finding = (await db.execute(
        select(PurposeFinding).where(
            PurposeFinding.id == finding_id,
            PurposeFinding.org_id == uuid.UUID(user.org_id),
        )
    )).scalars().first()
    if finding is None:
        raise NotFoundError(f"Purpose finding {finding_id} not found")

    before = {"status": finding.status}
    finding.status = body.decision
    finding.decided_by_user_id = uuid.UUID(user.user_id)
    finding.decided_at = func.now()
    finding.decision_reason = body.reason

    await audit_service.record(
        db, org_id=uuid.UUID(user.org_id), actor_user_id=uuid.UUID(user.user_id),
        action=f"purpose.finding.{body.decision}", entity_type="purpose_finding",
        entity_id=finding.id, before=before,
        after={"status": body.decision, "reason": body.reason},
    )
    await db.commit()
    await db.refresh(finding)
    return FindingResponse.model_validate(finding, from_attributes=True)
