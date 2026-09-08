import logging
import uuid
from typing import get_args

from langgraph.types import Command
from sqlalchemy.ext.asyncio import AsyncSession
from tenacity import retry, stop_after_attempt, wait_exponential

from app.agents.consent_agent.graph import get_compiled_graph
from app.core.exceptions import (
    FindingAlreadyDecidedError,
    InvalidEditError,
    NotFoundError,
    ReasonRequiredError,
)
from app.db.models import ConsentFinding
from app.db.repositories import finding_repository
from app.llm.schemas import Category, Priority, RiskLevel
from app.lookup import repository as lookup_repository
from app.services import audit_service

logger = logging.getLogger(__name__)

# The same enum/type constraints the LLM's own output is held to (llm/schemas.py's
# Literal types), applied to the one path where a HUMAN writes these fields. Derived
# via get_args() from the Literals themselves so the two can never drift apart.
_EDIT_FIELD_VALIDATORS: dict[str, tuple] = {
    "category": tuple(get_args(Category)),
    "risk_level": tuple(get_args(RiskLevel)),
    "priority": tuple(get_args(Priority)),
}


def _validate_edited_payload(edited_payload: dict) -> None:
    """A reviewer edit must satisfy the same constraints as the LLM output it
    overrides -- previously an out-of-enum string (e.g. risk_level='banana') would be
    silently persisted, since apply_edit only whitelists WHICH fields are editable,
    not what values they may hold."""
    for field, allowed in _EDIT_FIELD_VALIDATORS.items():
        if field in edited_payload and edited_payload[field] not in allowed:
            raise InvalidEditError(
                f"edited_payload.{field} must be one of {sorted(allowed)}, got {edited_payload[field]!r}"
            )
    if "finding_text" in edited_payload:
        text = edited_payload["finding_text"]
        if not isinstance(text, str) or not text.strip():
            raise InvalidEditError("edited_payload.finding_text must be a non-empty string")
    if "requires_human_review" in edited_payload and not isinstance(edited_payload["requires_human_review"], bool):
        raise InvalidEditError("edited_payload.requires_human_review must be a boolean")


async def _get_finding_in_org_or_raise(db: AsyncSession, finding_id: uuid.UUID, org_id: uuid.UUID) -> ConsentFinding:
    """finding_repository.get_finding enforces org_id itself (via a join to
    consent_scans) -- this backend connects to Postgres directly, so the RLS policies
    in migrations/0001_init.sql don't apply here, and that repository-level join is
    the actual tenant isolation enforcement, not just this wrapper."""
    finding = await finding_repository.get_finding(db, finding_id, org_id)
    if finding is None:
        raise NotFoundError(f"Finding {finding_id} not found")
    return finding


async def approve_finding(
    db: AsyncSession, *, finding_id: uuid.UUID, org_id: uuid.UUID, reviewer_user_id: uuid.UUID, reason: str | None
) -> ConsentFinding:
    finding = await _get_finding_in_org_or_raise(db, finding_id, org_id)

    # Reject/edit require a reason at the request-schema level; approve stays optional
    # EXCEPT for high-risk findings, where waving a serious violation through with no
    # recorded rationale is exactly what the audit trail exists to prevent.
    if finding.risk_level == "high" and not (reason and reason.strip()):
        raise ReasonRequiredError("Approving a high-risk finding requires a reason for the audit trail.")

    updated = await finding_repository.update_finding_status(db, finding_id, "approved", org_id)
    if updated is None:
        raise FindingAlreadyDecidedError(
            f"Finding {finding_id} is no longer pending (current status: {finding.status})"
        )
    await finding_repository.create_approval(
        db, finding_id=finding_id, reviewer_user_id=reviewer_user_id, decision="approved", reason=reason
    )
    await audit_service.record(
        db, org_id=org_id, actor_user_id=reviewer_user_id, action="finding.approved",
        entity_type="consent_finding", entity_id=finding_id,
        before={"status": finding.status}, after={"status": "approved", "reason": reason},
        agent_run_id=finding.agent_run_id,
    )
    await db.commit()

    await _maybe_resume_agent_run(finding.agent_run_id)
    # Returns the `finding` fetched at the top of this function, not update_finding_status's/
    # apply_edit's own return value. This is only correct because that repository call is an
    # ORM-enabled `UPDATE ... RETURNING ConsentFinding` on THIS SAME session -- SQLAlchemy
    # merges the returned row into the already-identity-mapped `finding` instance in place,
    # so its attributes (status, etc.) reflect the new values by the time we get here. If a
    # future change moves that update onto a different session/connection, this stops being
    # true and callers (the API route reads finding.status directly) would see stale data.
    return finding


async def reject_finding(
    db: AsyncSession, *, finding_id: uuid.UUID, org_id: uuid.UUID, reviewer_user_id: uuid.UUID, reason: str | None
) -> ConsentFinding:
    finding = await _get_finding_in_org_or_raise(db, finding_id, org_id)

    updated = await finding_repository.update_finding_status(db, finding_id, "rejected", org_id)
    if updated is None:
        raise FindingAlreadyDecidedError(
            f"Finding {finding_id} is no longer pending (current status: {finding.status})"
        )
    await finding_repository.create_approval(
        db, finding_id=finding_id, reviewer_user_id=reviewer_user_id, decision="rejected", reason=reason
    )
    await audit_service.record(
        db, org_id=org_id, actor_user_id=reviewer_user_id, action="finding.rejected",
        entity_type="consent_finding", entity_id=finding_id,
        before={"status": finding.status}, after={"status": "rejected", "reason": reason},
        agent_run_id=finding.agent_run_id,
    )
    await db.commit()

    await _maybe_resume_agent_run(finding.agent_run_id)
    # Returns the `finding` fetched at the top of this function, not update_finding_status's/
    # apply_edit's own return value. This is only correct because that repository call is an
    # ORM-enabled `UPDATE ... RETURNING ConsentFinding` on THIS SAME session -- SQLAlchemy
    # merges the returned row into the already-identity-mapped `finding` instance in place,
    # so its attributes (status, etc.) reflect the new values by the time we get here. If a
    # future change moves that update onto a different session/connection, this stops being
    # true and callers (the API route reads finding.status directly) would see stale data.
    return finding


async def edit_finding(
    db: AsyncSession,
    *,
    finding_id: uuid.UUID,
    org_id: uuid.UUID,
    reviewer_user_id: uuid.UUID,
    edited_payload: dict,
    reason: str | None,
) -> ConsentFinding:
    _validate_edited_payload(edited_payload)
    finding = await _get_finding_in_org_or_raise(db, finding_id, org_id)
    before = {"status": finding.status, "finding_text": finding.finding_text, "risk_level": finding.risk_level}

    reclassify = edited_payload.get("reclassify_cookie")
    if reclassify:
        # Build Plan Component 3: "store the human-confirmed classification back into
        # the lookup table so the system learns." Opt-in via an explicit payload key —
        # not every edit is a reclassification, and this has no UI yet to drive it from.
        await lookup_repository.upsert_human_confirmed(
            db,
            name_pattern=reclassify["name_pattern"],
            category=reclassify["category"],
            vendor=reclassify.get("vendor"),
        )

    updated = await finding_repository.apply_edit(db, finding_id, edited_payload, org_id)
    if updated is None:
        raise FindingAlreadyDecidedError(
            f"Finding {finding_id} is no longer pending (current status: {finding.status})"
        )
    await finding_repository.create_approval(
        db, finding_id=finding_id, reviewer_user_id=reviewer_user_id, decision="edited",
        reason=reason, edited_payload=edited_payload,
    )
    await audit_service.record(
        db, org_id=org_id, actor_user_id=reviewer_user_id, action="finding.edited",
        entity_type="consent_finding", entity_id=finding_id,
        before=before, after={"status": "edited", **edited_payload},
        agent_run_id=finding.agent_run_id,
    )
    await db.commit()

    await _maybe_resume_agent_run(finding.agent_run_id)
    # Returns the `finding` fetched at the top of this function, not update_finding_status's/
    # apply_edit's own return value. This is only correct because that repository call is an
    # ORM-enabled `UPDATE ... RETURNING ConsentFinding` on THIS SAME session -- SQLAlchemy
    # merges the returned row into the already-identity-mapped `finding` instance in place,
    # so its attributes (status, etc.) reflect the new values by the time we get here. If a
    # future change moves that update onto a different session/connection, this stops being
    # true and callers (the API route reads finding.status directly) would see stale data.
    return finding


@retry(reraise=True, stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=5))
async def _resume_graph(thread_id: str) -> None:
    graph = await get_compiled_graph()
    await graph.ainvoke(Command(resume=True), config={"configurable": {"thread_id": thread_id}})


async def _maybe_resume_agent_run(agent_run_id: uuid.UUID) -> None:
    """Every decision attempts a resume. If other findings from the same run are still
    pending, human_review_gate just re-interrupts immediately (docs/architecture §E) —
    so it's always safe to try, not just on the "last" decision.

    Retries a bounded 3x on a transient failure (e.g. a checkpointer DB hiccup) before
    giving up -- without this, a transient error on the LAST pending finding's decision
    would permanently strand the run at status="paused" with nothing left to trigger
    another resume attempt. Still swallowed (never raised to the caller) after
    exhausting retries: the reviewer's decision is already committed by this point and
    must not be reported as failed just because the graph didn't also close out."""
    try:
        await _resume_graph(str(agent_run_id))
    except Exception:
        logger.exception(
            "Could not resume agent_run %s after a review decision (3 attempts exhausted)", agent_run_id
        )
