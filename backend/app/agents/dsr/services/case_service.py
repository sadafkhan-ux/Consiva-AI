"""The DSR case: intake, state transitions, and the audit trail (§11, §12, §35, §47).

Every status change in Agent 3 goes through `transition` here. That gives one place
where three things always happen together:

    1. the move is checked against the state machine
    2. the status is written
    3. an audit entry is recorded on the SHARED audit_logs table

They are not separable. A status that changed without an audit entry is a case whose
history has a hole in it, and a status that changed without passing the state machine
is the thing §12 exists to prevent.

NO SILENT ENDINGS
-----------------
`fail` requires an error code, and the state machine says which statuses demand one.
A case cannot be put into `failed`, `rejected`, `expired` or `partially_completed`
without a reason that is queryable, not just readable in a log line.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.dsr.errors import CaseNotReadyError, DsrNotFoundError
from app.agents.dsr.rules import request_classifier
from app.agents.dsr.schemas import case
from app.agents.dsr.services import lifecycle
from app.db.models import DsrRequest
from app.db.repositories import dsr_repository
from app.services import audit_service

# The statutory response window Consiva defaults to. An organization that has a
# different obligation configures it; this is a default, not a legal assertion.
DEFAULT_SLA_DAYS = 30


# 6 random bytes -> 12 hex characters. The reference is unique per organization,
# so its entropy has to survive the birthday bound, not merely look random: at the
# 3 bytes this started with, an organization had a ~95% chance of a collision by its
# ten-thousandth case, at which point intake would start failing outright. 6 bytes
# puts that under 1% at a million cases per org.
_REFERENCE_BYTES = 6
# Draws before giving up. With the entropy above, needing even two is essentially
# unheard of; this exists so a collision is a retry rather than a 500.
_REFERENCE_ATTEMPTS = 5


def new_reference() -> str:
    """A short, human-quotable case reference. Random rather than sequential so it
    does not disclose how many DSRs an organization has received."""
    return f"DSR-{secrets.token_hex(_REFERENCE_BYTES).upper()}"


async def _unused_reference(db: AsyncSession, org_id: uuid.UUID) -> str:
    """Draw a reference this org is not already using.

    The database's unique constraint remains the real guarantee -- this only avoids
    turning an astronomically rare collision into a failed intake.
    """
    for _ in range(_REFERENCE_ATTEMPTS):
        candidate = new_reference()
        if not await dsr_repository.reference_exists(db, org_id, candidate):
            return candidate
    raise CaseNotReadyError(
        "could not allocate an unused case reference; this indicates a problem with "
        "reference generation rather than with the request"
    )


async def create_case(
    db: AsyncSession,
    *,
    org_id: uuid.UUID,
    raw_request: str,
    requester_email: str | None = None,
    requester_phone: str | None = None,
    requester_reference: str | None = None,
    idempotency_key: str | None = None,
    created_by_user_id: uuid.UUID | None = None,
    sla_days: int = DEFAULT_SLA_DAYS,
    now: datetime | None = None,
) -> tuple[DsrRequest, bool]:
    """Open a case. Returns (case, is_new).

    A repeated intake with the same idempotency key returns the EXISTING case rather
    than opening a second one for the same person -- a requester who double-submits a
    web form must not end up with two cases racing each other to delete the same row.
    """
    if not (raw_request and raw_request.strip()):
        raise CaseNotReadyError("a DSR request needs the requester's own words")
    if not any((requester_email, requester_phone, requester_reference)):
        raise CaseNotReadyError(
            "a DSR request needs at least one identifier (email, phone or reference) "
            "to search on"
        )

    if idempotency_key:
        existing = await dsr_repository.find_request_by_idempotency_key(db, org_id, idempotency_key)
        if existing is not None:
            return existing, False

    moment = now or datetime.now(UTC)
    request = await dsr_repository.create_request(
        db,
        org_id=org_id,
        reference=await _unused_reference(db, org_id),
        raw_request=raw_request.strip(),
        due_at=moment + timedelta(days=sla_days),
        requester_email=(requester_email or "").strip().lower() or None,
        requester_phone=(requester_phone or "").strip() or None,
        requester_reference=(requester_reference or "").strip() or None,
        idempotency_key=idempotency_key,
        created_by_user_id=created_by_user_id,
    )
    await audit_service.record(
        db,
        org_id=org_id,
        actor_user_id=created_by_user_id,
        action=case.AUDIT_CREATED,
        entity_type=case.AUDIT_ENTITY,
        entity_id=request.id,
        after={
            "reference": request.reference,
            "due_at": request.due_at.isoformat(),
            # The requester's own words are in raw_request on the row; the audit
            # entry records that a case was opened, not a second copy of their PII.
            "identifiers": sorted(
                k for k, v in (
                    ("email", request.requester_email),
                    ("phone", request.requester_phone),
                    ("reference", request.requester_reference),
                ) if v
            ),
        },
    )
    return request, True


async def transition(
    db: AsyncSession,
    request: DsrRequest,
    to_status: str,
    *,
    actor_user_id: uuid.UUID | None = None,
    audit_action: str | None = None,
    error_code: str | None = None,
    error_detail: str | None = None,
    detail: dict | None = None,
    now: datetime | None = None,
) -> DsrRequest:
    """Move a case to a new status, or refuse.

    The ONLY way a case's status changes. Checks the state machine, requires an error
    code where the target status demands one, writes the status, and audits it.
    """
    lifecycle.assert_transition(request.status, to_status)

    if lifecycle.requires_error_code(to_status) and not error_code:
        raise CaseNotReadyError(
            f"moving a case to {to_status} requires an error code explaining why "
            f"(one of the §38 domain codes); none was supplied"
        )
    if error_code and error_code not in case.ERROR_CODES:
        raise CaseNotReadyError(f"{error_code!r} is not a DSR domain error code")

    moment = now or datetime.now(UTC)
    before = request.status
    request.status = to_status
    request.updated_at = moment
    # Clear a stale reason when the case recovers; set the new one when it stops.
    request.error_code = error_code
    request.error_detail = error_detail
    if lifecycle.is_terminal(to_status):
        request.closed_at = moment
    if to_status == case.ESCALATED:
        request.escalated_at = moment
    await db.flush()

    await audit_service.record(
        db,
        org_id=request.org_id,
        actor_user_id=actor_user_id,
        action=audit_action or case.AUDIT_STATUS_CHANGED,
        entity_type=case.AUDIT_ENTITY,
        entity_id=request.id,
        before={"status": before},
        after={
            "status": to_status,
            "error_code": error_code,
            "error_detail": error_detail,
            **(detail or {}),
        },
    )
    return request


async def classify_case(
    db: AsyncSession,
    request: DsrRequest,
    *,
    actor_user_id: uuid.UUID | None = None,
    override_type: str | None = None,
) -> DsrRequest:
    """Classify the request, deterministically where possible (§14).

    An override is how a human resolves an ambiguous request -- it is recorded as
    method='manual' so the audit trail distinguishes "the rules decided this" from
    "a person decided this".
    """
    if override_type:
        chosen = request_classifier.coerce_model_type(override_type)
        if chosen is None:
            raise CaseNotReadyError(
                f"{override_type!r} is not a DSR request type; "
                f"expected one of {sorted(case.CLASSIFIABLE_TYPES)}"
            )
        request.request_type = chosen
        request.classification_method = request_classifier.METHOD_MANUAL
        request.classification_confidence = 1.0
        evidence = ("classified manually by a reviewer",)
    else:
        result = request_classifier.classify(request.raw_request)
        if result.ambiguous:
            # Two real requests in one message. Recorded as `other` so the case is
            # visibly unresolved rather than quietly resolved to one of them.
            request.request_type = case.OTHER
            request.classification_confidence = 0.0
        else:
            request.request_type = result.request_type or case.OTHER
            request.classification_confidence = result.confidence
        request.classification_method = result.method
        evidence = result.evidence

    await db.flush()
    await audit_service.record(
        db,
        org_id=request.org_id,
        actor_user_id=actor_user_id,
        action=case.AUDIT_CLASSIFIED,
        entity_type=case.AUDIT_ENTITY,
        entity_id=request.id,
        after={
            "request_type": request.request_type,
            "method": request.classification_method,
            "confidence": request.classification_confidence,
            "evidence": list(evidence),
        },
    )
    if request.status == case.RECEIVED:
        await transition(db, request, case.CLASSIFIED, actor_user_id=actor_user_id)
    return request


async def get_case_or_raise(db: AsyncSession, request_id: uuid.UUID, org_id: uuid.UUID) -> DsrRequest:
    """Fetch a case in this org, or 404.

    A case in ANOTHER org raises exactly the same error as one that does not exist.
    Distinguishing them would confirm the id, which is the IDOR disclosure org
    scoping exists to prevent (§37).
    """
    request = await dsr_repository.get_request(db, request_id, org_id)
    if request is None:
        raise DsrNotFoundError(f"DSR case {request_id} not found")
    return request
