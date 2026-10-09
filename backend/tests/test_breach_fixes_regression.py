"""Regression tests for the five Agent 4 fixes from the 2026-10 audit.

F-1  connector-mode execution is rejected at creation (not yet implemented anywhere)
F-2  RESPONDING advances itself to VERIFYING once every action is resolved
F-3  the generic /transition endpoint refuses targets that have their own gated route
F-4  building a plan advances the case past RESPONSE_PENDING on its own
F-5  closing an incident reports PARTIALLY_COMPLETED when an action was blocked/failed

F-2 and F-4's primary coverage lives in test_breach_end_to_end.py's rewritten scenario
1 and 3, which drive the full path through the real services. This file adds the
narrower, route-level checks those scenarios don't already cover, reusing the same
in-memory World/wire harness so no live database is needed -- route handlers are
plain `async def`s and are called directly, the same way a request would reach them
after FastAPI resolves Depends(get_current_user) and Depends(get_db).
"""

import pytest

from app.agents.breach.errors import IncidentNotReadyError
from app.agents.breach.schemas import incident as vocab
from app.agents.breach.services import incident_service, response_service
from app.api.v1.routes import incidents as routes
from app.core.security import CurrentUser
from app.db.models import IncidentAction
from tests.test_breach_end_to_end import NOW, ORG, USER, World, _assign_id, _DB, open_incident, wire

_USER = CurrentUser(user_id=str(USER), org_id=str(ORG))


# ── F-3: the generic transition endpoint ──────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize(
    "blocked_target", [vocab.APPROVED, vocab.CLOSED, vocab.PARTIALLY_COMPLETED]
)
async def test_generic_transition_refuses_targets_with_their_own_endpoint(blocked_target):
    """APPROVED has decide_action's readiness check; CLOSED/PARTIALLY_COMPLETED have
    close_incident's report-and-summary requirement. The generic endpoint must not be
    a way around either -- it is rejected before the database is even touched."""
    payload = routes.TransitionIn(to_status=blocked_target)
    with pytest.raises(IncidentNotReadyError):
        # `db` is never used: the guard runs before get_incident_or_raise, so passing
        # something that is not a real session proves the rejection happens first.
        await routes.transition(USER, payload, user=_USER, db=None)


@pytest.mark.asyncio
async def test_generic_transition_still_allows_a_legitimate_target(monkeypatch):
    """The fix must not become a blanket lockdown: every target with no dedicated,
    gated endpoint (most of the graph) has to keep working through this route."""
    world = World()
    wire(monkeypatch, world)
    db = _DB()
    case = await open_incident(
        db, world, title="Suspicious export from the billing service",
        description="A one-off manual data pull nobody can yet explain.",
    )
    assert case.status == vocab.INVESTIGATING

    payload = routes.TransitionIn(to_status=vocab.REVIEW_REQUIRED)
    result = await routes.transition(case.id, payload, user=_USER, db=db)
    assert result["status"] == vocab.REVIEW_REQUIRED


# ── F-1: connector execution mode ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_add_action_refuses_connector_execution_mode(monkeypatch):
    """Nothing anywhere executes a connector-mode action -- accepting one would create
    an action that can be approved and then can never reach completed or failed."""
    world = World()
    wire(monkeypatch, world)
    db = _DB()
    case = await open_incident(
        db, world, title="Suspicious export from the billing service",
        description="A one-off manual data pull nobody can yet explain.",
    )
    with pytest.raises(IncidentNotReadyError):
        await response_service.add_action(
            db, case, action_kind=vocab.ACT_DISABLE_ACCOUNT,
            title="Disable the account", rationale="testing", expected_result="done",
            execution_mode=vocab.EXECUTION_MODE_CONNECTOR, actor_user_id=USER,
        )
    assert not world.actions, "a rejected action must not be persisted"


# ── F-4: planning advances the case on its own ────────────────────────────────────

@pytest.mark.asyncio
async def test_build_plan_route_advances_past_response_pending(monkeypatch):
    """Before this fix, neither the route nor the background analysis job ever moved
    an incident out of RESPONSE_PENDING once a plan existed -- it stalled there."""
    world = World()
    wire(monkeypatch, world)
    db = _DB()
    case = await open_incident(
        db, world, title="Unauthorized access to the customer database",
        description="An account with no reason to touch it queried it all night.",
    )
    await incident_service.transition(db, case, vocab.RISK_ASSESSMENT, actor_user_id=USER, now=NOW)
    await incident_service.transition(db, case, vocab.RESPONSE_PENDING, actor_user_id=USER, now=NOW)

    result = await routes.build_plan(case.id, user=_USER, db=db)
    assert case.status != vocab.RESPONSE_PENDING
    assert result["summary"]["total"] > 0


# ── F-2 (follow-up found during commit-safety review) ─────────────────────────────

@pytest.mark.asyncio
async def test_rejecting_the_last_outstanding_action_also_advances_to_verifying(monkeypatch):
    """An action can be resolved by a reject decision, not just an attestation or a
    failure. If that is the LAST outstanding action while RESPONDING, the incident
    must still move itself to VERIFYING -- not just on the attest/fail paths."""
    world = World()
    wire(monkeypatch, world)
    db = _DB()
    case = await open_incident(
        db, world, title="Suspicious export from the billing service",
        description="A one-off manual data pull nobody can yet explain.",
    )

    gated = _assign_id(IncidentAction(
        org_id=ORG, incident_id=case.id, action_kind=vocab.ACT_DISABLE_ACCOUNT,
        execution_mode=vocab.EXECUTION_MODE_TRACKED, title="Disable the account",
        rationale="compromised", expected_result="cannot authenticate", risk="high",
        requires_approval=True, status="proposed",
    ))
    auto = _assign_id(IncidentAction(
        org_id=ORG, incident_id=case.id, action_kind=vocab.ACT_PRESERVE_LOGS,
        execution_mode=vocab.EXECUTION_MODE_TRACKED, title="Preserve logs",
        rationale="evidence", expected_result="logs retained", risk="low",
        requires_approval=False, status="proposed",
    ))
    world.actions.extend([gated, auto])

    await incident_service.transition(db, case, vocab.RISK_ASSESSMENT, actor_user_id=USER, now=NOW)
    await incident_service.transition(db, case, vocab.RESPONSE_PENDING, actor_user_id=USER, now=NOW)
    await incident_service.transition(db, case, vocab.APPROVAL_REQUIRED, actor_user_id=USER, now=NOW)
    await response_service.decide_action(
        db, case, gated.id, reviewer_user_id=USER, decision=vocab.DECISION_APPROVE,
        reason="Proportionate to the evidence.", now=NOW,
    )
    await incident_service.transition(db, case, vocab.APPROVED, actor_user_id=USER, now=NOW)
    await incident_service.transition(db, case, vocab.RESPONDING, actor_user_id=USER, now=NOW)
    await response_service.record_tracked_execution(
        db, case, gated.id, performed_by="priya@corp",
        attestation="Disabled the account.", actor_user_id=USER, now=NOW,
    )
    assert case.status == vocab.RESPONDING, "one action (preserve_logs) is still outstanding"

    # The reviewer decides the still-proposed auto action is not needed after all --
    # a reject while the case is already RESPONDING, which is legal: decide_action has
    # no case-status gate.
    await response_service.decide_action(
        db, case, auto.id, reviewer_user_id=USER, decision=vocab.DECISION_REJECT,
        reason="Logs already preserved by the disable_account action's own audit trail.",
        now=NOW,
    )
    assert case.status == vocab.VERIFYING


# ── F-5: closing reports what actually happened ───────────────────────────────────

@pytest.mark.asyncio
async def test_close_incident_route_reports_partial_completion(monkeypatch):
    """Closing as CLOSED when an action was blocked would claim something that did
    not happen, on the one field a reader checks first."""
    world = World()
    wire(monkeypatch, world)
    db = _DB()
    case = await open_incident(
        db, world, title="Suspicious export from the billing service",
        description="A one-off manual data pull nobody can yet explain.",
    )
    await incident_service.transition(db, case, vocab.REVIEW_REQUIRED, actor_user_id=USER, now=NOW)
    await incident_service.transition(db, case, vocab.CLOSURE_REVIEW, actor_user_id=USER, now=NOW)

    world.actions.append(_assign_id(IncidentAction(
        org_id=ORG, incident_id=case.id, action_kind=vocab.ACT_PRESERVE_LOGS,
        execution_mode=vocab.EXECUTION_MODE_TRACKED, title="Preserve logs",
        rationale="evidence", expected_result="logs retained", risk="low",
        requires_approval=False, status="blocked",
        blocked_reason="a retention rule blocked it",
    )))

    from app.agents.breach.services import communication_service
    await communication_service.generate_report(db, case, actor_user_id=USER)

    payload = routes.DecisionIn(
        decision=vocab.DECISION_APPROVE,
        reason="Investigation complete; one action could not be carried out.",
    )
    result = await routes.close_incident(case.id, payload, user=_USER, db=db)
    assert result["status"] == vocab.PARTIALLY_COMPLETED
    assert case.error_code == vocab.ERR_ACTION_BLOCKED


@pytest.mark.asyncio
async def test_close_incident_route_closes_cleanly_with_nothing_outstanding(monkeypatch):
    world = World()
    wire(monkeypatch, world)
    db = _DB()
    case = await open_incident(
        db, world, title="Suspicious export from the billing service",
        description="A one-off manual data pull nobody can yet explain.",
    )
    await incident_service.transition(db, case, vocab.REVIEW_REQUIRED, actor_user_id=USER, now=NOW)
    await incident_service.transition(db, case, vocab.CLOSURE_REVIEW, actor_user_id=USER, now=NOW)

    from app.agents.breach.services import communication_service
    await communication_service.generate_report(db, case, actor_user_id=USER)

    payload = routes.DecisionIn(decision=vocab.DECISION_APPROVE, reason="Nothing further to do.")
    result = await routes.close_incident(case.id, payload, user=_USER, db=db)
    assert result["status"] == vocab.CLOSED
    assert case.error_code is None
