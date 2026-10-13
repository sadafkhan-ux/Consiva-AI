"""Agent 3 (DSR Fulfillment) API.

Every route is authenticated and org-scoped through core/integration_auth, which accepts
EITHER a signed-in person OR an org-scoped service key (csv_...) holding `dsr:operate`
(cases, config reads) or `dsr:config` (config writes). The org comes from the verified
credential in both cases, so tenancy behaves identically across all agents. Nothing here accepts an org_id from the caller -- it comes from
the verified token and nowhere else, which is what closes the IDOR §37 asks about.

The long-running steps (search, execution) are QUEUED on the existing agent_jobs
queue rather than run inside the request. A DSR search takes as long as the slowest
authorized source; an execution must survive a dropped connection. Both return the
case with its new status, and the UI polls -- it never shows progress the backend
did not report (§33).
"""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

from fastapi import APIRouter, Depends, Request, status
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.dsr.connectors import authorization
from app.agents.dsr.errors import CaseNotReadyError, DsrNotFoundError, EvidenceTooLargeError
from app.agents.dsr.schemas import case as case_vocab
from app.agents.dsr.schemas.evidence_push import EvidencePush
from app.agents.dsr.services import (
    approval_service,
    case_service,
    evidence_push_service,
    identity_service,
    lifecycle,
    planning_service,
    response_service,
    sla_service,
)
from app.core import integration_auth
from app.core.security import CurrentUser
from app.db.repositories import dsr_repository, ropa_repository
from app.db.session import get_db
from app.jobs import queue
from app.services import audit_service, dsr_run_service

router = APIRouter(prefix="/api/v1/dsr", tags=["dsr"])


# ── Request models ───────────────────────────────────────────────────────────────

class DsrCreate(BaseModel):
    raw_request: str = Field(min_length=1, max_length=5000)
    requester_email: str | None = Field(default=None, max_length=320)
    requester_phone: str | None = Field(default=None, max_length=40)
    requester_reference: str | None = Field(default=None, max_length=200)
    idempotency_key: str | None = Field(default=None, max_length=200)

    @field_validator("requester_email")
    @classmethod
    def _looks_like_email(cls, value: str | None) -> str | None:
        if value and "@" not in value:
            raise ValueError("requester_email must contain '@'")
        return value


class VerifyIdentity(BaseModel):
    """Either answer a challenge, or record an out-of-band verification."""

    challenge: str | None = Field(default=None, max_length=200)
    manual: bool = False
    evidence_note: str | None = Field(default=None, max_length=1000)


class ClassifyRequest(BaseModel):
    override_type: str | None = Field(default=None, max_length=40)


class SourceAuthorizationIn(BaseModel):
    """A source's DSR authorization, as an administrator configures it.

    Everything here is an allowlist, and every one defaults to empty. A source with no
    authorization -- or one whose lists are empty -- permits nothing, which is the
    behaviour to want if this endpoint is ever called with a half-built body.
    """

    data_source_id: uuid.UUID
    searchable_tables: list[str] = Field(default_factory=list, max_length=500)
    identity_tables: list[str] = Field(default_factory=list, max_length=500)
    identifier_columns: dict[str, dict[str, str]] = Field(default_factory=dict)
    returnable_columns: dict[str, list[str]] = Field(default_factory=dict)
    record_key_columns: dict[str, list[str]] = Field(default_factory=dict)
    erasable_columns: dict[str, list[str]] = Field(default_factory=dict)
    allow_execution: bool = False
    # The NAME of the environment entry holding the write password -- never the
    # password. Validated as a name, and never echoed back in a response.
    write_credential_ref: str | None = Field(default=None, max_length=128)

    @field_validator("write_credential_ref")
    @classmethod
    def _looks_like_a_ref(cls, value: str | None) -> str | None:
        if value and not value.replace("_", "").isalnum():
            raise ValueError(
                "write_credential_ref is the NAME of a secret, not the secret itself; "
                "it must look like an environment variable name"
            )
        return value


class RetentionRuleIn(BaseModel):
    """One retention rule. `authority` is mandatory because a block with no attributable
    source is exactly the unattributable legal claim the engine refuses to make."""

    table_name: str = Field(min_length=1, max_length=63)
    date_column: str = Field(min_length=1, max_length=63)
    retention_days: int = Field(gt=0, le=100 * 365)
    authority: str = Field(min_length=3, max_length=200)
    data_source_id: uuid.UUID | None = None
    applies_to_operations: list[str] = Field(default_factory=lambda: ["delete_record"])
    notes: str | None = Field(default=None, max_length=2000)

    @field_validator("applies_to_operations")
    @classmethod
    def _known_operations(cls, value: list[str]) -> list[str]:
        unknown = [op for op in value if op not in case_vocab.MUTATING_OPERATIONS]
        if unknown:
            raise ValueError(
                f"a retention rule may only restrict a writing operation; "
                f"{unknown} are not in {sorted(case_vocab.MUTATING_OPERATIONS)}"
            )
        return value


class RecordSelection(BaseModel):
    """One record, and what the requester decided about it."""

    evidence_id: uuid.UUID
    action: str
    # Only meaningful for `correct`. The new value is always supplied explicitly --
    # the agent never infers what someone meant to change.
    corrections: dict | None = None

    @field_validator("action")
    @classmethod
    def _known_action(cls, value: str) -> str:
        if value not in case_vocab.SELECTIONS:
            raise ValueError(f"action must be one of {sorted(case_vocab.SELECTIONS)}")
        return value


class PlanSelections(BaseModel):
    selections: list[RecordSelection] = Field(default_factory=list, max_length=2000)


class Decision(BaseModel):
    decision: str
    reason: str | None = Field(default=None, max_length=2000)
    edited_payload: dict | None = None

    @field_validator("decision")
    @classmethod
    def _known_decision(cls, value: str) -> str:
        if value not in approval_service.DECISIONS:
            raise ValueError(f"decision must be one of {sorted(approval_service.DECISIONS)}")
        return value


# ── Response shaping ─────────────────────────────────────────────────────────────

def _case_response(request, *, identity_status: str | None = None) -> dict:
    """The case as the UI sees it. `allowed_transitions` comes from the same state
    machine the backend enforces, so the UI can grey out what is genuinely
    impossible rather than maintaining its own copy of the rules (§33, §34)."""
    return {
        "id": str(request.id),
        "reference": request.reference,
        "status": request.status,
        "request_type": request.request_type,
        "classification_method": request.classification_method,
        "classification_confidence": request.classification_confidence,
        "raw_request": request.raw_request,
        "requester_email": request.requester_email,
        "requester_phone": request.requester_phone,
        "requester_reference": request.requester_reference,
        "error_code": request.error_code,
        "error_detail": request.error_detail,
        "identity_status": identity_status,
        "allowed_transitions": sorted(lifecycle.allowed_transitions(request.status)),
        "is_terminal": lifecycle.is_terminal(request.status),
        "sla": sla_service.sla_view(request),
        "created_at": request.created_at.isoformat() if request.created_at else None,
        "closed_at": request.closed_at.isoformat() if request.closed_at else None,
    }


async def _case_with_identity(db: AsyncSession, request) -> dict:
    verification = await dsr_repository.get_latest_identity_verification(
        db, request.id, request.org_id
    )
    return _case_response(request, identity_status=identity_service.effective_status(verification))


# ── Cases ────────────────────────────────────────────────────────────────────────

@router.post("/requests", status_code=status.HTTP_201_CREATED)
async def create_request(
    payload: DsrCreate,
    user: CurrentUser = Depends(integration_auth.get_dsr_operator),
    db: AsyncSession = Depends(get_db),
) -> dict:
    request, is_new = await case_service.create_case(
        db,
        org_id=uuid.UUID(user.org_id),
        raw_request=payload.raw_request,
        requester_email=payload.requester_email,
        requester_phone=payload.requester_phone,
        requester_reference=payload.requester_reference,
        idempotency_key=payload.idempotency_key,
        created_by_user_id=uuid.UUID(user.user_id),
    )
    if is_new:
        # Classify immediately: it reads only the requester's own words, so it is not
        # behind the identity gate, and a classified case is one a reviewer can triage.
        await case_service.classify_case(db, request, actor_user_id=uuid.UUID(user.user_id))
    await db.commit()
    return await _case_with_identity(db, request)


@router.get("/requests")
async def list_requests(
    status_filter: str | None = None,
    limit: int = 50,
    user: CurrentUser = Depends(integration_auth.get_dsr_operator),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    rows = await dsr_repository.list_requests(
        db, uuid.UUID(user.org_id), status=status_filter, limit=min(limit, 200)
    )
    return [_case_response(r) for r in rows]


@router.get("/requests/{request_id}")
async def get_request(
    request_id: uuid.UUID,
    user: CurrentUser = Depends(integration_auth.get_dsr_operator),
    db: AsyncSession = Depends(get_db),
) -> dict:
    request = await case_service.get_case_or_raise(db, request_id, uuid.UUID(user.org_id))
    return await _case_with_identity(db, request)


@router.post("/requests/{request_id}/classify")
async def classify(
    request_id: uuid.UUID,
    payload: ClassifyRequest,
    user: CurrentUser = Depends(integration_auth.get_dsr_operator),
    db: AsyncSession = Depends(get_db),
) -> dict:
    request = await case_service.get_case_or_raise(db, request_id, uuid.UUID(user.org_id))
    await case_service.classify_case(
        db, request, actor_user_id=uuid.UUID(user.user_id), override_type=payload.override_type
    )
    await db.commit()
    return await _case_with_identity(db, request)


# ── Identity verification ────────────────────────────────────────────────────────

@router.post("/requests/{request_id}/identity/challenge")
async def start_identity_challenge(
    request_id: uuid.UUID,
    user: CurrentUser = Depends(integration_auth.get_dsr_operator),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Issue an email challenge.

    The plaintext challenge is returned ONCE, to the authenticated operator, for
    delivery to the requester -- no outbound email provider is configured in this
    project, so pretending it was sent would be a lie. It is stored only as a hash
    and cannot be recovered afterwards.
    """
    request = await case_service.get_case_or_raise(db, request_id, uuid.UUID(user.org_id))
    verification, challenge = await identity_service.start_challenge(db, request)
    if request.status == case_vocab.RECEIVED:
        await case_service.transition(
            db, request, case_vocab.IDENTITY_PENDING,
            actor_user_id=uuid.UUID(user.user_id),
            audit_action=case_vocab.AUDIT_IDENTITY_STARTED,
        )
    await audit_service.record(
        db, org_id=request.org_id, actor_user_id=uuid.UUID(user.user_id),
        action=case_vocab.AUDIT_IDENTITY_STARTED, entity_type=case_vocab.AUDIT_ENTITY,
        entity_id=request.id,
        after={"method": "email_challenge", "expires_at": verification.expires_at.isoformat()},
    )
    await db.commit()
    return {
        "verification_id": str(verification.id),
        "status": verification.status,
        "expires_at": verification.expires_at.isoformat(),
        "challenge": challenge,
        "delivery_note": (
            "No outbound email provider is configured. Deliver this challenge to the "
            "requester yourself; it is stored only as a hash and cannot be shown again."
        ),
    }


@router.post("/requests/{request_id}/identity/verify")
async def verify_identity(
    request_id: uuid.UUID,
    payload: VerifyIdentity,
    user: CurrentUser = Depends(integration_auth.get_dsr_operator),
    db: AsyncSession = Depends(get_db),
) -> dict:
    request = await case_service.get_case_or_raise(db, request_id, uuid.UUID(user.org_id))

    if payload.manual:
        verification = await identity_service.verify_manually(
            db, request,
            reviewer_user_id=uuid.UUID(user.user_id),
            evidence_note=payload.evidence_note or "",
        )
    elif payload.challenge:
        verification = await identity_service.submit_challenge(db, request, payload.challenge)
    else:
        raise CaseNotReadyError("supply either a challenge or manual=true with an evidence note")

    satisfied = identity_service.is_satisfied(verification)
    if satisfied and request.status in (case_vocab.RECEIVED, case_vocab.IDENTITY_PENDING, case_vocab.CLASSIFIED):
        await case_service.transition(
            db, request, case_vocab.IDENTITY_VERIFIED,
            actor_user_id=uuid.UUID(user.user_id),
            audit_action=case_vocab.AUDIT_IDENTITY_VERIFIED,
        )
    await db.commit()
    return {
        "status": verification.status,
        "satisfied": satisfied,
        "attempts": verification.attempts,
        "case": await _case_with_identity(db, request),
    }


# ── Search ───────────────────────────────────────────────────────────────────────

@router.post("/requests/{request_id}/search")
async def start_search(
    request_id: uuid.UUID,
    user: CurrentUser = Depends(integration_auth.get_dsr_operator),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Queue the subject search.

    The identity gate is checked HERE as well as inside the search itself, so an
    unverified case is refused at the API boundary rather than queueing a job that
    will only fail once a worker picks it up.
    """
    request = await case_service.get_case_or_raise(db, request_id, uuid.UUID(user.org_id))
    await identity_service.assert_identity_satisfied(db, request)

    await case_service.transition(
        db, request, case_vocab.SEARCHING,
        actor_user_id=uuid.UUID(user.user_id),
        audit_action=case_vocab.AUDIT_SEARCH_STARTED,
    )
    job = await queue.enqueue(
        db, org_id=request.org_id, job_type="dsr_search",
        payload={"request_id": str(request.id), "org_id": str(request.org_id)},
    )
    await db.commit()
    return {"job_id": str(job.id), "case": await _case_with_identity(db, request)}


@router.get("/requests/{request_id}/results")
async def list_results(
    request_id: uuid.UUID,
    user: CurrentUser = Depends(integration_auth.get_dsr_operator),
    db: AsyncSession = Depends(get_db),
) -> dict:
    org_id = uuid.UUID(user.org_id)
    request = await case_service.get_case_or_raise(db, request_id, org_id)
    runs = await dsr_repository.list_search_runs(db, request.id, org_id)
    evidence = await dsr_repository.list_evidence(db, request.id, org_id)
    return {
        "search_runs": [
            {
                "id": str(r.id), "source": r.source_name, "status": r.status,
                "matches": r.match_count, "distinct_subjects": r.distinct_subject_count,
                # Same defensive coalesce response_service.py's _collect_facts already
                # applies to this same column, for the same reason: a NOT NULL default
                # at the schema level doesn't protect a row written before that
                # constraint existed, or left mid-write by a source search that raised.
                "tables_searched": list(r.tables_searched or []), "error_code": r.error_code,
                "error_detail": r.error_detail,
                "completed_at": r.completed_at.isoformat() if r.completed_at else None,
            }
            for r in runs
        ],
        "evidence": [
            {
                "id": str(e.id), "source": e.source_name, "table": e.table_name,
                "matched_column": e.matched_column, "identifier_kind": e.identifier_kind,
                "match_type": e.match_type, "confidence": e.confidence,
                "record_reference": e.record_reference, "record_snapshot": e.record_snapshot or {},
                # Agent 2's data category for the column that matched, so a reviewer
                # reads "Contact Data" rather than having to know what `customer_email`
                # signifies. Null where the classifier could not place the column.
                "ropa_category": e.ropa_category,
                "observed_at": e.observed_at.isoformat() if e.observed_at else None,
            }
            for e in evidence
        ],
    }


# ── Push evidence: Consiva runs the lookup itself and posts back the result ─────
#
# The pull path above (`POST /search`) has the agent connect to the customer's
# database itself. An org with no registered source (no `ropa_data_sources` row,
# and therefore nothing `/config/sources` could ever authorize) can never use
# it -- every case ends in `failed`/SOURCE_NOT_AUTHORIZED. Consiva already holds
# those credentials; this lets it run the search and hand the agent the matched
# records instead, following the same push shape ROPA's `/evidence` already uses
# (app/api/v1/routes/ropa.py's `ingest_evidence`) -- same auth story (an org's
# integration key, `dsr:operate` -- the SAME scope that already gates /search and
# every other case-mutating route; no new scope was needed), same idempotency
# contract (a retried push with the same key returns what the first one produced).

@router.post("/requests/{request_id}/evidence", status_code=status.HTTP_201_CREATED)
async def push_evidence(
    request_id: uuid.UUID,
    http_request: Request,
    user: CurrentUser = Depends(integration_auth.get_dsr_operator),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Accept evidence Consiva's own lookup produced, analyze it, and leave the
    case in the SAME state a pull search would reach: classified, evidenced,
    planned (constraints and retention applied), and -- for an ambiguous or
    truncated result -- routed to REVIEW_REQUIRED exactly as an ambiguous
    connector-driven search already is.

    Refused with 409 unless the case's identity is verified (the same gate
    `/search` applies) and the case is in a status that may legally move to
    SEARCHING -- `failed` included, so a case stuck there by
    SOURCE_NOT_AUTHORIZED can be recovered this way. Refused with 413, the limit
    named in the message, over a documented ceiling on tables, rows per table,
    total rows, or payload bytes -- never silently truncated, which would hand
    the plan builder an incomplete answer it believes is complete.

    The payload bytes are checked on the RAW body, before it is parsed at all, so
    an oversized push is refused without ever being held in parsed form; see
    evidence_push_service.MAX_PUSH_PAYLOAD_BYTES.
    """
    raw = await http_request.body()
    if len(raw) > evidence_push_service.MAX_PUSH_PAYLOAD_BYTES:
        raise EvidenceTooLargeError(
            f"push payload is {len(raw)} byte(s); max "
            f"{evidence_push_service.MAX_PUSH_PAYLOAD_BYTES} bytes"
        )
    try:
        push = EvidencePush.model_validate_json(raw)
    except ValidationError as exc:
        # The same 422 envelope every other malformed body on this API gets
        # (app/main.py's RequestValidationError handler) -- "too large" (413,
        # above) and "malformed" are different outcomes and must not share a code.
        raise RequestValidationError(exc.errors()) from exc

    org_id = uuid.UUID(user.org_id)
    request = await case_service.get_case_or_raise(db, request_id, org_id)
    run, plan, _actions = await dsr_run_service.ingest_pushed_evidence(
        db, request, push,
        integration_key_id=user.key_id, integration_key_name=user.key_name,
        actor_user_id=uuid.UUID(user.user_id),
    )
    await db.commit()
    return {
        "search_run_id": str(run.id),
        "evidence_count": run.match_count,
        "case": await _case_with_identity(db, request),
        "plan": (await _plan_response(db, request, org_id)) if plan is not None else None,
    }


class ExecutionReportItem(BaseModel):
    """What Consiva actually carried out for ONE manifest action."""

    action_id: uuid.UUID
    status: str
    rows_affected: int | None = Field(default=None, ge=0)
    occurred_at: datetime | None = None
    error_text: str | None = Field(default=None, max_length=2000)

    @field_validator("status")
    @classmethod
    def _known_status(cls, value: str) -> str:
        if value not in ("done", "failed", "skipped"):
            raise ValueError("status must be one of 'done', 'failed', 'skipped'")
        return value


class ExecutionReport(BaseModel):
    items: list[ExecutionReportItem] = Field(min_length=1, max_length=2000)


@router.post("/requests/{request_id}/execution-report")
async def execution_report(
    request_id: uuid.UUID,
    payload: ExecutionReport,
    user: CurrentUser = Depends(integration_auth.get_dsr_operator),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Consiva reports what it actually did with the execution manifest
    `POST /execute` handed back: per action id, whether it was done/failed/skipped,
    how many rows it affected, when, and why if it failed.

    Idempotent per action id -- reporting the same action twice returns the first
    recorded outcome rather than double-counting it. Once every manifest action has
    a reported outcome, the case closes the same way a connector-driven execution
    already does: EXECUTION_VERIFIED (or FAILED if nothing succeeded), then
    RESPONSE_PENDING, so `/response` and `/complete` need no change at all --
    `/complete` already closes a case COMPLETED or PARTIALLY_COMPLETED depending on
    whether anything here failed or was skipped.
    """
    request = await case_service.get_case_or_raise(db, request_id, uuid.UUID(user.org_id))
    result = await dsr_run_service.record_execution_report(
        db, request, payload.items, actor_user_id=uuid.UUID(user.user_id),
    )
    await db.commit()
    return {
        "recorded": len(result["executions"]),
        "case_closed": result["closed"],
        "case": await _case_with_identity(db, request),
    }


@router.post("/requests/{request_id}/evidence/purge")
async def purge_pushed_evidence(
    request_id: uuid.UUID,
    user: CurrentUser = Depends(integration_auth.get_dsr_operator),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Clear the encrypted raw package(s) POST /evidence stored for this case.

    Refused unless the case is terminal (completed/partially_completed/rejected/
    cancelled/expired) -- the raw payload may still be needed while the case is
    open (a re-plan can read dsr_evidence, which this never touches, but a
    reviewer investigating a push itself may still need the original). `/complete`
    already calls this automatically when it closes a case COMPLETED or
    PARTIALLY_COMPLETED; this exists for every other terminal path, and for an
    operator who wants it purged sooner.
    """
    org_id = uuid.UUID(user.org_id)
    request = await case_service.get_case_or_raise(db, request_id, org_id)
    if not lifecycle.is_terminal(request.status):
        raise CaseNotReadyError(
            f"case {request.reference} is {request.status!r}, not terminal; evidence may "
            "only be purged once the case is closed"
        )
    purged = await evidence_push_service.purge(db, request.id, org_id)
    await audit_service.record(
        db, org_id=org_id, actor_user_id=uuid.UUID(user.user_id),
        action=case_vocab.AUDIT_EVIDENCE_PURGED, entity_type=case_vocab.AUDIT_ENTITY,
        entity_id=request.id, after={"pushes_purged": purged},
    )
    await db.commit()
    return {"pushes_purged": purged}


# ── Plan, review, approval ───────────────────────────────────────────────────────

@router.get("/requests/{request_id}/plan")
async def get_plan(
    request_id: uuid.UUID,
    user: CurrentUser = Depends(integration_auth.get_dsr_operator),
    db: AsyncSession = Depends(get_db),
) -> dict:
    org_id = uuid.UUID(user.org_id)
    request = await case_service.get_case_or_raise(db, request_id, org_id)
    return await _plan_response(db, request, org_id)


def _action_dict(a) -> dict:
    """One action, in the shape both the plan view and a push-mode execution
    manifest return it. `data_source_id is None` is how a reader (or a future
    maintainer) tells a pushed-evidence action apart from a connector-backed one --
    the former has no registered source to trace back to."""
    return {
        "id": str(a.id), "source": a.source_name, "table": a.table_name,
        "operation": a.operation, "payload": a.operation_payload,
        "reason": a.reason, "expected_result": a.expected_result,
        "risk": a.risk, "requires_approval": a.requires_approval,
        "status": a.status, "blocked_reason": a.blocked_reason,
        "requester_explanation": a.requester_explanation,
        "record_reference": a.record_reference,
        "pushed_evidence": a.data_source_id is None,
    }


async def _plan_response(db: AsyncSession, request, org_id: uuid.UUID) -> dict:
    plan = await dsr_repository.get_current_plan(db, request.id, org_id)
    if plan is None:
        return {"plan": None, "actions": [], "decisions": None}

    actions = await dsr_repository.list_actions(db, plan.id, org_id)
    return {
        "plan": {
            "id": str(plan.id), "version": plan.version, "status": plan.status,
            "summary": plan.summary, "requires_approval": plan.requires_approval,
            "constraints_evaluated": plan.constraints_evaluated,
        },
        "actions": [_action_dict(a) for a in actions],
        "decisions": await approval_service.plan_decision_summary(db, request, plan.id),
    }


@router.post("/requests/{request_id}/plan")
async def build_plan_from_selections(
    request_id: uuid.UUID,
    payload: PlanSelections,
    user: CurrentUser = Depends(integration_auth.get_dsr_operator),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Build (or rebuild) the action plan from the requester's per-record choices.

    This is what makes the email-first flow possible: the person looks at what was
    actually found and decides record by record, instead of the plan being inferred
    from one sentence typed at intake. A previous plan is superseded rather than
    edited, so an approval already given never silently attaches to different work.

    Every selection must name evidence belonging to THIS case -- an evidence id from
    somewhere else is rejected rather than ignored, because silently dropping it
    would produce a plan that does not match what the person chose.
    """
    org_id = uuid.UUID(user.org_id)
    request = await case_service.get_case_or_raise(db, request_id, org_id)

    # Selections describe records; records are only readable once identity is
    # established, so the same gate applies here as to the search itself.
    await identity_service.assert_identity_satisfied(db, request)

    known = {str(e.id) for e in await dsr_repository.list_evidence(db, request.id, org_id)}
    unknown = [str(s.evidence_id) for s in payload.selections if str(s.evidence_id) not in known]
    if unknown:
        raise CaseNotReadyError(
            f"{len(unknown)} selection(s) name evidence that does not belong to case "
            f"{request.reference}"
        )

    selections = {
        str(s.evidence_id): {"action": s.action, "corrections": s.corrections or {}}
        for s in payload.selections
    }
    grants = await dsr_run_service.grants_for(db, org_id)
    plan, actions = await planning_service.build_plan(
        db, request, grants=grants, selections=selections,
        retention_rules=await dsr_run_service.retention_rules_for(db, org_id),
    )

    # Not guarded by can_transition: a silently-skipped move here is how a case ends
    # up claiming APPROVED while freshly-planned work still awaits a decision. If the
    # move is genuinely illegal that is a bug worth surfacing, not swallowing.
    next_status = dsr_run_service.next_status_after_planning(plan, actions)
    if next_status != request.status:
        await case_service.transition(
            db, request, next_status,
            actor_user_id=uuid.UUID(user.user_id),
            audit_action=case_vocab.AUDIT_ACTION_PLANNED,
            detail={
                "plan_id": str(plan.id), "version": plan.version,
                "action_count": len(actions),
                "selected": _selection_counts(payload.selections),
            },
        )
    await db.commit()
    return await _plan_response(db, request, org_id)


def _selection_counts(selections: list[RecordSelection]) -> dict:
    """The live summary the review screen shows, counted from what was actually
    submitted rather than tallied in the browser."""
    counts = dict.fromkeys(sorted(case_vocab.SELECTIONS), 0)
    for s in selections:
        counts[s.action] += 1
    return counts


@router.post("/requests/{request_id}/actions/{action_id}/decision")
async def decide_action(
    request_id: uuid.UUID,
    action_id: uuid.UUID,
    payload: Decision,
    user: CurrentUser = Depends(integration_auth.get_dsr_operator),
    db: AsyncSession = Depends(get_db),
) -> dict:
    org_id = uuid.UUID(user.org_id)
    request = await case_service.get_case_or_raise(db, request_id, org_id)
    approval = await approval_service.decide_action(
        db, request, action_id,
        reviewer_user_id=uuid.UUID(user.user_id),
        decision=payload.decision,
        reason=payload.reason,
        edited_payload=payload.edited_payload,
    )

    # Once every action is decided, move the case to APPROVED so execution becomes
    # eligible. Half-decided plans stay put -- executing some actions while others
    # await review is how a requester gets a partial answer nobody chose to give.
    plan = await dsr_repository.get_current_plan(db, request.id, org_id)
    summary = await approval_service.plan_decision_summary(db, request, plan.id) if plan else {}
    if summary.get("ready_to_execute") and request.status == case_vocab.APPROVAL_REQUIRED:
        await case_service.transition(
            db, request, case_vocab.APPROVED, actor_user_id=uuid.UUID(user.user_id),
            audit_action=case_vocab.AUDIT_APPROVED,
        )
    elif summary.get("nothing_to_execute") and request.status == case_vocab.APPROVAL_REQUIRED:
        # Everything was rejected or blocked. The case still owes the requester an
        # answer explaining that (§47).
        await case_service.transition(
            db, request, case_vocab.RESPONSE_PENDING, actor_user_id=uuid.UUID(user.user_id),
        )
    await db.commit()
    return {
        "approval_id": str(approval.id),
        "decision": approval.decision,
        "expires_at": approval.expires_at.isoformat() if approval.expires_at else None,
        "decisions": summary,
        "case": await _case_with_identity(db, request),
    }


# ── Execution ────────────────────────────────────────────────────────────────────

@router.post("/requests/{request_id}/execute")
async def execute(
    request_id: uuid.UUID,
    user: CurrentUser = Depends(integration_auth.get_dsr_operator),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Queue execution of the approved actions -- or, when every runnable action
    came from PUSHED evidence (no connector, no credential this agent could use to
    write to that source), return them as an execution manifest instead, for
    Consiva to carry out and report back on via POST /execution-report. See
    dsr_run_service.issue_execution_manifest's docstring for why `execute` must
    not attempt the erasure itself in that case.

    Refused unless the case is APPROVED. Safe to call twice: a connector-driven
    action claims an idempotency key before it does anything (§25), and reissuing
    a manifest is idempotent by construction -- nothing changes until Consiva
    reports back.
    """
    org_id = uuid.UUID(user.org_id)
    request = await case_service.get_case_or_raise(db, request_id, org_id)
    if request.status != case_vocab.APPROVED:
        raise CaseNotReadyError(
            f"case {request.reference} is {request.status}; execution requires an "
            "approved plan"
        )

    plan = await dsr_repository.get_current_plan(db, request.id, org_id)
    actions = await dsr_repository.list_actions(db, plan.id, org_id) if plan else []
    runnable = [
        a for a in actions
        if a.status == "approved" or (a.status == "proposed" and not a.requires_approval)
    ]
    if runnable and all(a.data_source_id is None for a in runnable):
        manifest = await dsr_run_service.issue_execution_manifest(
            db, request, actor_user_id=uuid.UUID(user.user_id)
        )
        await db.commit()
        return {
            "mode": "push",
            "manifest": [_action_dict(a) for a in manifest],
            "case": await _case_with_identity(db, request),
        }

    job = await queue.enqueue(
        db, org_id=request.org_id, job_type="dsr_execute",
        payload={"request_id": str(request.id), "org_id": str(request.org_id)},
    )
    await audit_service.record(
        db, org_id=request.org_id, actor_user_id=uuid.UUID(user.user_id),
        action=case_vocab.AUDIT_ACTION_STARTED, entity_type=case_vocab.AUDIT_ENTITY,
        entity_id=request.id, after={"job_id": str(job.id)},
    )
    await db.commit()
    return {"mode": "connector", "job_id": str(job.id), "case": await _case_with_identity(db, request)}


@router.get("/requests/{request_id}/executions")
async def list_executions(
    request_id: uuid.UUID,
    user: CurrentUser = Depends(integration_auth.get_dsr_operator),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    org_id = uuid.UUID(user.org_id)
    request = await case_service.get_case_or_raise(db, request_id, org_id)
    rows = await dsr_repository.list_executions(db, request.id, org_id)
    return [
        {
            "id": str(e.id), "action_id": str(e.action_id), "status": e.status,
            "rows_affected": e.rows_affected,
            # Deliberately two separate fields: "the write ran" and "a read-back
            # confirmed it" are different claims and the UI must show both (§23).
            "verification_status": e.verification_status,
            "verification_detail": e.verification_detail,
            "verified_at": e.verified_at.isoformat() if e.verified_at else None,
            "error_code": e.error_code, "error_detail": e.error_detail,
            "attempts": e.attempts,
        }
        for e in rows
    ]


# ── Response ─────────────────────────────────────────────────────────────────────

@router.post("/requests/{request_id}/response")
async def generate_response(
    request_id: uuid.UUID,
    user: CurrentUser = Depends(integration_auth.get_dsr_operator),
    db: AsyncSession = Depends(get_db),
) -> dict:
    request = await case_service.get_case_or_raise(db, request_id, uuid.UUID(user.org_id))
    response = await response_service.build_response(
        db, request, actor_user_id=uuid.UUID(user.user_id)
    )
    await db.commit()
    return {
        "id": str(response.id), "version": response.version, "status": response.status,
        "body_text": response.body_text, "grounded_facts": response.grounded_facts,
        "drafted_by_model": response.drafted_by_model,
    }


@router.get("/requests/{request_id}/response")
async def get_response(
    request_id: uuid.UUID,
    user: CurrentUser = Depends(integration_auth.get_dsr_operator),
    db: AsyncSession = Depends(get_db),
) -> dict | None:
    org_id = uuid.UUID(user.org_id)
    request = await case_service.get_case_or_raise(db, request_id, org_id)
    response = await dsr_repository.get_latest_response(db, request.id, org_id)
    if response is None:
        return None
    return {
        "id": str(response.id), "version": response.version, "status": response.status,
        "body_text": response.body_text, "grounded_facts": response.grounded_facts,
        "drafted_by_model": response.drafted_by_model,
        "sent_at": response.sent_at.isoformat() if response.sent_at else None,
    }


@router.post("/requests/{request_id}/complete")
async def complete_case(
    request_id: uuid.UUID,
    user: CurrentUser = Depends(integration_auth.get_dsr_operator),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Mark the response sent and close the case.

    Requires a response to exist: a case cannot be completed without the requester
    having been answered, which is why COMPLETED is reachable only from
    RESPONSE_PENDING in the state machine.

    Closes as PARTIALLY_COMPLETED when any action was blocked or failed. Telling a
    requester their case is "completed" when one of their records was retained under
    a policy is telling them something untrue about their own data (§47).
    """
    org_id = uuid.UUID(user.org_id)
    request = await case_service.get_case_or_raise(db, request_id, org_id)
    response = await dsr_repository.get_latest_response(db, request.id, org_id)
    if response is None:
        raise CaseNotReadyError(
            f"case {request.reference} has no response; generate one before completing it"
        )
    response.status = "sent"
    response.sent_at = datetime.now(UTC)
    response.approved_by_user_id = uuid.UUID(user.user_id)
    await db.flush()

    plan = await dsr_repository.get_current_plan(db, request.id, org_id)
    actions = await dsr_repository.list_actions(db, plan.id, org_id) if plan else []
    blocked = sum(1 for a in actions if a.status == "blocked")
    failed = sum(1 for a in actions if a.status == "failed")
    closing = dsr_run_service.closing_status(blocked=blocked, failed=failed)

    await case_service.transition(
        db, request, closing,
        actor_user_id=uuid.UUID(user.user_id),
        audit_action=(
            case_vocab.AUDIT_COMPLETED if closing == case_vocab.COMPLETED
            else case_vocab.AUDIT_PARTIALLY_COMPLETED
        ),
        # PARTIALLY_COMPLETED is one of the statuses the state machine requires a
        # reason for, so the case row says which half did not happen.
        error_code=None if closing == case_vocab.COMPLETED else case_vocab.ERR_ACTION_PARTIAL,
        error_detail=(
            None if closing == case_vocab.COMPLETED
            else f"{blocked} action(s) blocked, {failed} failed; see the plan for reasons"
        ),
        detail={"blocked": blocked, "failed": failed, "response_id": str(response.id)},
    )
    await audit_service.record(
        db, org_id=org_id, actor_user_id=uuid.UUID(user.user_id),
        action=case_vocab.AUDIT_RESPONSE_SENT, entity_type=case_vocab.AUDIT_ENTITY,
        entity_id=request.id,
        after={"response_id": str(response.id), "version": response.version},
    )

    # The case just became terminal. Any raw pushed-evidence package is purged
    # here automatically -- dsr_evidence (what the engine actually reasoned over)
    # is untouched, so nothing above (the response, the audit trail) depended on
    # it still existing.
    purged = await evidence_push_service.purge(db, request.id, org_id)
    if purged:
        await audit_service.record(
            db, org_id=org_id, actor_user_id=uuid.UUID(user.user_id),
            action=case_vocab.AUDIT_EVIDENCE_PURGED, entity_type=case_vocab.AUDIT_ENTITY,
            entity_id=request.id, after={"pushes_purged": purged},
        )

    await db.commit()
    return await _case_with_identity(db, request)


# ── Configuration: authorized sources and retention rules ────────────────────────
#
# Before these existed both were written straight to the table by hand, which meant no
# validation, no audit entry, and no way for an administrator to see what a source was
# actually permitted to do. Both are admin-only: they decide what the agent may read
# and what it may erase.

def _require_admin(user: CurrentUser) -> None:
    """Configuration changes what the agent is allowed to touch, so they are not
    something an ordinary member may do. A token with no role is not an admin."""
    # A service key has no role by design; it reaches the write routes only through
    # get_dsr_config_writer, which has already required its dsr:config scope.
    if user.key_name is None and user.role != "admin":
        raise CaseNotReadyError(
            "changing DSR source authorization or retention policy requires an "
            "administrator"
        )


@router.get("/config/sources")
async def list_source_authorizations(
    user: CurrentUser = Depends(integration_auth.get_dsr_operator),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """What each source is permitted to do. Never returns a credential -- the row holds
    only the NAME of one, and even that is reported as a boolean.

    Looks up every org's source once (list_data_sources), not once per
    authorization row -- an org with N authorized sources previously cost N+1
    queries here (one per row via get_data_source) for no reason, since every
    row's source is necessarily in this same org's source list anyway."""
    org_id = uuid.UUID(user.org_id)
    rows = await dsr_repository.list_source_authorizations(db, org_id)
    sources_by_id = {s.id: s for s in await ropa_repository.list_data_sources(db, org_id)}
    out = []
    for a in rows:
        source = sources_by_id.get(a.data_source_id)
        out.append({
            "id": str(a.id),
            "data_source_id": str(a.data_source_id),
            "source_name": source.name if source else None,
            "searchable_tables": a.searchable_tables,
            "identity_tables": a.identity_tables,
            "identifier_columns": a.identifier_columns,
            "returnable_columns": a.returnable_columns,
            "record_key_columns": a.record_key_columns,
            "erasable_columns": a.erasable_columns,
            "allow_execution": a.allow_execution,
            "write_credential_configured": bool(a.write_credential_ref),
            "enabled": a.enabled,
        })
    return out


@router.put("/config/sources", status_code=status.HTTP_200_OK)
async def put_source_authorization(
    payload: SourceAuthorizationIn,
    user: CurrentUser = Depends(integration_auth.get_dsr_config_writer),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Replace a source's DSR authorization.

    Validated through the SAME resolver the connector uses, before it is stored. A
    configuration that would fail at search time -- a malformed identifier, an identity
    table outside the searchable set -- is rejected here instead, where an
    administrator can see why.
    """
    _require_admin(user)
    org_id = uuid.UUID(user.org_id)
    source = await ropa_repository.get_data_source(db, payload.data_source_id, org_id)
    if source is None:
        raise DsrNotFoundError(f"data source {payload.data_source_id} not found")

    candidate = SimpleNamespace(
        enabled=True,
        searchable_tables=payload.searchable_tables,
        identity_tables=payload.identity_tables,
        identifier_columns=payload.identifier_columns,
        returnable_columns=payload.returnable_columns,
        record_key_columns=payload.record_key_columns,
        erasable_columns=payload.erasable_columns,
        allow_execution=payload.allow_execution,
        write_credential_ref=payload.write_credential_ref,
    )
    authorization.resolve(candidate, source_name=source.name)  # raises on anything invalid

    row = await dsr_repository.upsert_source_authorization(
        db, org_id=org_id, data_source_id=payload.data_source_id,
        searchable_tables=payload.searchable_tables,
        identity_tables=payload.identity_tables,
        identifier_columns=payload.identifier_columns,
        returnable_columns=payload.returnable_columns,
        record_key_columns=payload.record_key_columns,
        erasable_columns=payload.erasable_columns,
        allow_execution=payload.allow_execution,
        write_credential_ref=payload.write_credential_ref,
    )
    await audit_service.record(
        db, org_id=org_id, actor_user_id=uuid.UUID(user.user_id),
        action="dsr.source_authorization_changed",
        entity_type="dsr_source_authorization", entity_id=row.id,
        after={
            "source": source.name,
            "searchable_tables": payload.searchable_tables,
            "allow_execution": payload.allow_execution,
            # The NAME only. The secret itself never enters the audit trail.
            "write_credential_ref": payload.write_credential_ref,
        },
    )
    await db.commit()
    return {"id": str(row.id), "source_name": source.name, "allow_execution": row.allow_execution}


@router.get("/config/retention")
async def list_retention(
    user: CurrentUser = Depends(integration_auth.get_dsr_operator),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    rows = await dsr_repository.list_retention_rules(db, uuid.UUID(user.org_id))
    return [
        {
            "id": str(r.id), "table_name": r.table_name, "date_column": r.date_column,
            "retention_days": r.retention_days, "authority": r.authority,
            "applies_to_operations": r.applies_to_operations,
            "data_source_id": str(r.data_source_id) if r.data_source_id else None,
            "scope": "source" if r.data_source_id else "organisation",
            "notes": r.notes, "enabled": r.enabled,
        }
        for r in rows
    ]


@router.put("/config/retention", status_code=status.HTTP_200_OK)
async def put_retention_rule(
    payload: RetentionRuleIn,
    user: CurrentUser = Depends(integration_auth.get_dsr_config_writer),
    db: AsyncSession = Depends(get_db),
) -> dict:
    _require_admin(user)
    org_id = uuid.UUID(user.org_id)
    row = await dsr_repository.upsert_retention_rule(
        db, org_id=org_id, table_name=payload.table_name, date_column=payload.date_column,
        retention_days=payload.retention_days, authority=payload.authority,
        data_source_id=payload.data_source_id,
        applies_to_operations=payload.applies_to_operations,
        notes=payload.notes, created_by_user_id=uuid.UUID(user.user_id),
    )
    await audit_service.record(
        db, org_id=org_id, actor_user_id=uuid.UUID(user.user_id),
        action="dsr.retention_rule_changed",
        entity_type="dsr_retention_rule", entity_id=row.id,
        after={
            "table": payload.table_name, "retention_days": payload.retention_days,
            "authority": payload.authority,
        },
    )
    await db.commit()
    return {"id": str(row.id), "table_name": row.table_name, "authority": row.authority}


@router.delete("/config/retention/{rule_id}", status_code=status.HTTP_200_OK)
async def disable_retention_rule(
    rule_id: uuid.UUID,
    user: CurrentUser = Depends(integration_auth.get_dsr_config_writer),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Disables rather than deletes. A rule that blocked an erasure last month is part
    of why that case ended as it did, and the audit trail refers to it."""
    _require_admin(user)
    org_id = uuid.UUID(user.org_id)
    if not await dsr_repository.delete_retention_rule(db, rule_id, org_id):
        raise DsrNotFoundError(f"retention rule {rule_id} not found")
    await audit_service.record(
        db, org_id=org_id, actor_user_id=uuid.UUID(user.user_id),
        action="dsr.retention_rule_disabled",
        entity_type="dsr_retention_rule", entity_id=rule_id, after={"enabled": False},
    )
    await db.commit()
    return {"id": str(rule_id), "enabled": False}


# ── Audit timeline ───────────────────────────────────────────────────────────────

@router.get("/requests/{request_id}/audit")
async def case_audit(
    request_id: uuid.UUID,
    user: CurrentUser = Depends(integration_auth.get_dsr_operator),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """The case timeline, read from the SHARED append-only audit_logs table. There is
    no separate DSR event log -- this is the same table Agent 1's decisions land in."""
    org_id = uuid.UUID(user.org_id)
    request = await case_service.get_case_or_raise(db, request_id, org_id)
    rows = await dsr_repository.list_case_audit(db, request.id, org_id)
    return [
        {
            "id": str(r.id), "action": r.action, "actor_user_id": str(r.actor_user_id) if r.actor_user_id else None,
            "before": r.before, "after": r.after,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]
