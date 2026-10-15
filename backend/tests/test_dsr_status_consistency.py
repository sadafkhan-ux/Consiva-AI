"""DSR status consistency: a case's stored status must say what actually happened.

Each test here pins a defect found by tracing why a portal showed `Resolved` while the
agent showed `Failed` (and the reverse), against the in-memory fakes the rest of the
DSR suite uses -- no database, no real connector, no customer data:

  1. a failure reported by the portal could never be corrected by its later success
  2. a mixed connector + pushed plan closed COMPLETED with an action that never ran
  3. a partial search failure was erased from the case row by the next transition
  4. a retried search job re-queried the customer source on a case already FAILED
  5. a FAILED execution had no retry path at all
"""

import uuid
from types import SimpleNamespace

import pytest

from app.agents.dsr.errors import ActionFailedError, CaseNotReadyError
from app.agents.dsr.schemas import case
from app.agents.dsr.services import approval_service, case_service, lifecycle, search_service
from app.api.v1.routes import dsr as dsr_routes
from app.core.security import CurrentUser
from app.db.repositories import dsr_repository
from app.services import dsr_run_service

from tests.test_dsr_end_to_end import (  # noqa: F401 -- `world` is a pytest fixture
    ORG, USER, FakeConnector, FakeDB, _open_case, _plan, _search, _use_connector,
    _verify, match, world,
)
from tests.test_dsr_execution_closure_audit import _fake_purge, _run_worker_execution_against


async def _approved_case(monkeypatch, n=1, connector=None):
    fc = connector or FakeConnector(matches=[match(record_id=7), match("orders", 11)][:n])
    _use_connector(monkeypatch, fc)
    request = await _open_case("Delete my personal information.")
    await _verify(request)
    await _search(request)
    _, actions = await _plan(request)
    await case_service.transition(FakeDB(), request, case.APPROVAL_REQUIRED)
    for a in actions:
        await approval_service.decide_action(
            FakeDB(), request, a.id, reviewer_user_id=USER, decision="approved", reason="ok",
        )
    await case_service.transition(FakeDB(), request, case.APPROVED)
    return request, actions, fc


def _report(action, status, rows=1):
    return SimpleNamespace(
        action_id=action.id, status=status, rows_affected=rows, occurred_at=None,
        error_text="transient" if status == "failed" else None,
    )


# ── 1. A reported failure can be superseded ──────────────────────────────────────

@pytest.mark.asyncio
async def test_a_later_done_report_supersedes_an_earlier_failed_one(world, monkeypatch):
    request, actions, _ = await _approved_case(monkeypatch, 2)
    for a in actions:
        a.data_source_id = None  # pushed evidence: the portal carries these out
    await dsr_run_service.issue_execution_manifest(FakeDB(), request, actor_user_id=USER)

    await dsr_run_service.record_execution_report(FakeDB(), request, [_report(actions[0], "failed")])
    assert actions[0].status == "failed"
    await dsr_run_service.record_execution_report(FakeDB(), request, [_report(actions[0], "done")])

    assert actions[0].status == "executed", "the portal's retry succeeded; the agent must say so"
    execution = next(e for e in world.executions if e.action_id == actions[0].id)
    assert execution.status == "verified" and execution.attempts == 2
    assert len([e for e in world.executions if e.action_id == actions[0].id]) == 1


@pytest.mark.asyncio
async def test_a_recorded_success_is_never_overwritten_by_a_later_failed_report(world, monkeypatch):
    request, actions, _ = await _approved_case(monkeypatch, 2)
    for a in actions:
        a.data_source_id = None
    await dsr_run_service.issue_execution_manifest(FakeDB(), request, actor_user_id=USER)

    await dsr_run_service.record_execution_report(FakeDB(), request, [_report(actions[0], "done")])
    await dsr_run_service.record_execution_report(FakeDB(), request, [_report(actions[0], "failed")])

    assert actions[0].status == "executed"


# ── 2. Mixed plans cannot close with unexecuted work ─────────────────────────────

@pytest.mark.asyncio
async def test_a_mixed_plan_waits_for_the_pushed_action_before_it_can_close(world, monkeypatch):
    monkeypatch.setattr(dsr_repository, "purge_evidence_pushes", _fake_purge)
    request, actions, _ = await _approved_case(monkeypatch, 2)
    actions[1].data_source_id = None  # one connector action, one pushed action
    _run_worker_execution_against(monkeypatch, FakeDB())

    await dsr_run_service.execute_queued_actions(request.id, request.org_id)

    assert actions[0].status == "executed"
    assert actions[1].status == "approved"
    assert request.status == case.EXECUTING, "the pushed action has not been reported yet"

    user = CurrentUser(user_id=str(USER), org_id=str(ORG))
    with pytest.raises(CaseNotReadyError):
        await dsr_routes.complete_case(request_id=request.id, user=user, db=FakeDB())

    # Once the portal reports, the SAME path every other case takes closes it.
    await dsr_run_service.record_execution_report(FakeDB(), request, [_report(actions[1], "done")])
    assert request.status == case.RESPONSE_PENDING
    assert request.error_code is None


@pytest.mark.asyncio
async def test_complete_refuses_a_case_with_an_approved_action_that_has_no_outcome(world, monkeypatch):
    request, actions, _ = await _approved_case(monkeypatch, 1)
    # Force the inconsistent state the safety net exists for.
    await case_service.transition(FakeDB(), request, case.EXECUTING)
    await case_service.transition(FakeDB(), request, case.EXECUTION_VERIFIED)
    await case_service.transition(FakeDB(), request, case.RESPONSE_PENDING)
    from app.agents.dsr.services import response_service
    await response_service.build_response(FakeDB(), request)

    user = CurrentUser(user_id=str(USER), org_id=str(ORG))
    with pytest.raises(CaseNotReadyError, match="no recorded outcome"):
        await dsr_routes.complete_case(request_id=request.id, user=user, db=FakeDB())
    assert request.status == case.RESPONSE_PENDING


# ── 3. A partial search failure survives planning ────────────────────────────────

@pytest.mark.asyncio
async def test_partial_search_failure_stays_on_the_case_after_planning(world, monkeypatch):
    _use_connector(monkeypatch, FakeConnector(matches=[match()]))
    request = await _open_case("Give me a copy of my data")
    await _verify(request)
    await case_service.transition(FakeDB(), request, case.SEARCHING, actor_user_id=USER)

    summary = search_service.SearchSummary()
    summary.evidence_count, summary.sources_searched, summary.sources_failed = 1, ["crm"], ["billing"]

    async def fake_run_search(db, req, **kw):
        return summary
    monkeypatch.setattr(search_service, "run_search", fake_run_search)
    _run_worker_execution_against(monkeypatch, FakeDB())

    await dsr_run_service.execute_queued_search(request.id, request.org_id)

    assert request.status != case.SEARCHING
    assert request.error_code == case.ERR_SEARCH_FAILED
    assert "could not be searched" in (request.error_detail or "")


@pytest.mark.asyncio
async def test_a_clean_search_carries_no_error_code_through_planning(world, monkeypatch):
    _use_connector(monkeypatch, FakeConnector(matches=[match()]))
    request = await _open_case("Give me a copy of my data")
    await _verify(request)
    await case_service.transition(FakeDB(), request, case.SEARCHING, actor_user_id=USER)
    summary = search_service.SearchSummary()
    summary.evidence_count, summary.sources_searched = 1, ["crm"]

    async def fake_run_search(db, req, **kw):
        return summary
    monkeypatch.setattr(search_service, "run_search", fake_run_search)
    _run_worker_execution_against(monkeypatch, FakeDB())

    await dsr_run_service.execute_queued_search(request.id, request.org_id)

    assert request.error_code is None


# ── 4. A retried search job on a settled case is a no-op ─────────────────────────

@pytest.mark.asyncio
async def test_search_job_on_a_failed_case_does_not_re_query_the_source(world, monkeypatch):
    _use_connector(monkeypatch, FakeConnector(matches=[match()]))
    request = await _open_case("Give me a copy of my data")
    await _verify(request)
    await case_service.transition(FakeDB(), request, case.SEARCHING, actor_user_id=USER)
    await case_service.transition(
        FakeDB(), request, case.FAILED, error_code=case.ERR_SEARCH_FAILED, error_detail="boom",
    )
    _run_worker_execution_against(monkeypatch, FakeDB())
    calls = []

    async def spy(db, req, **kw):
        calls.append(req.id)
        raise AssertionError("the source must not be searched again")
    monkeypatch.setattr(search_service, "run_search", spy)

    await dsr_run_service.execute_queued_search(request.id, request.org_id)  # must not raise

    assert calls == []
    assert request.status == case.FAILED and request.error_code == case.ERR_SEARCH_FAILED


# ── 5. A failed execution can be retried, once, through the normal gates ─────────

@pytest.mark.asyncio
async def test_failed_execution_can_be_reopened_and_succeeds_on_retry(world, monkeypatch):
    fc = FakeConnector(matches=[match()], exec_raises=ActionFailedError("source briefly down"))
    request, actions, fc = await _approved_case(monkeypatch, 1, connector=fc)
    _run_worker_execution_against(monkeypatch, FakeDB())

    await dsr_run_service.execute_queued_actions(request.id, request.org_id)
    assert request.status == case.FAILED and request.error_code == case.ERR_ACTION_FAILED
    assert actions[0].status == "failed"
    assert lifecycle.failure_is_retryable(request.status, request.error_code)
    # /execute still refuses a FAILED case: retry is an explicit, separate step.
    assert not lifecycle.can_transition(case.FAILED, case.EXECUTING)

    await dsr_run_service.reopen_failed_execution(FakeDB(), request, actor_user_id=USER)
    assert request.status == case.APPROVED and actions[0].status == "approved"

    fc._exec_raises = None  # the source is back
    await dsr_run_service.execute_queued_actions(request.id, request.org_id)

    assert actions[0].status == "executed"
    assert request.status == case.RESPONSE_PENDING and request.error_code is None
    mine = [e for e in world.executions if e.action_id == actions[0].id]
    assert len(mine) == 1, "the idempotency ledger must still hold ONE execution per action"
    assert mine[0].status == "verified" and mine[0].attempts == 2


@pytest.mark.asyncio
async def test_retry_with_a_lapsed_approval_goes_back_for_a_fresh_decision(world, monkeypatch):
    fc = FakeConnector(matches=[match()], exec_raises=ActionFailedError("down"))
    request, actions, fc = await _approved_case(monkeypatch, 1, connector=fc)
    _run_worker_execution_against(monkeypatch, FakeDB())
    await dsr_run_service.execute_queued_actions(request.id, request.org_id)

    for approval in world.approvals:
        approval.expires_at = approval.expires_at.replace(year=2020)  # lapsed

    await dsr_run_service.reopen_failed_execution(FakeDB(), request, actor_user_id=USER)

    assert actions[0].status == "proposed", "an expired approval must not authorize a retry"
    assert request.status == case.APPROVAL_REQUIRED


@pytest.mark.asyncio
async def test_only_an_execution_failure_can_be_reopened(world, monkeypatch):
    _use_connector(monkeypatch, FakeConnector(matches=[match()]))
    request = await _open_case("Delete my personal information.")
    await _verify(request)
    await case_service.transition(FakeDB(), request, case.SEARCHING, actor_user_id=USER)
    await case_service.transition(
        FakeDB(), request, case.FAILED, error_code=case.ERR_SOURCE_NOT_AUTHORIZED, error_detail="x",
    )
    with pytest.raises(CaseNotReadyError):
        await dsr_run_service.reopen_failed_execution(FakeDB(), request, actor_user_id=USER)


# ── The customer/portal-facing status vocabulary ─────────────────────────────────

@pytest.mark.parametrize("stored,public", [
    (case.RECEIVED, "PENDING"),
    (case.IDENTITY_PENDING, "VERIFICATION_REQUIRED"),
    (case.SEARCHING, "SEARCHING"),
    (case.SEARCH_COMPLETED, "SEARCHING"),
    (case.REVIEW_REQUIRED, "AWAITING_APPROVAL"),
    (case.APPROVAL_REQUIRED, "AWAITING_APPROVAL"),
    (case.EXECUTING, "EXECUTING"),
    (case.RESPONSE_PENDING, "EXECUTING"),
    (case.COMPLETED, "COMPLETED"),
    (case.PARTIALLY_COMPLETED, "PARTIALLY_COMPLETED"),
    (case.FAILED, "FAILED"),
])
def test_public_status_projection(stored, public):
    assert lifecycle.public_status(stored) == public


def test_every_stored_status_has_a_public_status():
    for status in case.ALL_STATUSES:
        assert lifecycle.public_status(status) in {
            "PENDING", "VERIFICATION_REQUIRED", "SEARCHING", "AWAITING_APPROVAL", "EXECUTING",
            "COMPLETED", "PARTIALLY_COMPLETED", "FAILED", "REJECTED", "ESCALATED",
            "CANCELLED", "EXPIRED",
        }, status


def test_only_failed_with_a_recoverable_code_is_retryable():
    assert lifecycle.failure_is_retryable(case.FAILED, case.ERR_ACTION_FAILED)
    assert lifecycle.failure_is_retryable(case.FAILED, case.ERR_CONNECTOR_TIMEOUT)
    assert not lifecycle.failure_is_retryable(case.FAILED, case.ERR_SOURCE_NOT_AUTHORIZED)
    assert not lifecycle.failure_is_retryable(case.FAILED, case.ERR_IDENTITY_FAILED)
    assert not lifecycle.failure_is_retryable(case.COMPLETED, None)


@pytest.mark.asyncio
async def test_results_say_found_is_not_the_same_as_done_on_a_failed_case(world, monkeypatch):
    _use_connector(monkeypatch, FakeConnector(matches=[match()]))
    request = await _open_case("Delete my personal information.")
    await _verify(request)
    await _search(request)
    await case_service.transition(
        FakeDB(), request, case.FAILED, error_code=case.ERR_ACTION_FAILED, error_detail="x",
    )
    user = CurrentUser(user_id=str(USER), org_id=str(ORG))

    result = await dsr_routes.list_results(request_id=request.id, user=user, db=FakeDB())

    assert result["evidence"], "evidence stays visible"
    assert result["case_status"] == case.FAILED
    assert "no action on them is confirmed" in result["evidence_notice"]
