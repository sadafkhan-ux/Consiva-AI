"""Orchestration for Agent 3, and its entry points from the worker.

Lives in app/services/ next to ropa_run_service.py rather than under agents/dsr/
for the same reason that one does: it is the seam between the platform (jobs,
sessions, transactions) and the agent's own logic, and keeping it here means
app/jobs/worker.py imports one module per agent from one place.

WORKER CONTRACT
---------------
Both entry points take only (id, org_id) and open their own session, because a
worker job is not inside a request. Every path here ends with the case in an
explicit status -- a job that raises still leaves the case `failed` with a domain
error code, so a crashed worker never leaves a case looking like it is still
running (§39, §47).
"""

from __future__ import annotations

import logging
import uuid

from app.agents.dsr.connectors import factory
from app.agents.dsr.errors import DsrError
from app.agents.dsr.rules import constraints as rules
from app.agents.dsr.schemas import case
from app.agents.dsr.services import (
    case_service,
    execution_service,
    planning_service,
    search_service,
)
from app.db.repositories import dsr_repository, ropa_repository
from app.db.session import async_session_factory

logger = logging.getLogger(__name__)


async def execute_queued_search(request_id: uuid.UUID, org_id: uuid.UUID, job_id: uuid.UUID | None = None) -> None:
    """Worker entry point for `dsr_search`.

    Searches every authorized source, writes evidence, builds the action plan, and
    leaves the case in the status the outcome actually warrants -- search_completed,
    review_required, approval_required, or failed with a code.
    """
    async with async_session_factory() as db:
        request = await case_service.get_case_or_raise(db, request_id, org_id)
        try:
            summary = await search_service.run_search(
                db, request, job_id=job_id, correlation_id=str(request_id),
            )
        except DsrError as exc:
            await case_service.transition(
                db, request, case.FAILED,
                audit_action=case.AUDIT_FAILED,
                error_code=exc.code or case.ERR_SEARCH_FAILED,
                error_detail=exc.message,
            )
            await db.commit()
            logger.warning("DSR search could not start for %s: %s", request.reference, exc.code)
            return
        except Exception as exc:
            await case_service.transition(
                db, request, case.FAILED,
                audit_action=case.AUDIT_FAILED,
                error_code=case.ERR_SEARCH_FAILED,
                error_detail=f"{type(exc).__name__} while searching",
            )
            await db.commit()
            logger.exception("Unexpected error in DSR search for %s", request.reference)
            raise

        await case_service.transition(
            db, request, case.SEARCH_COMPLETED,
            audit_action=case.AUDIT_SEARCH_COMPLETED,
            error_code=summary.outcome_code,
            error_detail=_search_detail(summary),
            detail={
                "sources_searched": summary.sources_searched,
                "sources_failed": summary.sources_failed,
                "evidence_count": summary.evidence_count,
            },
        )

        # Ambiguity stops here. Building a plan against records that may belong to
        # two different people is exactly what §42 scenario 5 forbids.
        if summary.ambiguous:
            await case_service.transition(
                db, request, case.REVIEW_REQUIRED,
                audit_action=case.AUDIT_REVIEW_REQUESTED,
                error_code=case.ERR_MULTIPLE_MATCHES,
                error_detail=(
                    "more than one subject matched the supplied identifiers; a human "
                    "must confirm which records belong to the requester"
                ),
            )
            await db.commit()
            return

        grants = await grants_for(db, request.org_id)
        plan, actions = await planning_service.build_plan(
            db, request, grants=grants,
            retention_rules=await retention_rules_for(db, request.org_id),
        )
        await case_service.transition(
            db, request, next_status_after_planning(plan, actions),
            audit_action=case.AUDIT_ACTION_PLANNED,
            detail={"plan_id": str(plan.id), "action_count": len(actions)},
        )
        await db.commit()


async def execute_queued_actions(request_id: uuid.UUID, org_id: uuid.UUID, job_id: uuid.UUID | None = None) -> None:
    """Worker entry point for `dsr_execute`.

    Executes every approved action on the current plan. One failing action does not
    abandon the rest: each is attempted, each records its own outcome, and the case
    ends `execution_verified` or `partially_completed` depending on what actually
    verified.
    """
    async with async_session_factory() as db:
        request = await case_service.get_case_or_raise(db, request_id, org_id)
        plan = await dsr_repository.get_current_plan(db, request.id, request.org_id)
        if plan is None:
            await case_service.transition(
                db, request, case.FAILED, audit_action=case.AUDIT_FAILED,
                error_code=case.ERR_ACTION_BLOCKED,
                error_detail="no current action plan to execute",
            )
            await db.commit()
            return

        if request.status == case.APPROVED:
            await case_service.transition(
                db, request, case.EXECUTING, audit_action=case.AUDIT_ACTION_STARTED,
            )

        actions = await dsr_repository.list_actions(db, plan.id, request.org_id)
        runnable = [
            a for a in actions
            if a.status == "approved" or (a.status == "proposed" and not a.requires_approval)
        ]

        # Re-read at execution time rather than reusing what planning saw: a retention
        # rule added between approval and execution must block the write (§24).
        rules_at_execution = await retention_rules_for(db, request.org_id)

        succeeded, failed = 0, 0
        for action in runnable:
            try:
                await execution_service.execute_action(
                    db, request, action.id, job_id=job_id, correlation_id=str(request_id),
                    retention_rules=rules_at_execution,
                )
                succeeded += 1
            except DsrError as exc:
                # Recorded on the execution row by execute_action itself; this only
                # decides how the CASE ends.
                failed += 1
                logger.warning(
                    "DSR action %s failed for case %s: %s", action.id, request.reference, exc.code
                )

        blocked = sum(1 for a in actions if a.status == "blocked")
        if failed or blocked:
            plan.status = "partially_executed"
        elif succeeded:
            plan.status = "executed"
        await db.flush()

        counts = {"succeeded": succeeded, "failed": failed, "blocked": blocked}
        status, code = status_after_execution(succeeded=succeeded, failed=failed, blocked=blocked)
        await case_service.transition(
            db, request, status,
            audit_action=case.AUDIT_ACTION_VERIFIED if status == case.EXECUTION_VERIFIED
            else case.AUDIT_FAILED,
            error_code=code,
            error_detail=_execution_detail(succeeded, failed, blocked),
            detail=counts,
        )
        if status == case.EXECUTION_VERIFIED:
            # Even a partial execution owes the requester a response explaining both
            # halves -- what changed and what did not (§47). A wholly failed one stops
            # at FAILED, which is recoverable, so a human can retry it.
            await case_service.transition(
                db, request, case.RESPONSE_PENDING,
                error_code=code,
                error_detail=_execution_detail(succeeded, failed, blocked),
                detail=counts,
            )
        await db.commit()


def status_after_execution(*, succeeded: int, failed: int, blocked: int) -> tuple[str, str | None]:
    """Where a case goes once its actions have been attempted, and why.

    EXECUTION_VERIFIED asserts that the system CONFIRMED the outcome (§23). Reaching
    it with zero successes and several failures would state something that did not
    happen, so a wholly failed execution goes to FAILED instead -- which is
    deliberately not terminal, so a connector outage does not permanently kill a case
    that can simply be retried.

    A mixed result is still EXECUTION_VERIFIED, because some records genuinely did
    change and were read back, but it carries ACTION_PARTIAL so neither the case nor
    the response can present itself as a clean success.
    """
    if succeeded == 0 and failed > 0:
        return case.FAILED, case.ERR_ACTION_FAILED
    if failed or blocked:
        return case.EXECUTION_VERIFIED, case.ERR_ACTION_PARTIAL
    return case.EXECUTION_VERIFIED, None


def closing_status(*, blocked: int, failed: int) -> str:
    """COMPLETED or PARTIALLY_COMPLETED.

    Telling a requester their case is "completed" when one of their records was
    retained under a policy, or when an action failed, is telling them something
    untrue about their own data. Both statuses are terminal; only one of them claims
    everything was done.
    """
    return case.PARTIALLY_COMPLETED if (blocked or failed) else case.COMPLETED


def _execution_detail(succeeded: int, failed: int, blocked: int) -> str | None:
    if not (failed or blocked):
        return None
    parts = []
    if succeeded:
        parts.append(f"{succeeded} action(s) completed and verified")
    if failed:
        parts.append(f"{failed} failed")
    if blocked:
        parts.append(f"{blocked} blocked by a constraint")
    return "; ".join(parts)


def next_status_after_planning(plan, actions) -> str:
    """Where a case goes once its plan exists.

    Three outcomes, and the middle one is the defect this function was extracted to
    fix. APPROVAL_REQUIRED is only correct when there is something a reviewer can
    actually decide: the only thing that moves a case out of that status is a
    decision recorded against an action, so routing a plan with nothing to approve
    there left it waiting forever for a decision nobody could make.

      * nothing actionable  -> RESPONSE_PENDING. No match, or every action blocked
                               by a constraint. The requester is still owed an
                               explanation (§47), so this is never a silent close.
      * something to decide -> APPROVAL_REQUIRED.
      * nothing to decide   -> APPROVED, so execution becomes eligible immediately.
                               An access request's disclosures are the request being
                               fulfilled, not a change anyone needs to authorize.
    """
    actionable = [a for a in actions if a.status == "proposed"]
    if not actionable:
        return case.RESPONSE_PENDING
    if any(a.requires_approval for a in actionable):
        return case.APPROVAL_REQUIRED
    return case.APPROVED


async def retention_rules_for(db, org_id: uuid.UUID) -> tuple:
    """This organisation's configured retention rules, as engine rule objects.

    Loaded fresh at both planning and execution. The constraint engine stays a pure
    function of its inputs -- it never reads a database -- so this is the seam where
    stored configuration becomes applied policy.
    """
    rows = await dsr_repository.list_retention_rules(db, org_id)
    return rules.rules_from_config(rows)


async def grants_for(db, org_id: uuid.UUID) -> dict:
    """Resolve every DSR authorization in this org to a validated grant, keyed by
    source name -- what the planner needs to evaluate constraints without opening a
    connection."""
    grants: dict = {}
    for authorization in await dsr_repository.list_source_authorizations(db, org_id):
        data_source = await ropa_repository.get_data_source(db, authorization.data_source_id, org_id)
        if data_source is None:
            continue
        try:
            grants[data_source.name] = factory.build_grant(
                authorization, source_name=data_source.name
            )
        except DsrError as exc:
            # A misconfigured authorization must not silently vanish -- the planner
            # sees no grant for this source and blocks its actions with a reason.
            logger.warning(
                "DSR authorization for source %s is invalid and will block its actions: %s",
                data_source.name, exc.message,
            )
    return grants


def _search_detail(summary) -> str | None:
    if summary.ambiguous:
        return "more than one subject matched the supplied identifiers"
    if not summary.found_anything and summary.sources_failed:
        return f"no record found; {len(summary.sources_failed)} source(s) could not be searched"
    if not summary.found_anything:
        return "no record matched the supplied identifiers in any authorized source"
    return None
