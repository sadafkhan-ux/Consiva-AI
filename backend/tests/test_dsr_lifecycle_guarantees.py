"""Lifecycle guarantees stated as properties, end to end through the real services.

Each test names one promise the DSR agent makes to a customer and holds it against the same
in-memory world the rest of the suite uses. Synthetic data only; no real connector, database
or customer action.
"""

import uuid
from types import SimpleNamespace

import pytest

from app.agents.dsr.errors import ActionFailedError, ApprovalRequiredError, CaseNotReadyError
from app.agents.dsr.schemas import case
from app.agents.dsr.services import approval_service, case_service, execution_service, lifecycle, search_service
from app.api.v1.routes import dsr as dsr_routes
from app.core.security import CurrentUser
from app.db.repositories import dsr_repository
from app.services import dsr_run_service

from tests.test_dsr_end_to_end import (  # noqa: F401 -- `world` is a pytest fixture
    ORG, USER, FakeConnector, FakeDB, _open_case, _plan, _search, _use_connector, _verify,
    match, world,
)
from tests.test_dsr_execution_closure_audit import _run_worker_execution_against

ADMIN = CurrentUser(user_id=str(USER), org_id=str(ORG), role="admin")


async def _enqueue_ok(monkeypatch):
    async def fake(db, *, org_id, job_type, payload):
        return SimpleNamespace(id=uuid.uuid4())
    monkeypatch.setattr(dsr_routes.queue, "enqueue", fake)


@pytest.mark.asyncio
async def test_a_failed_search_can_be_retried_without_duplicating_evidence(world, monkeypatch):
    _use_connector(monkeypatch, FakeConnector(matches=[match(record_id=7)]))
    await _enqueue_ok(monkeypatch)
    request = await _open_case("Delete my personal information.")
    await _verify(request)
    await case_service.transition(FakeDB(), request, case.SEARCHING, actor_user_id=USER)
    await search_service.run_search(FakeDB(), request)
    assert len(world.evidence) == 1
    await case_service.transition(
        FakeDB(), request, case.FAILED, error_code=case.ERR_SEARCH_FAILED, error_detail="a source was down",
    )
    assert lifecycle.failure_is_retryable(request.status, request.error_code)

    result = await dsr_routes.start_search(request_id=request.id, user=ADMIN, db=FakeDB())
    assert request.status == case.SEARCHING and result["job_id"] is not None
    await search_service.run_search(FakeDB(), request)

    assert len(world.evidence) == 1, "retrying the search must not plan the same record twice"


@pytest.mark.asyncio
async def test_a_case_that_lost_its_source_waits_for_setup_and_recovers_when_one_is_added(world, monkeypatch):
    _use_connector(monkeypatch, FakeConnector(matches=[match()]))
    await _enqueue_ok(monkeypatch)
    request = await _open_case("Delete my personal information.")
    await _verify(request)
    await case_service.transition(FakeDB(), request, case.SEARCHING, actor_user_id=USER)

    real = dsr_repository.list_source_authorizations

    async def none(db, org_id):
        return []
    monkeypatch.setattr(dsr_repository, "list_source_authorizations", none)
    _run_worker_execution_against(monkeypatch, FakeDB())
    await dsr_run_service.execute_queued_search(request.id, request.org_id)

    assert request.status == case.FAILED and request.error_code == case.ERR_SOURCE_NOT_AUTHORIZED
    assert lifecycle.public_status(request.status, request.error_code) == "WAITING_FOR_SETUP"
    message, step = lifecycle.plain_status(request.status, request.error_code)
    assert "no data source is connected" in message and "DSR Settings" in step

    monkeypatch.setattr(dsr_repository, "list_source_authorizations", real)
    await dsr_routes.start_search(request_id=request.id, user=ADMIN, db=FakeDB())
    assert request.status == case.SEARCHING


@pytest.mark.asyncio
async def test_nothing_executes_without_a_current_human_approval(world, monkeypatch):
    fc = FakeConnector(matches=[match()])
    _use_connector(monkeypatch, fc)
    request = await _open_case("Delete my personal information.")
    await _verify(request)
    await _search(request)
    _, actions = await _plan(request)
    assert actions[0].requires_approval
    await case_service.transition(FakeDB(), request, case.APPROVAL_REQUIRED)
    await case_service.transition(FakeDB(), request, case.APPROVED)  # approved by the wrong route

    with pytest.raises(ApprovalRequiredError):
        await execution_service.execute_action(FakeDB(), request, actions[0].id)
    assert fc.executions == 0, "the connector must not be touched without an approval"

    # And the route itself refuses a case that is not APPROVED.
    other = await _open_case("Delete my personal information.")
    with pytest.raises(CaseNotReadyError):
        await dsr_routes.execute(request_id=other.id, user=ADMIN, db=FakeDB())


@pytest.mark.asyncio
async def test_a_retry_runs_each_failed_action_exactly_once_more(world, monkeypatch):
    fc = FakeConnector(matches=[match(record_id=7), match("orders", 11)], exec_raises=ActionFailedError("down"))
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
    _run_worker_execution_against(monkeypatch, FakeDB())

    await dsr_run_service.execute_queued_actions(request.id, request.org_id)
    assert request.status == case.FAILED and fc.executions == 2

    await dsr_run_service.reopen_failed_execution(FakeDB(), request, actor_user_id=USER)
    fc._exec_raises = None
    await dsr_run_service.execute_queued_actions(request.id, request.org_id)
    assert fc.executions == 4, "two failed actions, retried once each -- no more, no fewer"
    assert request.status == case.RESPONSE_PENDING

    # A third pass is a no-op: the case has moved on and the ledger holds one row per action.
    await dsr_run_service.execute_queued_actions(request.id, request.org_id)
    assert fc.executions == 4
    assert len(world.executions) == 2


@pytest.mark.asyncio
async def test_a_partially_failed_execution_closes_partial_and_is_not_silently_retried(world, monkeypatch):
    """One action verifies, one fails: the case must say so, and a closed case does not run again."""
    from app.agents.dsr.services import response_service

    class _Selective(FakeConnector):
        async def execute_action(self, **kw):
            self.executions += 1
            if kw["table_name"] == "orders":
                raise ActionFailedError("orders is read-only")
            return self._exec_outcome

    fc = _Selective(matches=[match(record_id=7), match("orders", 11)])
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
    _run_worker_execution_against(monkeypatch, FakeDB())
    monkeypatch.setattr(dsr_repository, "purge_evidence_pushes", lambda *a, **k: _zero())

    await dsr_run_service.execute_queued_actions(request.id, request.org_id)
    assert request.status == case.RESPONSE_PENDING and request.error_code == case.ERR_ACTION_PARTIAL

    await response_service.build_response(FakeDB(), request)
    result = await dsr_routes.complete_case(request_id=request.id, user=ADMIN, db=FakeDB())

    assert result["status"] == case.PARTIALLY_COMPLETED
    assert result["public_status"] == "PARTIALLY_COMPLETED"
    out = await dsr_routes.get_outcome(request_id=request.id, user=ADMIN, db=FakeDB())
    assert out["execution"]["executed_verified"] == 1 and out["execution"]["failed"] == 1
    assert out["portal_may_show_resolved"] is False
    before = fc.executions
    await dsr_run_service.execute_queued_actions(request.id, request.org_id)
    assert fc.executions == before, "a closed case must not execute anything again"


async def _zero():
    return 0


@pytest.mark.asyncio
async def test_every_case_response_carries_a_status_a_message_and_a_next_step(world, monkeypatch):
    _use_connector(monkeypatch, FakeConnector(matches=[match()]))
    request = await _open_case("Delete my personal information.")

    shown = await dsr_routes.get_request(request_id=request.id, user=ADMIN, db=FakeDB())

    assert shown["public_status"] == "PENDING"
    assert shown["status_message"] and shown["next_step"]
    async def only_this(db, org_id, **kw):
        return [request]
    monkeypatch.setattr(dsr_repository, "list_requests", only_this)
    listed = await dsr_routes.list_requests(status_filter=None, limit=10, user=ADMIN, db=FakeDB())
    assert all(c["public_status"] and c["status_message"] for c in listed)
