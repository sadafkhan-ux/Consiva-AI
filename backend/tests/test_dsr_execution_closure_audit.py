"""Safe execution and closure (prompt §23, §24, §25, §47) -- audit of the lane.

These tests target three places the Safe Execution & Closure audit flagged as the
most likely spots for a real defect:

  1. The postgres connector's write path: a DELETE whose WHERE clause matches no
     row must be reported as a FAILURE, not a trivial "verified by absence" success
     (the absence check is otherwise satisfied whether or not THIS action did
     anything).
  2. Closure gating: a human reviewer's REJECTION of an action is a deliberate
     "this will not be done", exactly like a constraint block or an execution
     failure -- a case must not close COMPLETED (or report a clean, non-partial
     EXECUTION_VERIFIED) while one of its actions sits "rejected".
  3. `/complete`'s own state gate: PARTIALLY_COMPLETED is one of the state
     machine's exception statuses reachable from ANY non-terminal status, so
     `/complete` must not rely on "a response exists" alone to decide a case may
     close -- it must also check the case is actually AT response_pending.

No live Postgres and no live app database: the connector test reuses the fake
asyncpg connection from test_dsr_connector_security.py, and the closure tests reuse
the in-memory `world` harness from test_dsr_end_to_end.py (both already prove the
surrounding machinery; this file only adds the coverage that was missing).
"""

import uuid
from types import SimpleNamespace

import pytest

from app.agents.dsr.errors import CaseNotReadyError, VerificationFailedError
from app.agents.dsr.schemas import case
from app.agents.dsr.services import approval_service, case_service
from app.core.security import CurrentUser
from app.db.repositories import dsr_repository
from app.services import dsr_run_service

from tests.test_dsr_connector_security import FakeConnection, _stub_connect, make_connector
from tests.test_dsr_end_to_end import (
    NOW,
    ORG,
    USER,
    FakeDB,
    _open_case,
    _plan,
    _search,
    _use_connector,
    _verify,
    match,
    world,  # noqa: F401 -- pytest fixture, used by name as a test parameter
)


class _FakeSessionContext:
    """Just enough of an async context manager for
    `dsr_run_service.execute_queued_actions` (a worker entry point that opens its
    OWN session via `async_session_factory()`) to hand back the SAME in-memory
    `FakeDB`/`world` the rest of a test already uses, instead of dialling a real
    database that does not exist in this test process."""

    def __init__(self, db):
        self._db = db

    async def __aenter__(self):
        return self._db

    async def __aexit__(self, *exc_info):
        return False


def _run_worker_execution_against(monkeypatch, db):
    """Point `dsr_run_service.execute_queued_actions` at `db` instead of a real
    session, so it can be exercised the same way the rest of this file drives
    every other service function -- against the in-memory `world`."""
    monkeypatch.setattr(
        dsr_run_service, "async_session_factory", lambda: _FakeSessionContext(db)
    )

    async def no_retention_rules(db, org_id, **kw):
        return []

    # Not part of the `world` fixture's own patch set (test_dsr_end_to_end.py's
    # scenarios that reach execute_queued_actions-equivalent behaviour do so by
    # driving execution_service.execute_action directly rather than through this
    # worker entry point, so this particular repository call was never stubbed).
    monkeypatch.setattr(dsr_repository, "list_retention_rules", no_retention_rules)


# ── 1. A DELETE that matches zero rows must fail, not pass by vacuous absence ────

@pytest.mark.asyncio
async def test_delete_matching_zero_rows_is_a_failure_not_a_trivial_success(monkeypatch):
    """A stale or wrong record_reference produces a WHERE clause that matches
    nothing. The naive version of this check ("is the row gone afterwards?") is
    trivially true when the row was never there, which would report success for an
    action that did not do anything. `rows_affected == 0` must be its own failure.
    """
    conn = FakeConnection(rows=[], execute_status="DELETE 0", fetchval_result=0)
    connector = make_connector(
        allow_execution=True, write_credential_ref="W",
        write_user="dsr_write", write_password="w-secret",
    )
    monkeypatch.setattr(connector, "_connect", _stub_connect(conn))
    with pytest.raises(VerificationFailedError):
        await connector.execute_action(
            table_name="customers", record_reference={"id": 999},
            operation=case.OP_DELETE_RECORD, payload={},
        )


@pytest.mark.asyncio
async def test_delete_matching_rows_and_confirmed_absent_still_succeeds(monkeypatch):
    """Sanity check alongside the above: a genuine delete (rows_affected > 0,
    confirmed absent on read-back) must still report success. The fix must not
    turn every delete into a failure."""
    conn = FakeConnection(rows=[], execute_status="DELETE 1", fetchval_result=0)
    connector = make_connector(
        allow_execution=True, write_credential_ref="W",
        write_user="dsr_write", write_password="w-secret",
    )
    monkeypatch.setattr(connector, "_connect", _stub_connect(conn))
    outcome = await connector.execute_action(
        table_name="customers", record_reference={"id": 1},
        operation=case.OP_DELETE_RECORD, payload={},
    )
    assert outcome.verified is True
    assert outcome.rows_affected == 1


# ── 2. Closure gating must not be blind to a reviewer's rejection ───────────────

@pytest.mark.asyncio
async def test_worker_execution_flags_partial_when_one_action_was_rejected(world, monkeypatch):
    """Two actions on the same plan: one approved and executed successfully, one
    explicitly REJECTED by a reviewer (never attempted). Before the fix,
    `execute_queued_actions` only counted action.status == "blocked" towards the
    partial outcome, so this case would reach EXECUTION_VERIFIED with no error code
    at all -- indistinguishable from a case where everything the requester asked
    for actually happened.
    """
    from tests.test_dsr_end_to_end import FakeConnector

    fc = FakeConnector(matches=[match(record_id=7), match("orders", 11)])
    _use_connector(monkeypatch, fc)
    request = await _open_case("Delete my personal information.")
    await _verify(request)
    await _search(request)
    _, actions = await _plan(request)
    assert len(actions) == 2

    await case_service.transition(FakeDB(), request, case.APPROVAL_REQUIRED)
    await approval_service.decide_action(
        FakeDB(), request, actions[0].id, reviewer_user_id=USER,
        decision="approved", reason="confirmed",
    )
    await approval_service.decide_action(
        FakeDB(), request, actions[1].id, reviewer_user_id=USER,
        decision="rejected", reason="legal hold applies to this record",
    )
    assert actions[1].status == "rejected"
    await case_service.transition(FakeDB(), request, case.APPROVED)

    _run_worker_execution_against(monkeypatch, FakeDB())
    await dsr_run_service.execute_queued_actions(request.id, request.org_id)

    assert actions[0].status == "executed"
    assert actions[1].status == "rejected", "a rejected action must never be executed"
    # execute_queued_actions folds EXECUTION_VERIFIED straight on to RESPONSE_PENDING
    # in the same call once nothing failed outright (see its own source) -- the
    # ERR_ACTION_PARTIAL code it carried through that fold-in is what proves this
    # was NOT a clean success.
    assert request.status == case.RESPONSE_PENDING
    # The whole point of the fix: a rejected action must surface as a partial
    # outcome, not a clean, code-less success.
    assert request.error_code == case.ERR_ACTION_PARTIAL


@pytest.mark.asyncio
async def test_complete_route_closes_partially_completed_when_an_action_was_rejected(
    world, monkeypatch,
):
    """End-to-end through the actual `/complete` route handler: a case with one
    executed action and one rejected action must close PARTIALLY_COMPLETED, never
    COMPLETED. Before the fix, `complete_case` only counted "blocked"/"failed"
    actions, so this exact case closed as a plain COMPLETED -- telling the
    requester everything they asked for was done when a reviewer had in fact
    declined one of their requests.
    """
    from tests.test_dsr_end_to_end import FakeConnector
    from app.agents.dsr.services import response_service
    from app.api.v1.routes import dsr as dsr_routes

    monkeypatch.setattr(dsr_repository, "purge_evidence_pushes", _fake_purge)

    fc = FakeConnector(matches=[match(record_id=7), match("orders", 11)])
    _use_connector(monkeypatch, fc)
    request = await _open_case("Delete my personal information.")
    await _verify(request)
    await _search(request)
    _, actions = await _plan(request)
    assert len(actions) == 2

    await case_service.transition(FakeDB(), request, case.APPROVAL_REQUIRED)
    await approval_service.decide_action(
        FakeDB(), request, actions[0].id, reviewer_user_id=USER,
        decision="approved", reason="confirmed",
    )
    await approval_service.decide_action(
        FakeDB(), request, actions[1].id, reviewer_user_id=USER,
        decision="rejected", reason="legal hold applies to this record",
    )
    await case_service.transition(FakeDB(), request, case.APPROVED)
    _run_worker_execution_against(monkeypatch, FakeDB())
    await dsr_run_service.execute_queued_actions(request.id, request.org_id)
    assert request.status == case.RESPONSE_PENDING

    await response_service.build_response(FakeDB(), request)

    user = CurrentUser(user_id=str(USER), org_id=str(ORG))
    result = await dsr_routes.complete_case(request_id=request.id, user=user, db=FakeDB())

    assert result["status"] == case.PARTIALLY_COMPLETED, (
        "a case with a rejected action closed as COMPLETED -- the requester would "
        "be told everything they asked for was done"
    )
    assert request.status == case.PARTIALLY_COMPLETED


async def _fake_purge(db, request_id, org_id):
    return 0


# ── 3. `/complete` must gate on the case's CURRENT status, not just "a response
#       exists" -- PARTIALLY_COMPLETED is reachable from any non-terminal status. ──

@pytest.mark.asyncio
async def test_complete_refuses_a_case_that_is_not_at_response_pending(world, monkeypatch):
    """A response generated earlier (e.g. before a re-plan pulled the case back
    into an earlier status) must not let `/complete` close a case that is not
    actually at RESPONSE_PENDING. Before the fix, `complete_case` checked only
    "does a response row exist" -- and because PARTIALLY_COMPLETED is one of the
    state machine's always-reachable exception statuses, a case with any
    blocked/rejected/failed action anywhere on its plan could be closed
    PARTIALLY_COMPLETED from ANY non-terminal status, including one still
    EXECUTING, using a stale response left over from an earlier round.
    """
    from tests.test_dsr_end_to_end import FakeConnector
    from app.agents.dsr.services import response_service
    from app.api.v1.routes import dsr as dsr_routes

    monkeypatch.setattr(dsr_repository, "purge_evidence_pushes", _fake_purge)

    fc = FakeConnector(matches=[match()])
    _use_connector(monkeypatch, fc)
    request = await _open_case("Delete my personal information.")
    await _verify(request)
    await _search(request)
    _, actions = await _plan(request)

    await case_service.transition(FakeDB(), request, case.APPROVAL_REQUIRED)
    await approval_service.decide_action(
        FakeDB(), request, actions[0].id, reviewer_user_id=USER,
        decision="approved", reason="confirmed",
    )
    await case_service.transition(FakeDB(), request, case.APPROVED)
    _run_worker_execution_against(monkeypatch, FakeDB())
    await dsr_run_service.execute_queued_actions(request.id, request.org_id)
    assert request.status == case.RESPONSE_PENDING

    # A response now exists for this (clean) round.
    await response_service.build_response(FakeDB(), request)

    # Something pulls the case back into an earlier, non-terminal status -- a
    # re-plan is the realistic trigger; forced here directly to isolate the gate
    # under test rather than re-deriving a full re-plan journey.
    request.status = case.EXECUTING

    user = CurrentUser(user_id=str(USER), org_id=str(ORG))
    with pytest.raises(CaseNotReadyError):
        await dsr_routes.complete_case(request_id=request.id, user=user, db=FakeDB())
    assert request.status == case.EXECUTING, "a refused completion must not touch case state"


# ── 4. Idempotent retries at the execution level, proven at the route layer ─────

@pytest.mark.asyncio
async def test_reposting_execute_on_a_push_mode_case_is_refused_at_the_route(world, monkeypatch):
    """Push-evidence mode (every runnable action has no registered connector
    source, i.e. `data_source_id is None`) transitions APPROVED -> EXECUTING
    synchronously inside the first `/execute` call, because there is no connector
    job to queue -- see dsr_run_service.issue_execution_manifest. A second call
    must therefore land on a case that is no longer APPROVED and be refused by the
    route's own status guard, not by some deeper, implicit idempotency the caller
    has to trust is there.

    Built directly on a planned action with `data_source_id` forced to None
    (rather than going through the full POST /evidence push-ingestion path, which
    is someone else's surface) -- this is exactly what `execute()`'s own branch
    condition (`all(a.data_source_id is None for a in runnable)`) tests for.
    """
    from tests.test_dsr_end_to_end import FakeConnector
    from app.api.v1.routes import dsr as dsr_routes

    _use_connector(monkeypatch, FakeConnector(matches=[match()]))
    request = await _open_case("Delete my personal information.")
    await _verify(request)
    await _search(request)
    _, actions = await _plan(request)
    actions[0].data_source_id = None  # simulate a pushed-evidence action

    await case_service.transition(FakeDB(), request, case.APPROVAL_REQUIRED)
    await approval_service.decide_action(
        FakeDB(), request, actions[0].id, reviewer_user_id=USER,
        decision="approved", reason="confirmed",
    )
    await case_service.transition(FakeDB(), request, case.APPROVED)

    user = CurrentUser(user_id=str(USER), org_id=str(ORG))
    first = await dsr_routes.execute(request_id=request.id, user=user, db=FakeDB())
    assert first["mode"] == "push"
    assert request.status == case.EXECUTING

    with pytest.raises(CaseNotReadyError):
        await dsr_routes.execute(request_id=request.id, user=user, db=FakeDB())


@pytest.mark.asyncio
async def test_two_queued_connector_executions_of_the_same_action_still_run_once(
    world, monkeypatch,
):
    """The connector/job-queue path does not itself transition the case out of
    APPROVED when `/execute` is called -- that happens later, when a worker picks
    up the queued job (see app.services.dsr_run_service.execute_queued_actions).
    So a fast duplicate POST /execute, racing ahead of the worker, is not refused
    by the route's own `status != APPROVED` guard the way the push-mode path is,
    and two `dsr_execute` jobs can end up queued for the same case.

    This is a real, confirmed looseness in the route (reported in the lane audit
    rather than changed -- the route's enqueue-then-let-the-worker-transition
    design is intentional and not worth restructuring for this). Two things make
    it safe rather than a defect in its own right:

      * the actual write is gated by `execution_service`'s idempotency-key claim
        (proven directly by test_scenario_8 in test_dsr_end_to_end.py) --
        proven again here by driving it through two separately-queued worker
        runs, the scenario a route-level retry would actually create; and
      * `execute_queued_actions` itself now (a fix made alongside this test) no
        -ops on a job that arrives after the case has already moved past
        APPROVED/EXECUTING, instead of crashing with InvalidCaseTransitionError
        trying to re-transition a case already at RESPONSE_PENDING -- which is
        exactly what the SECOND queued job used to do before this fix.
    """
    from tests.test_dsr_end_to_end import FakeConnector
    from app.api.v1.routes import dsr as dsr_routes

    fc = FakeConnector(matches=[match()])
    _use_connector(monkeypatch, fc)

    async def fake_enqueue(db, *, org_id, job_type, payload):
        return SimpleNamespace(id=uuid.uuid4())

    monkeypatch.setattr(dsr_routes.queue, "enqueue", fake_enqueue)

    request = await _open_case("Delete my personal information.")
    await _verify(request)
    await _search(request)
    _, actions = await _plan(request)

    await case_service.transition(FakeDB(), request, case.APPROVAL_REQUIRED)
    await approval_service.decide_action(
        FakeDB(), request, actions[0].id, reviewer_user_id=USER,
        decision="approved", reason="confirmed",
    )
    await case_service.transition(FakeDB(), request, case.APPROVED)

    user = CurrentUser(user_id=str(USER), org_id=str(ORG))
    # Two back-to-back calls, before any worker has run: the route does not
    # itself refuse the second one, because `request.status` is still APPROVED.
    first = await dsr_routes.execute(request_id=request.id, user=user, db=FakeDB())
    second = await dsr_routes.execute(request_id=request.id, user=user, db=FakeDB())
    assert first["mode"] == second["mode"] == "connector"
    assert request.status == case.APPROVED, (
        "the connector path does not transition the case until a worker runs"
    )

    # Both queued jobs are now processed -- exactly what would happen if two
    # `dsr_execute` jobs sit in the queue for the same case.
    _run_worker_execution_against(monkeypatch, FakeDB())
    await dsr_run_service.execute_queued_actions(request.id, request.org_id)
    await dsr_run_service.execute_queued_actions(request.id, request.org_id)

    assert fc.executions == 1, "the deletion was performed more than once"
    assert len(world.executions) == 1
    assert request.status in (case.EXECUTION_VERIFIED, case.RESPONSE_PENDING)


# ── Cross-lane finding (search/evidence lane): error_code and error_detail must
#    agree about a partial search failure, not just error_code alone. ────────────

def test_search_detail_names_the_partial_failure_when_something_was_still_found():
    """A case with evidence from one source and a failure in another must not
    pair `error_code=SEARCH_FAILED` (SearchSummary.outcome_code already returns
    this) with `error_detail=None` -- that pairing told a reviewer reading only
    the human-readable detail that nothing was wrong, while the machine-readable
    code said otherwise.
    """
    summary = SimpleNamespace(
        ambiguous=False, found_anything=True,
        sources_searched=["crm"], sources_failed=["billing"],
    )
    detail = dsr_run_service._search_detail(summary)
    assert detail is not None
    assert "billing" not in detail or "1 source(s) could not be searched" not in detail  # sanity: no stray names
    assert "1 source(s) searched successfully" in detail
    assert "1 source(s) could not be searched" in detail


def test_search_detail_is_none_for_a_genuinely_clean_search():
    summary = SimpleNamespace(
        ambiguous=False, found_anything=True, sources_searched=["crm"], sources_failed=[],
    )
    assert dsr_run_service._search_detail(summary) is None


# ── 5. Already verified correct; recorded here so the audit is not silent about it ─
#
# * Response honesty for a NO_MATCH access request ("we did not find any record...
#   No data was changed.") is already proven by
#   test_dsr_end_to_end.test_scenario_4_no_match_is_explicit_and_executes_nothing.
# * The apply+verify write is already wrapped in `async with conn.transaction()`
#   in app/agents/dsr/connectors/postgres.py's execute_action, and a verification
#   failure raises INSIDE that block, so asyncpg issues a real ROLLBACK rather
#   than leaving a half-applied write -- there is no autocommit mode in play to be
#   dishonest about.
# * `assert_executable`/`assert_erasable` (authorization.py) and
#   `identity_service.assert_identity_satisfied` are invoked from
#   execution_service.execute_action on every call, immediately before the
#   connector is built and before the write -- not reused from plan/approval time.


# ── Cross-lane finding: POST /response creates a resource, must answer 201 ──────

def test_post_response_route_returns_201_like_every_other_resource_creating_post():
    """`build_response` writes a brand new `dsr_responses` row on every call (a new
    version each time, never an update-in-place) -- the same "this POST creates a
    resource" shape as POST /requests and POST /evidence, both of which already
    answer 201. This route answered the default 200 instead, which is inconsistent
    with its own sibling routes in this same file."""
    from app.api.v1.routes import dsr as dsr_routes

    matches = [
        r for r in dsr_routes.router.routes
        if r.path == "/api/v1/dsr/requests/{request_id}/response" and "POST" in r.methods
    ]
    assert len(matches) == 1
    assert matches[0].status_code == 201
