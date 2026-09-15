"""Agent 4 (Breach Response) API.

Every route authenticated and org-scoped through the SAME dependency Agents 1, 2 and 3
use, so tenancy behaves identically across all four. Nothing accepts an org_id from
the caller -- it comes from the verified token and nowhere else.

Incident evidence is the most sensitive data in the platform: account names, attack
paths, occasionally a credential somebody pasted into a ticket. Evidence detail is
therefore withheld from list responses and from any row flagged as having carried
secrets, and the flag is surfaced so a reviewer knows detail exists rather than
assuming the row is empty.
"""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.breach.errors import IncidentNotFoundError, IncidentNotReadyError
from app.agents.breach.schemas import incident as vocab
from app.agents.breach.services import (
    communication_service,
    incident_service,
    investigation_service,
    lifecycle,
    response_service,
    sla_service,
)
from app.core.security import CurrentUser, get_current_user
from app.db.repositories import incident_repository
from app.db.session import get_db
from app.jobs import queue
from app.services import audit_service

router = APIRouter(prefix="/api/v1/incidents", tags=["incidents"])


# ── Request models ───────────────────────────────────────────────────────────────

class IncidentCreate(BaseModel):
    title: str = Field(min_length=3, max_length=300)
    description: str = Field(min_length=10, max_length=20_000)
    source: str
    detected_at: datetime
    reported_by: str | None = Field(default=None, max_length=200)
    occurred_at: datetime | None = None
    initial_severity: str | None = None
    idempotency_key: str | None = Field(default=None, max_length=200)

    @field_validator("source")
    @classmethod
    def _known_source(cls, value: str) -> str:
        if value not in vocab.INCIDENT_SOURCES:
            raise ValueError(f"source must be one of {sorted(vocab.INCIDENT_SOURCES)}")
        return value

    @field_validator("initial_severity")
    @classmethod
    def _known_severity(cls, value: str | None) -> str | None:
        if value and value not in vocab.SEVERITIES:
            raise ValueError(f"initial_severity must be one of {list(vocab.SEVERITIES)}")
        return value


class EvidenceIn(BaseModel):
    kind: str
    source_system: str = Field(min_length=1, max_length=200)
    summary: str = Field(min_length=1, max_length=2_000)
    detail: dict | None = None
    observed_at: datetime | None = None

    @field_validator("kind")
    @classmethod
    def _known_kind(cls, value: str) -> str:
        if value not in vocab.EVIDENCE_KINDS:
            raise ValueError(f"kind must be one of {sorted(vocab.EVIDENCE_KINDS)}")
        return value


class TimelineIn(BaseModel):
    occurred_at: datetime
    event: str = Field(min_length=1, max_length=1_000)
    confidence: str = vocab.POSSIBLE
    actor: str | None = Field(default=None, max_length=200)
    source_system: str | None = Field(default=None, max_length=200)
    evidence_id: uuid.UUID | None = None

    @field_validator("confidence")
    @classmethod
    def _known_confidence(cls, value: str) -> str:
        if value not in vocab.CONFIDENCE_LEVELS:
            raise ValueError(f"confidence must be one of {list(vocab.CONFIDENCE_LEVELS)}")
        return value


class AffectedSystemIn(BaseModel):
    system_name: str = Field(min_length=1, max_length=200)
    system_kind: str
    component: str | None = Field(default=None, max_length=200)
    confidence: str = vocab.POSSIBLE
    evidence_id: uuid.UUID | None = None
    notes: str | None = Field(default=None, max_length=2_000)

    @field_validator("system_kind")
    @classmethod
    def _known_kind(cls, value: str) -> str:
        if value not in vocab.SYSTEM_KINDS:
            raise ValueError(f"system_kind must be one of {sorted(vocab.SYSTEM_KINDS)}")
        return value


class SubjectsIn(BaseModel):
    subject_group: str = Field(min_length=1, max_length=120)
    record_count: int | None = Field(default=None, ge=0)
    count_basis: str
    basis_note: str | None = Field(default=None, max_length=1_000)
    confidence: str = vocab.POSSIBLE

    @field_validator("count_basis")
    @classmethod
    def _known_basis(cls, value: str) -> str:
        if value not in ("counted", "estimated", "unknown"):
            raise ValueError("count_basis must be counted, estimated or unknown")
        return value


class FindingIn(BaseModel):
    """Setting one of the two substantive findings. `reason` is required for
    `confirmed` at the service layer, which is where the rule belongs."""

    field: str
    confidence: str
    reason: str = Field(default="", max_length=2_000)

    @field_validator("field")
    @classmethod
    def _known_field(cls, value: str) -> str:
        if value not in ("personal_data_involved", "breach_confirmed"):
            raise ValueError("field must be personal_data_involved or breach_confirmed")
        return value

    @field_validator("confidence")
    @classmethod
    def _known_confidence(cls, value: str) -> str:
        if value not in vocab.CONFIDENCE_LEVELS:
            raise ValueError(f"confidence must be one of {list(vocab.CONFIDENCE_LEVELS)}")
        return value


class ActionIn(BaseModel):
    action_kind: str
    title: str = Field(min_length=3, max_length=300)
    rationale: str = Field(min_length=3, max_length=2_000)
    expected_result: str = Field(min_length=3, max_length=2_000)
    target: str | None = Field(default=None, max_length=300)
    assignee_label: str | None = Field(default=None, max_length=200)

    @field_validator("action_kind")
    @classmethod
    def _known_kind(cls, value: str) -> str:
        if value not in vocab.ACTION_KINDS:
            raise ValueError(f"action_kind must be one of {sorted(vocab.ACTION_KINDS)}")
        return value


class DecisionIn(BaseModel):
    decision: str
    reason: str | None = Field(default=None, max_length=2_000)

    @field_validator("decision")
    @classmethod
    def _known_decision(cls, value: str) -> str:
        if value not in vocab.DECISIONS:
            raise ValueError(f"decision must be one of {sorted(vocab.DECISIONS)}")
        return value


class AttestationIn(BaseModel):
    """What a person did outside Consiva. Both fields mandatory: "somebody did it"
    with no name and no description records nothing."""

    performed_by: str = Field(min_length=1, max_length=200)
    attestation: str = Field(min_length=5, max_length=2_000)


class CommunicationIn(BaseModel):
    audience: str
    subject: str = Field(min_length=3, max_length=300)
    body: str | None = Field(default=None, max_length=50_000)

    @field_validator("audience")
    @classmethod
    def _known_audience(cls, value: str) -> str:
        if value not in vocab.COMMUNICATION_AUDIENCES:
            raise ValueError(f"audience must be one of {sorted(vocab.COMMUNICATION_AUDIENCES)}")
        return value


class TransitionIn(BaseModel):
    to_status: str
    reason: str | None = Field(default=None, max_length=2_000)
    error_code: str | None = None


# ── Response shaping ─────────────────────────────────────────────────────────────

def _incident_response(case) -> dict:
    """`allowed_transitions` comes from the same state machine the backend enforces,
    so the UI greys out what is genuinely impossible rather than keeping its own copy
    of the rules."""
    return {
        "id": str(case.id),
        "reference": case.reference,
        "title": case.title,
        "description": case.description,
        "source": case.source,
        "reported_by": case.reported_by,
        "incident_type": case.incident_type,
        "classification_method": case.classification_method,
        "classification_confidence": case.classification_confidence,
        "initial_severity": case.initial_severity,
        "severity": case.severity,
        "severity_score": case.severity_score,
        "severity_confidence": case.severity_confidence,
        # The two questions the agent exists to answer carefully.
        "personal_data_involved": case.personal_data_involved,
        "breach_confirmed": case.breach_confirmed,
        "status": case.status,
        "error_code": case.error_code,
        "error_detail": case.error_detail,
        "occurred_at": case.occurred_at.isoformat() if case.occurred_at else None,
        "detected_at": case.detected_at.isoformat() if case.detected_at else None,
        "closure_summary": case.closure_summary,
        "allowed_transitions": sorted(lifecycle.allowed_transitions(case.status)),
        "is_terminal": lifecycle.is_terminal(case.status),
        "sla": sla_service.sla_view(case),
        "created_at": case.created_at.isoformat() if case.created_at else None,
        "closed_at": case.closed_at.isoformat() if case.closed_at else None,
    }


# ── Incidents ────────────────────────────────────────────────────────────────────

@router.post("", status_code=status.HTTP_201_CREATED)
async def create_incident(
    payload: IncidentCreate,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    case, is_new = await incident_service.create_incident(
        db,
        org_id=uuid.UUID(user.org_id),
        title=payload.title,
        description=payload.description,
        source=payload.source,
        detected_at=payload.detected_at,
        reported_by=payload.reported_by,
        occurred_at=payload.occurred_at,
        initial_severity=payload.initial_severity,
        idempotency_key=payload.idempotency_key,
        created_by_user_id=uuid.UUID(user.user_id),
    )
    if is_new:
        # Classification reads only what the reporter wrote, so it is not behind any
        # gate and a classified incident is one a responder can triage immediately.
        await incident_service.classify_incident(
            db, case, actor_user_id=uuid.UUID(user.user_id)
        )
        await incident_service.transition(
            db, case, vocab.VALIDATING, actor_user_id=uuid.UUID(user.user_id),
            audit_action=vocab.AUDIT_VALIDATED,
        )
    await db.commit()
    return _incident_response(case)


@router.get("")
async def list_incidents(
    status_filter: str | None = None,
    limit: int = 50,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    rows = await incident_repository.list_incidents(
        db, uuid.UUID(user.org_id), status=status_filter, limit=min(limit, 200)
    )
    return [_incident_response(r) for r in rows]


@router.get("/{incident_id}")
async def get_incident(
    incident_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    case = await incident_service.get_incident_or_raise(db, incident_id, uuid.UUID(user.org_id))
    return _incident_response(case)


@router.post("/{incident_id}/classify")
async def classify(
    incident_id: uuid.UUID,
    override_type: str | None = None,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    case = await incident_service.get_incident_or_raise(db, incident_id, uuid.UUID(user.org_id))
    await incident_service.classify_incident(
        db, case, actor_user_id=uuid.UUID(user.user_id), override_type=override_type
    )
    await db.commit()
    return _incident_response(case)


@router.post("/{incident_id}/finding")
async def set_finding(
    incident_id: uuid.UUID,
    payload: FindingIn,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Record whether personal data was involved, or whether this is a breach.

    `confirmed` requires a named person and a reason -- this is the endpoint where an
    organisation commits to a position, and the audit trail records who did.
    """
    case = await incident_service.get_incident_or_raise(db, incident_id, uuid.UUID(user.org_id))
    await incident_service.set_finding(
        db, case, field=payload.field, confidence=payload.confidence,
        actor_user_id=uuid.UUID(user.user_id), reason=payload.reason,
    )
    await db.commit()
    return _incident_response(case)


@router.post("/{incident_id}/transition")
async def transition(
    incident_id: uuid.UUID,
    payload: TransitionIn,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    case = await incident_service.get_incident_or_raise(db, incident_id, uuid.UUID(user.org_id))
    await incident_service.transition(
        db, case, payload.to_status, actor_user_id=uuid.UUID(user.user_id),
        error_code=payload.error_code, error_detail=payload.reason,
        detail={"reason": payload.reason} if payload.reason else None,
    )
    await db.commit()
    return _incident_response(case)


# ── Evidence and timeline ────────────────────────────────────────────────────────

@router.post("/{incident_id}/evidence", status_code=status.HTTP_201_CREATED)
async def add_evidence(
    incident_id: uuid.UUID,
    payload: EvidenceIn,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    case = await incident_service.get_incident_or_raise(db, incident_id, uuid.UUID(user.org_id))
    row = await investigation_service.add_evidence(
        db, case, kind=payload.kind, source_system=payload.source_system,
        summary=payload.summary, detail=payload.detail,
        observed_at=payload.observed_at, actor_user_id=uuid.UUID(user.user_id),
    )
    await db.commit()
    return {
        "id": str(row.id), "kind": row.kind, "summary": row.summary,
        "secrets_redacted": row.contains_secrets, "is_derived": row.is_derived,
    }


@router.get("/{incident_id}/evidence")
async def list_evidence(
    incident_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """Evidence detail is NOT returned in a listing.

    A list view is the one most likely to be left open on a shared screen, and the
    detail is the part that carries account names and attack paths. The summary and
    the fact that detail exists are enough to navigate; the detail itself needs a
    deliberate request.
    """
    org_id = uuid.UUID(user.org_id)
    case = await incident_service.get_incident_or_raise(db, incident_id, org_id)
    rows = await incident_repository.list_evidence(db, case.id, org_id)
    return [
        {
            "id": str(e.id), "kind": e.kind, "source_system": e.source_system,
            "summary": e.summary,
            "observed_at": e.observed_at.isoformat() if e.observed_at else None,
            "is_derived": e.is_derived,
            "contains_secrets": e.contains_secrets,
            "has_detail": bool(e.detail),
            "supersedes_id": str(e.supersedes_id) if e.supersedes_id else None,
        }
        for e in rows
    ]


@router.get("/{incident_id}/evidence/{evidence_id}")
async def get_evidence_detail(
    incident_id: uuid.UUID,
    evidence_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """One piece of evidence, with its detail. Every read is audited (§38).

    Detail was redacted of secret-shaped fields before it was ever stored, so what
    comes back here has already been through that filter -- but the access is still
    recorded, because who looked at incident evidence is itself something an
    investigation may need to answer.
    """
    org_id = uuid.UUID(user.org_id)
    case = await incident_service.get_incident_or_raise(db, incident_id, org_id)
    row = await incident_repository.get_evidence(db, evidence_id, org_id)
    if row is None or row.incident_id != case.id:
        raise IncidentNotFoundError(f"evidence {evidence_id} not found")

    await audit_service.record(
        db, org_id=org_id, actor_user_id=uuid.UUID(user.user_id),
        action="incident.evidence_viewed", entity_type=vocab.AUDIT_ENTITY,
        entity_id=case.id, after={"evidence_id": str(row.id), "kind": row.kind},
    )
    await db.commit()
    return {
        "id": str(row.id), "kind": row.kind, "source_system": row.source_system,
        "summary": row.summary, "detail": row.detail,
        "observed_at": row.observed_at.isoformat() if row.observed_at else None,
        "contains_secrets": row.contains_secrets, "is_derived": row.is_derived,
    }


@router.post("/{incident_id}/timeline", status_code=status.HTTP_201_CREATED)
async def add_timeline_entry(
    incident_id: uuid.UUID,
    payload: TimelineIn,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    case = await incident_service.get_incident_or_raise(db, incident_id, uuid.UUID(user.org_id))
    row = await investigation_service.add_timeline_entry(
        db, case, occurred_at=payload.occurred_at, event=payload.event,
        confidence=payload.confidence, actor=payload.actor,
        source_system=payload.source_system, evidence_id=payload.evidence_id,
        actor_user_id=uuid.UUID(user.user_id),
    )
    await db.commit()
    return {"id": str(row.id), "occurred_at": row.occurred_at.isoformat(),
            "event": row.event, "confidence": row.confidence}


@router.get("/{incident_id}/timeline")
async def get_timeline(
    incident_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    org_id = uuid.UUID(user.org_id)
    case = await incident_service.get_incident_or_raise(db, incident_id, org_id)
    rows = await incident_repository.list_timeline(db, case.id, org_id)
    return [
        {
            "id": str(t.id), "occurred_at": t.occurred_at.isoformat(), "event": t.event,
            "actor": t.actor, "source_system": t.source_system,
            "confidence": t.confidence,
            "evidence_id": str(t.evidence_id) if t.evidence_id else None,
        }
        for t in rows
    ]


# ── Affected systems, data, subjects ─────────────────────────────────────────────

@router.post("/{incident_id}/systems", status_code=status.HTTP_201_CREATED)
async def add_affected_system(
    incident_id: uuid.UUID,
    payload: AffectedSystemIn,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    case = await incident_service.get_incident_or_raise(db, incident_id, uuid.UUID(user.org_id))
    row = await investigation_service.record_affected_system(
        db, case, system_name=payload.system_name, system_kind=payload.system_kind,
        component=payload.component, confidence=payload.confidence,
        evidence_id=payload.evidence_id, notes=payload.notes,
        actor_user_id=uuid.UUID(user.user_id),
    )
    await db.commit()
    return {
        "id": str(row.id), "system_name": row.system_name, "system_kind": row.system_kind,
        "confidence": row.confidence,
        "known_to_consiva": row.data_source_id is not None,
    }


@router.post("/{incident_id}/analyse")
async def start_analysis(
    incident_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Queue the derivation work: ROPA data mapping, timeline seeding, risk.

    Queued rather than run inline because it reads schema baselines and iterates
    evidence. Containment is deliberately NOT queued -- a person performs a tracked
    action and attests to it, and a queued "disable account" job would be the fake
    execution §50 forbids.
    """
    case = await incident_service.get_incident_or_raise(db, incident_id, uuid.UUID(user.org_id))
    if case.status in (vocab.REPORTED, vocab.VALIDATING):
        await incident_service.transition(
            db, case, vocab.INVESTIGATING, actor_user_id=uuid.UUID(user.user_id),
            audit_action=vocab.AUDIT_INVESTIGATION_STARTED,
        )
    job = await queue.enqueue(
        db, org_id=case.org_id, job_type="incident_analysis",
        payload={"incident_id": str(case.id), "org_id": str(case.org_id)},
    )
    await db.commit()
    return {"job_id": str(job.id), "incident": _incident_response(case)}


@router.get("/{incident_id}/impact")
async def get_impact(
    incident_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    org_id = uuid.UUID(user.org_id)
    case = await incident_service.get_incident_or_raise(db, incident_id, org_id)
    systems = await incident_repository.list_affected_systems(db, case.id, org_id)
    data = await incident_repository.list_affected_data(db, case.id, org_id)
    subjects = await incident_repository.list_affected_subjects(db, case.id, org_id)
    total, basis = investigation_service.impact_total(subjects)
    return {
        "systems": [
            {
                "id": str(s.id), "system_name": s.system_name, "system_kind": s.system_kind,
                "component": s.component, "confidence": s.confidence,
                "known_to_consiva": s.data_source_id is not None, "notes": s.notes,
            }
            for s in systems
        ],
        "data_categories": [
            {
                "category": c,
                "confidence": min(
                    (d.confidence for d in data if d.data_category == c),
                    key=vocab.CONFIDENCE_ORDER.get,
                ),
                "derived_from": sorted({d.derived_from for d in data if d.data_category == c}),
                "columns": [
                    f"{d.table_name}.{d.column_name}"
                    for d in data if d.data_category == c and d.table_name
                ][:20],
            }
            for c in sorted({d.data_category for d in data})
        ],
        "subjects": [
            {
                "group": s.subject_group, "record_count": s.record_count,
                "count_basis": s.count_basis, "basis_note": s.basis_note,
                "confidence": s.confidence,
            }
            for s in subjects
        ],
        # Never a bare number: the basis travels with the total, always.
        "total": {"record_count": total, "count_basis": basis},
    }


@router.post("/{incident_id}/subjects", status_code=status.HTTP_201_CREATED)
async def record_subjects(
    incident_id: uuid.UUID,
    payload: SubjectsIn,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    case = await incident_service.get_incident_or_raise(db, incident_id, uuid.UUID(user.org_id))
    row = await investigation_service.record_affected_subjects(
        db, case, subject_group=payload.subject_group, record_count=payload.record_count,
        count_basis=payload.count_basis, basis_note=payload.basis_note,
        confidence=payload.confidence, actor_user_id=uuid.UUID(user.user_id),
    )
    await db.commit()
    return {"group": row.subject_group, "record_count": row.record_count,
            "count_basis": row.count_basis, "confidence": row.confidence}


@router.get("/{incident_id}/risk")
async def get_risk(
    incident_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    org_id = uuid.UUID(user.org_id)
    case = await incident_service.get_incident_or_raise(db, incident_id, org_id)
    current = await incident_repository.get_current_risk(db, case.id, org_id)
    if current is None:
        return {"current": None, "history": []}
    history = await incident_repository.list_risk_assessments(db, case.id, org_id)
    return {
        "current": {
            "id": str(current.id), "version": current.version,
            "risk_level": current.risk_level, "risk_score": current.risk_score,
            # Reported alongside the score, never folded into it.
            "confidence": current.confidence,
            "reason": current.reason,
            "factors": current.factors,
            # Kept in its own field so a system fact and a legal question can never be
            # mistaken for one another (§20).
            "regulatory_context": current.regulatory_context,
            "review_status": current.review_status,
        },
        "history": [
            {"version": r.version, "risk_level": r.risk_level,
             "confidence": r.confidence, "review_status": r.review_status}
            for r in history
        ],
    }


# ── Response plan, approval, containment ─────────────────────────────────────────

@router.post("/{incident_id}/plan")
async def build_plan(
    incident_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    case = await incident_service.get_incident_or_raise(db, incident_id, uuid.UUID(user.org_id))
    await response_service.build_response_plan(
        db, case, actor_user_id=uuid.UUID(user.user_id)
    )
    await db.commit()
    return await _plan_response(db, case, uuid.UUID(user.org_id))


@router.post("/{incident_id}/actions", status_code=status.HTTP_201_CREATED)
async def add_action(
    incident_id: uuid.UUID,
    payload: ActionIn,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    case = await incident_service.get_incident_or_raise(db, incident_id, uuid.UUID(user.org_id))
    row = await response_service.add_action(
        db, case, action_kind=payload.action_kind, title=payload.title,
        rationale=payload.rationale, expected_result=payload.expected_result,
        target=payload.target, assignee_label=payload.assignee_label,
        actor_user_id=uuid.UUID(user.user_id),
    )
    await db.commit()
    return {"id": str(row.id), "action_kind": row.action_kind, "status": row.status,
            "requires_approval": row.requires_approval}


@router.get("/{incident_id}/actions")
async def get_plan(
    incident_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    org_id = uuid.UUID(user.org_id)
    case = await incident_service.get_incident_or_raise(db, incident_id, org_id)
    return await _plan_response(db, case, org_id)


async def _plan_response(db: AsyncSession, case, org_id: uuid.UUID) -> dict:
    actions = await incident_repository.list_actions(db, case.id, org_id)
    executions = await incident_repository.list_executions(db, case.id, org_id)
    by_action: dict[str, list] = {}
    for execution in executions:
        by_action.setdefault(str(execution.action_id), []).append(execution)

    return {
        "actions": [
            {
                "id": str(a.id), "action_kind": a.action_kind, "title": a.title,
                "rationale": a.rationale, "expected_result": a.expected_result,
                "target": a.target, "risk": a.risk, "status": a.status,
                "requires_approval": a.requires_approval,
                "blocked_reason": a.blocked_reason,
                "assignee_label": a.assignee_label,
                # The honest field: 'tracked' means a person does this, not Consiva.
                "execution_mode": a.execution_mode,
                "executions": [
                    {
                        "id": str(e.id), "status": e.status,
                        "verification_status": e.verification_status,
                        "performed_by": e.performed_by,
                        "attestation": e.attestation,
                        "rows_affected": e.rows_affected,
                    }
                    for e in by_action.get(str(a.id), [])
                ],
            }
            for a in actions
        ],
        "summary": await response_service.plan_summary(db, case),
    }


@router.post("/{incident_id}/actions/{action_id}/decision")
async def decide_action(
    incident_id: uuid.UUID,
    action_id: uuid.UUID,
    payload: DecisionIn,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    org_id = uuid.UUID(user.org_id)
    case = await incident_service.get_incident_or_raise(db, incident_id, org_id)
    approval = await response_service.decide_action(
        db, case, action_id, reviewer_user_id=uuid.UUID(user.user_id),
        decision=payload.decision, reason=payload.reason,
    )
    summary = await response_service.plan_summary(db, case)
    if summary["ready_to_respond"] and case.status == vocab.APPROVAL_REQUIRED:
        await incident_service.transition(
            db, case, vocab.APPROVED, actor_user_id=uuid.UUID(user.user_id),
            audit_action=vocab.AUDIT_APPROVED,
        )
    await db.commit()
    return {
        "approval_id": str(approval.id), "decision": approval.decision,
        "expires_at": approval.expires_at.isoformat() if approval.expires_at else None,
        "summary": summary, "incident": _incident_response(case),
    }


@router.post("/{incident_id}/actions/{action_id}/attest")
async def attest_action(
    incident_id: uuid.UUID,
    action_id: uuid.UUID,
    payload: AttestationIn,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Record that a person carried out a tracked containment action.

    This is an attestation, not a verification. Consiva did not watch the account
    being disabled and cannot check that it was -- `verification_status` says
    `attested`, and no part of the system upgrades that to `read_back`.
    """
    case = await incident_service.get_incident_or_raise(db, incident_id, uuid.UUID(user.org_id))
    if case.status == vocab.APPROVED:
        await incident_service.transition(
            db, case, vocab.RESPONDING, actor_user_id=uuid.UUID(user.user_id),
            audit_action=vocab.AUDIT_ACTION_STARTED,
        )
    execution = await response_service.record_tracked_execution(
        db, case, action_id, performed_by=payload.performed_by,
        attestation=payload.attestation, actor_user_id=uuid.UUID(user.user_id),
    )
    await db.commit()
    return {
        "execution_id": str(execution.id), "status": execution.status,
        "verification_status": execution.verification_status,
        "performed_by": execution.performed_by,
        "note": execution.verification_detail.get("note") if execution.verification_detail else None,
    }


@router.post("/{incident_id}/actions/{action_id}/failed")
async def fail_action(
    incident_id: uuid.UUID,
    action_id: uuid.UUID,
    payload: DecisionIn,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    case = await incident_service.get_incident_or_raise(db, incident_id, uuid.UUID(user.org_id))
    row = await response_service.mark_action_failed(
        db, case, action_id, reason=payload.reason or "",
        actor_user_id=uuid.UUID(user.user_id),
    )
    await db.commit()
    return {"id": str(row.id), "status": row.status, "reason": row.blocked_reason}


# ── Communications and report ────────────────────────────────────────────────────

@router.post("/{incident_id}/communications", status_code=status.HTTP_201_CREATED)
async def draft_communication(
    incident_id: uuid.UUID,
    payload: CommunicationIn,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    case = await incident_service.get_incident_or_raise(db, incident_id, uuid.UUID(user.org_id))
    row = await communication_service.draft_communication(
        db, case, audience=payload.audience, subject=payload.subject,
        body=payload.body, actor_user_id=uuid.UUID(user.user_id),
    )
    await db.commit()
    return {"id": str(row.id), "audience": row.audience, "status": row.status,
            "subject": row.subject, "body": row.body}


@router.get("/{incident_id}/communications")
async def list_communications(
    incident_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    org_id = uuid.UUID(user.org_id)
    case = await incident_service.get_incident_or_raise(db, incident_id, org_id)
    rows = await incident_repository.list_communications(db, case.id, org_id)
    return [
        {
            "id": str(c.id), "audience": c.audience, "subject": c.subject,
            "body": c.body, "status": c.status,
            "external": c.audience in vocab.EXTERNAL_AUDIENCES,
            "drafted_by_model": c.drafted_by_model,
            "approved_at": c.approved_at.isoformat() if c.approved_at else None,
            "sent_at": c.sent_at.isoformat() if c.sent_at else None,
        }
        for c in rows
    ]


@router.post("/{incident_id}/communications/{communication_id}/decision")
async def decide_communication(
    incident_id: uuid.UUID,
    communication_id: uuid.UUID,
    payload: DecisionIn,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    case = await incident_service.get_incident_or_raise(db, incident_id, uuid.UUID(user.org_id))
    row = await communication_service.approve_communication(
        db, case, communication_id, reviewer_user_id=uuid.UUID(user.user_id),
        decision=payload.decision, reason=payload.reason,
    )
    await db.commit()
    return {"id": str(row.id), "status": row.status, "audience": row.audience}


@router.post("/{incident_id}/communications/{communication_id}/sent")
async def mark_sent(
    incident_id: uuid.UUID,
    communication_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Record that a PERSON sent an approved communication.

    Consiva has no outbound provider and does not send anything. An external
    communication that has not been approved cannot be marked sent, whoever asks.
    """
    case = await incident_service.get_incident_or_raise(db, incident_id, uuid.UUID(user.org_id))
    row = await communication_service.mark_communication_sent(
        db, case, communication_id, actor_user_id=uuid.UUID(user.user_id)
    )
    await db.commit()
    return {
        "id": str(row.id), "status": row.status,
        "sent_at": row.sent_at.isoformat() if row.sent_at else None,
        "note": "recorded as sent by a person; Consiva has no outbound provider",
    }


@router.post("/{incident_id}/report")
async def generate_report(
    incident_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    case = await incident_service.get_incident_or_raise(db, incident_id, uuid.UUID(user.org_id))
    row = await communication_service.generate_report(
        db, case, actor_user_id=uuid.UUID(user.user_id)
    )
    await db.commit()
    return {"id": str(row.id), "version": row.version, "body_text": row.body_text,
            "grounded_facts": row.grounded_facts, "drafted_by_model": row.drafted_by_model}


@router.get("/{incident_id}/report")
async def get_report(
    incident_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict | None:
    org_id = uuid.UUID(user.org_id)
    case = await incident_service.get_incident_or_raise(db, incident_id, org_id)
    row = await incident_repository.get_latest_report(db, case.id, org_id)
    if row is None:
        return None
    return {"id": str(row.id), "version": row.version, "body_text": row.body_text,
            "grounded_facts": row.grounded_facts, "status": row.status,
            "drafted_by_model": row.drafted_by_model}


@router.post("/{incident_id}/close")
async def close_incident(
    incident_id: uuid.UUID,
    payload: DecisionIn,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Close the incident. Requires a report to exist and a closure summary.

    An incident is finished when somebody has looked at the whole thing and said so,
    which is why CLOSED is reachable only from CLOSURE_REVIEW in the state machine.
    """
    org_id = uuid.UUID(user.org_id)
    case = await incident_service.get_incident_or_raise(db, incident_id, org_id)
    if not (payload.reason and payload.reason.strip()):
        raise IncidentNotReadyError("closing an incident requires a closure summary")
    if await incident_repository.get_latest_report(db, case.id, org_id) is None:
        raise IncidentNotReadyError(
            "an incident cannot be closed without a report; generate one first"
        )

    case.closure_summary = payload.reason.strip()
    await db.flush()
    await incident_service.transition(
        db, case, vocab.CLOSED, actor_user_id=uuid.UUID(user.user_id),
        audit_action=vocab.AUDIT_CLOSED, detail={"closure_summary": case.closure_summary},
    )
    await db.commit()
    return _incident_response(case)


@router.get("/{incident_id}/audit")
async def get_audit(
    incident_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """What happened IN CONSIVA, from the shared append-only audit_logs -- distinct
    from /timeline, which is what happened in the world."""
    org_id = uuid.UUID(user.org_id)
    case = await incident_service.get_incident_or_raise(db, incident_id, org_id)
    rows = await incident_repository.list_incident_audit(db, case.id, org_id)
    return [
        {
            "id": str(r.id), "action": r.action,
            "actor_user_id": str(r.actor_user_id) if r.actor_user_id else None,
            "before": r.before, "after": r.after,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]
