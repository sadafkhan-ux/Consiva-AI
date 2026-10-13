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
from datetime import UTC, datetime

from app.agents.dsr.connectors import factory
from app.agents.dsr.errors import CaseNotReadyError, DsrError, DsrNotFoundError
from app.agents.dsr.rules import constraints as rules
from app.agents.dsr.schemas import case
from app.agents.dsr.schemas.evidence_push import EvidencePush
from app.agents.dsr.services import (
    case_service,
    evidence_push_service,
    execution_service,
    planning_service,
    search_service,
)
from app.db.models import DsrAction, DsrActionPlan, DsrRequest, DsrSearchRun
from app.db.repositories import dsr_repository, ropa_repository
from app.db.session import async_session_factory
from app.services import audit_service

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
        if request.status not in (case.APPROVED, case.EXECUTING):
            # A duplicate/stale `dsr_execute` job: POST /execute's connector path
            # enqueues a job without itself moving the case out of APPROVED (see
            # its docstring), so a route-level retry before this worker runs can
            # leave two jobs queued for the same case. By the time the second one
            # is dequeued, the first has already carried the case past EXECUTING
            # (to EXECUTION_VERIFIED/RESPONSE_PENDING, or FAILED). Nothing is left
            # to execute and nothing ought to be re-done -- the actions themselves
            # are protected by execution_service's idempotency-key claim either
            # way -- so this is a silent no-op rather than a crash on an illegal
            # state transition (lifecycle only allows EXECUTING -> ... from
            # APPROVED/EXECUTING, never from RESPONSE_PENDING or FAILED).
            logger.info(
                "DSR execute job for case %s arrived while the case was already %s; "
                "nothing left to execute, treating this job as a no-op",
                request.reference, request.status,
            )
            return
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
            if (a.status == "approved" or (a.status == "proposed" and not a.requires_approval))
            # A pushed-evidence action has no data_source_id -- no connector, no
            # credential this worker could use to write to it. It is reported on
            # via POST /execution-report instead (dsr_run_service.
            # record_execution_report), never attempted here; attempting it would
            # only ever raise SourceNotAuthorizedError and miscount it as failed.
            and a.data_source_id is not None
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

        # A reviewer's rejection ("rejected") is as much "this was not done" as a
        # constraint block -- both leave the record unchanged on purpose. Counting
        # only "blocked" here would let a plan with one approved-and-executed action
        # and one rejected action report as a clean, non-partial EXECUTION_VERIFIED.
        blocked = sum(1 for a in actions if a.status in ("blocked", "rejected"))
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
    retained under a policy, rejected by a reviewer, or when an action failed, is
    telling them something untrue about their own data. Both statuses are terminal;
    only one of them claims everything was done. Callers are expected to fold a
    reviewer's "rejected" action decisions into `blocked` -- a reviewer declining to
    act on a record is a deliberate non-change, exactly like a constraint block, and
    must not be invisible to this check.
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
    if summary.sources_failed:
        # Mirrors SearchSummary.outcome_code's own ordering: a source failing is
        # never folded into a clean result just because another source found
        # something. Without this branch, a case with evidence from one source and
        # a failure in another carried error_code=SEARCH_FAILED (outcome_code
        # already accounts for this) but error_detail=None -- an inconsistent,
        # self-contradicting pairing that told a reviewer reading only the detail
        # text that nothing was wrong.
        return (
            f"{len(summary.sources_searched)} source(s) searched successfully; "
            f"{len(summary.sources_failed)} source(s) could not be searched"
        )
    return None


# ── Push evidence: POST /requests/{id}/evidence's orchestration ─────────────────
#
# Mirrors execute_queued_search above, but synchronously: nothing here opens a
# network connection to a customer system (the slow part already happened on
# Consiva's side before this request arrived), so there is no job to queue. The
# low-level, schema-aware pieces (limits, the gate, building a SourceGrant and
# DsrEvidence rows from a pushed table, encrypting the raw payload) live in
# app/agents/dsr/services/evidence_push_service.py; this function is the seam
# that turns them into the SAME case states a pull-search would reach.

async def ingest_pushed_evidence(
    db,
    request: DsrRequest,
    push: EvidencePush,
    *,
    integration_key_id: uuid.UUID | None,
    integration_key_name: str | None,
    actor_user_id: uuid.UUID | None,
    now: datetime | None = None,
) -> tuple[DsrSearchRun, DsrActionPlan | None, list[DsrAction]]:
    """VALIDATE -> STORE -> ANALYZE a pushed evidence package.

    Idempotent on `push.client_reference`: a retried POST with the same value
    returns the EXISTING search run and plan rather than re-ingesting and
    re-planning -- the same contract dsr_repository.find_request_by_idempotency_key
    already gives case intake.
    """
    moment = now or datetime.now(UTC)

    if push.client_reference:
        existing_run = await dsr_repository.find_search_run_by_idempotency_key(
            db, request.org_id, push.client_reference
        )
        if existing_run is not None:
            plan = await dsr_repository.get_current_plan(db, request.id, request.org_id)
            actions = await dsr_repository.list_actions(db, plan.id, request.org_id) if plan else []
            return existing_run, plan, actions

    await evidence_push_service.assert_case_accepts_push(db, request)
    evidence_push_service.assert_subject_matches_case(request, push.subject)
    evidence_push_service.enforce_limits(push)

    run = await dsr_repository.create_search_run(
        db, org_id=request.org_id, request_id=request.id, source_name=push.source_name,
        data_source_id=None,
        identifier_kinds=evidence_push_service.search_run_identifier_kinds(push),
        correlation_id=str(request.id),
    )
    run.ingest_mode = "push"
    run.idempotency_key = push.client_reference
    run.status = "running"
    await db.flush()

    total_rows = sum(len(t.rows) for t in push.tables)
    ambiguous = push.truncated or any(t.truncated for t in push.tables)

    ciphertext = evidence_push_service.encrypt_push_payload(push)
    await dsr_repository.create_evidence_push(
        db, org_id=request.org_id, request_id=request.id, search_run_id=run.id,
        integration_key_id=integration_key_id, source_name=push.source_name,
        searched_at=push.searched_at, subject=push.subject.model_dump(exclude_none=True),
        payload_ciphertext=ciphertext, table_count=len(push.tables), row_count=total_rows,
        truncated=ambiguous, client_reference=push.client_reference,
    )

    evidence_rows = evidence_push_service.evidence_rows_for_push(request, run, push)
    await dsr_repository.add_evidence(db, request.org_id, evidence_rows)

    run.match_count = len(evidence_rows)
    run.distinct_subject_count = 1 if evidence_rows else 0
    run.tables_searched = [t.table_name for t in push.tables]
    run.completed_at = moment
    if ambiguous:
        run.status = "multiple_matches"
        run.error_code = case.ERR_MULTIPLE_MATCHES
        run.error_detail = (
            "the pushed evidence is marked truncated; the result is incomplete and must be "
            "reviewed before any action is taken"
        )
    elif not evidence_rows:
        run.status = "no_match"
        run.error_code = case.ERR_NO_MATCH
        run.error_detail = "no record was included in the pushed evidence"
    else:
        run.status = "completed"
    await db.flush()

    # NEVER a row value -- only table names, column names and counts (push-evidence
    # privacy requirement).
    logger.info(
        "DSR evidence pushed for case %s: source=%r tables=%d rows=%d truncated=%s "
        "columns_by_table=%s",
        request.reference, push.source_name, len(push.tables), total_rows, ambiguous,
        {t.table_name: list(t.columns) for t in push.tables},
    )

    await audit_service.record(
        db, org_id=request.org_id, actor_user_id=actor_user_id,
        action=case.AUDIT_EVIDENCE_PUSHED, entity_type=case.AUDIT_ENTITY, entity_id=request.id,
        after={
            "search_run_id": str(run.id), "source_name": push.source_name,
            "table_names": [t.table_name for t in push.tables], "table_count": len(push.tables),
            "row_count": total_rows, "truncated": ambiguous,
            "integration_key_name": integration_key_name, "client_reference": push.client_reference,
        },
    )

    await case_service.transition(
        db, request, case.SEARCHING, actor_user_id=actor_user_id,
        audit_action=case.AUDIT_SEARCH_STARTED, detail={"source_name": push.source_name, "mode": "push"},
    )
    await case_service.transition(
        db, request, case.SEARCH_COMPLETED, actor_user_id=actor_user_id,
        audit_action=case.AUDIT_SEARCH_COMPLETED, error_code=run.error_code, error_detail=run.error_detail,
        detail={"sources_searched": [push.source_name], "evidence_count": len(evidence_rows), "mode": "push"},
    )

    if ambiguous:
        await case_service.transition(
            db, request, case.REVIEW_REQUIRED, actor_user_id=actor_user_id,
            audit_action=case.AUDIT_REVIEW_REQUESTED, error_code=case.ERR_MULTIPLE_MATCHES,
            error_detail=run.error_detail,
        )
        return run, None, []

    grants = dict(await grants_for(db, request.org_id))
    # The pushed source's grant for THIS call's evidence. If Consiva's source_name
    # collides with an already-authorized connector source, the push grant wins for
    # the rows this call just wrote -- it is the only grant that actually describes
    # them (external_execution=True, no write_credential_ref).
    grants[push.source_name] = evidence_push_service.grant_for_push(push)
    plan, actions = await planning_service.build_plan(
        db, request, grants=grants, retention_rules=await retention_rules_for(db, request.org_id),
    )
    await case_service.transition(
        db, request, next_status_after_planning(plan, actions), actor_user_id=actor_user_id,
        audit_action=case.AUDIT_ACTION_PLANNED,
        detail={"plan_id": str(plan.id), "action_count": len(actions), "mode": "push"},
    )
    return run, plan, actions


# ── Execution manifest: POST /requests/{id}/execute's push-mode path ────────────

async def issue_execution_manifest(
    db, request: DsrRequest, *, actor_user_id: uuid.UUID | None
) -> list[DsrAction]:
    """Because the agent has no database access in push-evidence mode, `execute`
    must not run the erasure -- it hands back the approved actions as an
    EXECUTION MANIFEST instead, for Consiva to carry out and report back on via
    POST /requests/{id}/execution-report. Moves the case APPROVED -> EXECUTING
    directly: there is no connector call to queue, so there is no job."""
    plan = await dsr_repository.get_current_plan(db, request.id, request.org_id)
    if plan is None:
        raise CaseNotReadyError(f"case {request.reference} has no current action plan to execute")
    actions = await dsr_repository.list_actions(db, plan.id, request.org_id)
    runnable = [
        a for a in actions
        if a.status == "approved" or (a.status == "proposed" and not a.requires_approval)
    ]

    await case_service.transition(
        db, request, case.EXECUTING, actor_user_id=actor_user_id,
        audit_action=case.AUDIT_ACTION_STARTED, detail={"mode": "push", "action_count": len(runnable)},
    )
    await audit_service.record(
        db, org_id=request.org_id, actor_user_id=actor_user_id,
        action=case.AUDIT_EXECUTION_MANIFEST_ISSUED, entity_type=case.AUDIT_ENTITY, entity_id=request.id,
        after={"action_ids": [str(a.id) for a in runnable], "count": len(runnable)},
    )
    return runnable


# ── Execution report: POST /requests/{id}/execution-report ──────────────────────

#: {reported status -> dsr_actions.status}
_ACTION_STATUS_FOR_REPORT = {"done": "executed", "failed": "failed", "skipped": "skipped"}
#: {reported status -> dsr_executions.status}. There is no 'skipped' value in that
#: table's check constraint (migration 0011 predates this feature); 'partial' --
#: otherwise unused -- is the closest existing meaning ("something other than a
#: clean success or a clean failure") and is reused rather than widening that
#: constraint for one new case.
_EXECUTION_STATUS_FOR_REPORT = {"done": "verified", "failed": "failed", "skipped": "partial"}
_VERIFICATION_STATUS_FOR_REPORT = {"done": "passed", "failed": "failed", "skipped": "not_applicable"}

#: An action may only be reported against if it was genuinely part of the
#: manifest (runnable at EXECUTING time) or has already been reported once (a
#: retry) -- never a blocked or rejected action, which Consiva was never handed.
_REPORTABLE_ACTION_STATUSES = frozenset({"approved", "proposed", "executed", "failed", "skipped"})


async def record_execution_report(
    db,
    request: DsrRequest,
    items: list,
    *,
    actor_user_id: uuid.UUID | None = None,
    now: datetime | None = None,
) -> dict:
    """Record what Consiva actually carried out for each manifest action.

    Idempotent per action id, claimed through the SAME dsr_executions ledger a
    connector-driven execution uses (dsr_repository.claim_execution) -- a retried
    report for an action already recorded returns the existing row rather than
    double-counting it. Once every runnable action on the current plan has a
    terminal outcome, the case is moved on exactly as execute_queued_actions
    already does: EXECUTION_VERIFIED (or FAILED if nothing succeeded), then
    RESPONSE_PENDING -- so GET /response, POST /response and POST /complete work
    completely unchanged.
    """
    moment = now or datetime.now(UTC)
    if request.status != case.EXECUTING:
        raise CaseNotReadyError(
            f"case {request.reference} is {request.status!r}; an execution report is only "
            "accepted while the case is executing"
        )
    plan = await dsr_repository.get_current_plan(db, request.id, request.org_id)
    if plan is None:
        raise CaseNotReadyError(f"case {request.reference} has no current action plan")
    actions_by_id = {a.id: a for a in await dsr_repository.list_actions(db, plan.id, request.org_id)}

    recorded = []
    for item in items:
        action = actions_by_id.get(item.action_id)
        if action is None:
            raise DsrNotFoundError(f"action {item.action_id} not found on case {request.reference}")
        if action.status not in _REPORTABLE_ACTION_STATUSES:
            raise CaseNotReadyError(
                f"action {item.action_id} is {action.status!r}; it was not part of the "
                "execution manifest and cannot be reported against"
            )

        execution, is_new = await dsr_repository.claim_execution(
            db, org_id=request.org_id, request_id=request.id, action_id=action.id,
            idempotency_key=f"dsr_push_execution_report:{action.id}",
            executed_by_user_id=actor_user_id,
        )
        if not is_new:
            recorded.append(execution)
            continue

        execution.status = _EXECUTION_STATUS_FOR_REPORT[item.status]
        execution.verification_status = _VERIFICATION_STATUS_FOR_REPORT[item.status]
        execution.rows_affected = item.rows_affected
        execution.error_code = case.ERR_ACTION_FAILED if item.status == "failed" else None
        execution.error_detail = item.error_text
        execution.verified_at = moment if item.status == "done" else None
        execution.started_at = execution.started_at or moment
        execution.completed_at = item.occurred_at or moment
        execution.attempts = 1
        action.status = _ACTION_STATUS_FOR_REPORT[item.status]
        await db.flush()
        recorded.append(execution)

        await audit_service.record(
            db, org_id=request.org_id, actor_user_id=actor_user_id,
            action=case.AUDIT_ACTION_VERIFIED if item.status == "done" else case.AUDIT_FAILED,
            entity_type=case.AUDIT_ENTITY, entity_id=request.id,
            after={
                "action_id": str(action.id), "execution_id": str(execution.id),
                "reported_status": item.status, "rows_affected": item.rows_affected,
                "error_text": item.error_text,
            },
        )

    await audit_service.record(
        db, org_id=request.org_id, actor_user_id=actor_user_id,
        action=case.AUDIT_EXECUTION_REPORT_RECEIVED, entity_type=case.AUDIT_ENTITY, entity_id=request.id,
        after={"reported": [{"action_id": str(i.action_id), "status": i.status} for i in items]},
    )

    pending = [
        a for a in actions_by_id.values()
        if a.status == "approved" or (a.status == "proposed" and not a.requires_approval)
    ]
    closed = False
    if not pending:
        succeeded = sum(1 for a in actions_by_id.values() if a.status == "executed")
        failed = sum(1 for a in actions_by_id.values() if a.status == "failed")
        # "rejected" (a reviewer declined the action before it ever reached the
        # manifest) counts the same as "blocked"/"skipped" here -- all three mean the
        # record was deliberately left unchanged, and excluding "rejected" would let
        # this path close a case as a clean EXECUTION_VERIFIED despite one.
        blocked = sum(
            1 for a in actions_by_id.values() if a.status in ("blocked", "skipped", "rejected")
        )
        if succeeded or failed or blocked:
            closed = True
            plan.status = (
                "partially_executed" if (failed or blocked)
                else ("executed" if succeeded else plan.status)
            )
            await db.flush()
            status, code = status_after_execution(succeeded=succeeded, failed=failed, blocked=blocked)
            detail = {"succeeded": succeeded, "failed": failed, "blocked": blocked, "mode": "push"}
            await case_service.transition(
                db, request, status, actor_user_id=actor_user_id,
                audit_action=(
                    case.AUDIT_ACTION_VERIFIED if status == case.EXECUTION_VERIFIED else case.AUDIT_FAILED
                ),
                error_code=code, error_detail=_execution_detail(succeeded, failed, blocked), detail=detail,
            )
            if status == case.EXECUTION_VERIFIED:
                await case_service.transition(
                    db, request, case.RESPONSE_PENDING, actor_user_id=actor_user_id,
                    error_code=code, error_detail=_execution_detail(succeeded, failed, blocked), detail=detail,
                )

    return {"executions": recorded, "closed": closed, "case": request}
