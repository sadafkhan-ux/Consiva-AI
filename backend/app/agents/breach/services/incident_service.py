"""Incident intake, classification and state transitions (§8, §9, §32, §42).

Every status change in Agent 4 goes through `transition` here, so three things always
happen together: the move is checked against the state machine, the status is written,
and an entry lands in the SHARED audit_logs. A status that changed without an audit
entry is an incident whose history has a hole in it.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.breach.errors import (
    IncidentNotFoundError,
    IncidentNotReadyError,
    InvalidIncidentError,
)
from app.agents.breach.rules import classification
from app.agents.breach.schemas import incident as vocab
from app.agents.breach.services import lifecycle
from app.db.models import IncidentCase
from app.db.repositories import incident_repository
from app.services import audit_service

# The default window an organisation gives itself to complete initial assessment.
# NOT a legal deadline: §41 is explicit that statutory clocks must come from
# configuration or approved knowledge, never be hard-coded. This is an internal
# working target, and the reason it is measured from `detected_at` rather than
# `reported_at` is that awareness, not paperwork, is what most clocks run from.
DEFAULT_RESPONSE_WINDOW = timedelta(hours=72)

_REFERENCE_BYTES = 5          # 10 hex characters; see the DSR agent for the birthday maths
_REFERENCE_ATTEMPTS = 5


def new_reference() -> str:
    """A short, quotable incident reference. Random rather than sequential so it does
    not disclose how many incidents an organisation has had."""
    return f"INC-{secrets.token_hex(_REFERENCE_BYTES).upper()}"


async def _unused_reference(db: AsyncSession, org_id: uuid.UUID) -> str:
    for _ in range(_REFERENCE_ATTEMPTS):
        candidate = new_reference()
        if not await incident_repository.reference_exists(db, org_id, candidate):
            return candidate
    raise IncidentNotReadyError(
        "could not allocate an unused incident reference; this indicates a problem "
        "with reference generation rather than with the report"
    )


async def create_incident(
    db: AsyncSession,
    *,
    org_id: uuid.UUID,
    title: str,
    description: str,
    source: str,
    detected_at: datetime,
    reported_by: str | None = None,
    occurred_at: datetime | None = None,
    initial_severity: str | None = None,
    response_window: timedelta = DEFAULT_RESPONSE_WINDOW,
    correlation_id: str | None = None,
    idempotency_key: str | None = None,
    created_by_user_id: uuid.UUID | None = None,
    now: datetime | None = None,
) -> tuple[IncidentCase, bool]:
    """Open an incident. Returns (incident, is_new).

    A repeated intake with the same idempotency key returns the EXISTING incident
    rather than opening a second one -- a SIEM that retries its webhook must not
    produce two incidents for one alert, which would split the evidence for a single
    event across two investigations.
    """
    moment = now or datetime.now(UTC)

    if not (title and title.strip()):
        raise InvalidIncidentError("an incident needs a title")
    if not (description and description.strip()):
        raise InvalidIncidentError(
            "an incident needs a description; what was observed is the evidence the "
            "whole investigation starts from"
        )
    if source not in vocab.INCIDENT_SOURCES:
        raise InvalidIncidentError(
            f"{source!r} is not a known incident source; expected one of "
            f"{sorted(vocab.INCIDENT_SOURCES)}"
        )
    if initial_severity and initial_severity not in vocab.SEVERITIES:
        raise InvalidIncidentError(f"{initial_severity!r} is not a severity level")

    # Timestamps are checked rather than trusted. A detection date in the future is
    # almost always a timezone mistake, and one that silently stands would put the
    # response clock in the wrong place.
    if detected_at > moment + timedelta(minutes=5):
        raise InvalidIncidentError(
            "detected_at is in the future; check the timezone on the reporting system"
        )
    if occurred_at and occurred_at > detected_at + timedelta(minutes=5):
        raise InvalidIncidentError(
            "the incident cannot have occurred after it was detected; check the "
            "occurred_at and detected_at timestamps"
        )

    if idempotency_key:
        existing = await incident_repository.find_by_idempotency_key(db, org_id, idempotency_key)
        if existing is not None:
            return existing, False

    row = await incident_repository.create_incident(
        db,
        org_id=org_id,
        reference=await _unused_reference(db, org_id),
        title=title.strip(),
        description=description.strip(),
        source=source,
        detected_at=detected_at,
        reported_by=(reported_by or "").strip() or None,
        occurred_at=occurred_at,
        initial_severity=initial_severity,
        # Measured from detection, not from when somebody got round to filing it.
        due_at=detected_at + response_window,
        correlation_id=correlation_id,
        idempotency_key=idempotency_key,
        created_by_user_id=created_by_user_id,
    )
    await audit_service.record(
        db,
        org_id=org_id,
        actor_user_id=created_by_user_id,
        action=vocab.AUDIT_CREATED,
        entity_type=vocab.AUDIT_ENTITY,
        entity_id=row.id,
        after={
            "reference": row.reference,
            "source": source,
            "detected_at": detected_at.isoformat(),
            "due_at": row.due_at.isoformat() if row.due_at else None,
            "initial_severity": initial_severity,
        },
    )
    return row, True


async def transition(
    db: AsyncSession,
    case: IncidentCase,
    to_status: str,
    *,
    actor_user_id: uuid.UUID | None = None,
    audit_action: str | None = None,
    error_code: str | None = None,
    error_detail: str | None = None,
    detail: dict | None = None,
    now: datetime | None = None,
) -> IncidentCase:
    """Move an incident to a new status, or refuse.

    The ONLY way an incident's status changes.
    """
    lifecycle.assert_transition(case.status, to_status)

    if lifecycle.requires_error_code(to_status) and not error_code:
        raise IncidentNotReadyError(
            f"moving an incident to {to_status} requires a domain error code "
            "explaining why; none was supplied"
        )
    if error_code and error_code not in vocab.ERROR_CODES:
        raise IncidentNotReadyError(f"{error_code!r} is not an incident error code")

    moment = now or datetime.now(UTC)
    before = case.status
    case.status = to_status
    case.updated_at = moment
    case.error_code = error_code
    case.error_detail = error_detail
    if lifecycle.is_terminal(to_status):
        case.closed_at = moment
    if to_status == vocab.ESCALATED:
        case.escalated_at = moment
    await db.flush()

    await audit_service.record(
        db,
        org_id=case.org_id,
        actor_user_id=actor_user_id,
        action=audit_action or vocab.AUDIT_STATUS_CHANGED,
        entity_type=vocab.AUDIT_ENTITY,
        entity_id=case.id,
        before={"status": before},
        after={
            "status": to_status, "error_code": error_code,
            "error_detail": error_detail, **(detail or {}),
        },
    )
    return case


async def classify_incident(
    db: AsyncSession,
    case: IncidentCase,
    *,
    actor_user_id: uuid.UUID | None = None,
    override_type: str | None = None,
) -> IncidentCase:
    """Classify the incident, deterministically where possible (§9).

    An override is how a human resolves an ambiguous report; it is recorded as
    method='manual' so the audit trail distinguishes "the rules decided this" from
    "a person decided this".
    """
    if override_type:
        chosen = classification.coerce_model_type(override_type)
        if chosen is None:
            raise InvalidIncidentError(
                f"{override_type!r} is not an incident type; expected one of "
                f"{sorted(vocab.CLASSIFIABLE_TYPES)}"
            )
        case.incident_type = chosen
        case.classification_method = classification.METHOD_MANUAL
        case.classification_confidence = 1.0
        evidence: tuple[str, ...] = ("classified manually by a reviewer",)
    else:
        result = classification.classify(case.title, case.description)
        if result.ambiguous:
            # The report describes competing things. Recorded as `other` so the
            # incident is visibly unresolved rather than quietly resolved to one of
            # them -- the type drives the containment plan, so guessing is expensive.
            case.incident_type = vocab.TYPE_OTHER
            case.classification_confidence = 0.0
        else:
            case.incident_type = result.incident_type or vocab.TYPE_OTHER
            case.classification_confidence = result.confidence
        case.classification_method = result.method
        evidence = result.evidence

    await db.flush()
    await audit_service.record(
        db,
        org_id=case.org_id,
        actor_user_id=actor_user_id,
        action=vocab.AUDIT_CLASSIFIED,
        entity_type=vocab.AUDIT_ENTITY,
        entity_id=case.id,
        after={
            "incident_type": case.incident_type,
            "method": case.classification_method,
            "confidence": case.classification_confidence,
            "evidence": list(evidence),
        },
    )
    return case


async def set_finding(
    db: AsyncSession,
    case: IncidentCase,
    *,
    field: str,
    confidence: str,
    actor_user_id: uuid.UUID,
    reason: str,
) -> IncidentCase:
    """Set one of the two substantive findings -- was personal data involved, was this
    a breach -- at a stated confidence.

    CONFIRMED requires a human actor and a reason, always. No rule, model or heuristic
    reaches this function with `confirmed`: `MACHINE_ASSERTABLE_CONFIDENCE` excludes
    it, and this is the guard that makes that real rather than aspirational. Saying
    "personal data was definitely involved" is a statement an organisation makes, with
    a name against it.
    """
    if field not in ("personal_data_involved", "breach_confirmed"):
        raise InvalidIncidentError(f"{field!r} is not a substantive incident finding")
    if confidence not in vocab.CONFIDENCE_LEVELS:
        raise InvalidIncidentError(f"{confidence!r} is not a confidence level")
    if confidence == vocab.CONFIRMED:
        if actor_user_id is None:
            raise IncidentNotReadyError(
                "only a named person may record a finding as confirmed"
            )
        if not (reason and reason.strip()):
            raise IncidentNotReadyError(
                "recording a finding as confirmed requires a reason naming the "
                "evidence it rests on"
            )

    before = getattr(case, field)
    setattr(case, field, confidence)
    await db.flush()
    await audit_service.record(
        db,
        org_id=case.org_id,
        actor_user_id=actor_user_id,
        action=vocab.AUDIT_INVESTIGATION_COMPLETED,
        entity_type=vocab.AUDIT_ENTITY,
        entity_id=case.id,
        before={field: before},
        after={field: confidence, "reason": reason},
    )
    return case


async def get_incident_or_raise(
    db: AsyncSession, incident_id: uuid.UUID, org_id: uuid.UUID
) -> IncidentCase:
    """Fetch an incident in this org, or 404.

    An incident in ANOTHER org raises exactly the same error as one that does not
    exist. Distinguishing them would confirm the id, and incident references are more
    sensitive than most: knowing an organisation has an incident is itself information.
    """
    case = await incident_repository.get_incident(db, incident_id, org_id)
    if case is None:
        raise IncidentNotFoundError(f"incident {incident_id} not found")
    return case
