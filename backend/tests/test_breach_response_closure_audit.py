"""Regression tests for the 2026-10 Breach Response (Agent 4) closure-audit lane.

The headline defect: `closing_status` only ever saw `blocked`/`failed` action
counts. A reviewer's REJECT decision on a containment action was invisible to it --
`close_incident` computed `blocked`/`failed` from the action list but never folded
`rejected` into either bucket, so an incident with one executed action and one
explicitly-rejected action closed as CLOSED (clean) instead of PARTIALLY_COMPLETED.
This is the exact same class of bug Agent 3 (DSR) had and fixed this session in
`dsr_run_service.closing_status` / `routes/dsr.py`'s `close_request`.

While in this territory, three further gaps of the same shape were found and fixed,
all in `response_service.py`:

  * `decide_action` had no guard against a second decision being recorded against an
    action that was already approved or rejected -- the same class of bug DSR's
    `approval_service.decide_action` had (fixed this session with a 409 guard).
  * `_preflight` (shared by attestation and failure-recording) only refused a
    `blocked` or `completed` action -- an action a reviewer explicitly REJECTED, or
    one already recorded `failed`/`skipped`, could still pass preflight and be
    attested as successfully carried out, as long as it did not itself require
    approval.
  * `mark_action_failed` took no preflight at all: an action still `proposed` (never
    approved) could be reported "failed", misrepresenting an attempt nobody was ever
    authorised to make, and an already-resolved action could be silently re-marked.

Reuses the in-memory World/wire harness from test_breach_end_to_end.py, the same way
test_breach_fixes_regression.py does, so no live database is needed.
"""

import pytest

from app.agents.breach.errors import (
    ActionBlockedError,
    ApprovalRequiredError,
    IncidentNotReadyError,
)
from app.agents.breach.schemas import incident as vocab
from app.agents.breach.services import communication_service, incident_service, response_service
from app.api.v1.routes import incidents as routes
from app.core.security import CurrentUser
from app.db.models import IncidentAction
from tests.test_breach_end_to_end import NOW, ORG, USER, World, _assign_id, _DB, open_incident, wire

_USER = CurrentUser(user_id=str(USER), org_id=str(ORG))


def _action(case, **kw) -> IncidentAction:
    return _assign_id(IncidentAction(
        org_id=ORG, incident_id=case.id,
        action_kind=kw.pop("action_kind", vocab.ACT_DISABLE_ACCOUNT),
        execution_mode=kw.pop("execution_mode", vocab.EXECUTION_MODE_TRACKED),
        title=kw.pop("title", "Disable the compromised account"),
        rationale=kw.pop("rationale", "credential known compromised"),
        expected_result=kw.pop("expected_result", "the account can no longer authenticate"),
        risk=kw.pop("risk", "high"),
        requires_approval=kw.pop("requires_approval", True),
        status=kw.pop("status", "proposed"),
        **kw,
    ))


# ── The headline fix: a rejected action is not invisible to closing_status ────────

@pytest.mark.asyncio
async def test_close_incident_reports_partial_completion_when_an_action_was_rejected(
    monkeypatch,
):
    """Before the fix: `close_incident` computed `blocked = sum(status == "blocked")`
    and `failed = sum(status == "failed")`, so a "rejected" action fell into neither
    bucket and was invisible to `closing_status` -- an incident with one executed
    action and one explicitly-rejected action closed CLOSED, stating the plan
    completed cleanly when a reviewer had in fact declined part of it.

    After the fix: `rejected` is folded into `blocked` at the point the counts are
    built (mirroring Agent 3's DSR fix), so the SAME action list now closes
    PARTIALLY_COMPLETED, with an error_code and error_detail a reader checks first.
    """
    world = World()
    wire(monkeypatch, world)
    db = _DB()
    case = await open_incident(
        db, world, title="Suspicious export from the billing service",
        description="A one-off manual data pull nobody can yet explain.",
    )
    await incident_service.transition(db, case, vocab.REVIEW_REQUIRED, actor_user_id=USER, now=NOW)
    await incident_service.transition(db, case, vocab.CLOSURE_REVIEW, actor_user_id=USER, now=NOW)

    # One action genuinely carried out ...
    world.actions.append(_action(
        case, action_kind=vocab.ACT_PRESERVE_LOGS, title="Preserve logs",
        rationale="evidence", expected_result="logs retained", risk="low",
        requires_approval=False, status="completed",
    ))
    # ... and one a reviewer explicitly declined. Not "blocked" by a constraint, not
    # "failed" in an attempt -- a human said no.
    world.actions.append(_action(
        case, action_kind=vocab.ACT_DISABLE_ACCOUNT, title="Disable the account",
        rationale="compromised", expected_result="cannot authenticate", risk="high",
        requires_approval=True, status="rejected",
    ))

    await communication_service.generate_report(db, case, actor_user_id=USER)

    payload = routes.DecisionIn(
        decision=vocab.DECISION_APPROVE,
        reason="Investigation complete; one containment action was rejected as disproportionate.",
    )
    result = await routes.close_incident(case.id, payload, user=_USER, db=db)

    assert result["status"] == vocab.PARTIALLY_COMPLETED, (
        "a rejected containment action must not be invisible to closing_status -- "
        "the incident closed as if the plan was carried out cleanly"
    )
    assert case.error_code == vocab.ERR_ACTION_BLOCKED
    assert "1 action(s) blocked" in case.error_detail


@pytest.mark.asyncio
async def test_close_incident_still_closes_cleanly_when_nothing_was_rejected(monkeypatch):
    """The fix must not become a blanket downgrade: an incident with nothing blocked,
    rejected or failed still closes CLOSED."""
    world = World()
    wire(monkeypatch, world)
    db = _DB()
    case = await open_incident(
        db, world, title="Suspicious export from the billing service",
        description="A one-off manual data pull nobody can yet explain.",
    )
    await incident_service.transition(db, case, vocab.REVIEW_REQUIRED, actor_user_id=USER, now=NOW)
    await incident_service.transition(db, case, vocab.CLOSURE_REVIEW, actor_user_id=USER, now=NOW)

    world.actions.append(_action(
        case, action_kind=vocab.ACT_PRESERVE_LOGS, title="Preserve logs",
        rationale="evidence", expected_result="logs retained", risk="low",
        requires_approval=False, status="completed",
    ))

    await communication_service.generate_report(db, case, actor_user_id=USER)
    payload = routes.DecisionIn(decision=vocab.DECISION_APPROVE, reason="Nothing further to do.")
    result = await routes.close_incident(case.id, payload, user=_USER, db=db)
    assert result["status"] == vocab.CLOSED
    assert case.error_code is None


# ── decide_action: a decision cannot be silently overwritten ──────────────────────

@pytest.mark.asyncio
async def test_decide_action_refuses_to_overwrite_an_already_decided_action(monkeypatch):
    """Same class of bug as DSR's approval_service.decide_action: a stale UI
    re-submit, two reviewers racing on the same action, or a retried request must not
    be able to flip an already-approved (or already-rejected) action to a different
    decision with no conflict and no trace beyond the audit log."""
    world = World()
    wire(monkeypatch, world)
    db = _DB()
    case = await open_incident(
        db, world, title="Unauthorized access to the customer database",
        description="An account with no reason to touch it queried it all night.",
    )
    action = _action(case)
    world.actions.append(action)

    await response_service.decide_action(
        db, case, action.id, reviewer_user_id=USER, decision=vocab.DECISION_APPROVE,
        reason="Proportionate to the evidence.", now=NOW,
    )
    assert action.status == "approved"

    with pytest.raises(IncidentNotReadyError):
        await response_service.decide_action(
            db, case, action.id, reviewer_user_id=USER, decision=vocab.DECISION_REJECT,
            reason="Changed my mind.", now=NOW,
        )
    assert action.status == "approved", (
        "a second, conflicting decision must not silently overwrite the first "
        "reviewer's call"
    )


# ── _preflight: a reviewer's rejection must stop execution, full stop ────────────

@pytest.mark.asyncio
async def test_a_rejected_action_cannot_be_attested_even_without_requiring_approval(
    monkeypatch,
):
    """Before the fix, `_preflight` only refused a "blocked" or "completed" action.
    An auto action (requires_approval=False) that a reviewer explicitly rejected
    skipped the approval-currency check entirely (since it never required one), and
    so could still be attested -- recorded as successfully carried out -- despite the
    reviewer's "no". A rejection has to stop containment as completely as a
    constraint block does, regardless of whether the action needed approval."""
    world = World()
    wire(monkeypatch, world)
    db = _DB()
    case = await open_incident(
        db, world, title="Suspicious export from the billing service",
        description="A one-off manual data pull nobody can yet explain.",
    )
    await incident_service.transition(db, case, vocab.RISK_ASSESSMENT, actor_user_id=USER, now=NOW)
    await incident_service.transition(db, case, vocab.RESPONSE_PENDING, actor_user_id=USER, now=NOW)
    await incident_service.transition(db, case, vocab.APPROVED, actor_user_id=USER, now=NOW)
    await incident_service.transition(db, case, vocab.RESPONDING, actor_user_id=USER, now=NOW)

    action = _action(
        case, action_kind=vocab.ACT_PRESERVE_LOGS, title="Preserve logs",
        rationale="evidence", expected_result="logs retained", risk="low",
        requires_approval=False, status="rejected",
    )
    world.actions.append(action)

    with pytest.raises(ActionBlockedError):
        await response_service.record_tracked_execution(
            db, case, action.id, performed_by="priya@corp",
            attestation="Did it anyway.", actor_user_id=USER, now=NOW,
        )
    assert action.status == "rejected", "a rejected action must not be executed"


# ── mark_action_failed: only a genuinely-attempted action can "fail" ─────────────

@pytest.mark.asyncio
async def test_an_unapproved_action_cannot_be_reported_as_failed(monkeypatch):
    """Before the fix, `mark_action_failed` fetched the action directly with no
    preflight at all: an action still `proposed` -- never approved, nobody
    authorised to touch it -- could be reported "failed", misrepresenting an attempt
    that was never sanctioned as a genuine one."""
    world = World()
    wire(monkeypatch, world)
    db = _DB()
    case = await open_incident(
        db, world, title="Unauthorized access to the customer database",
        description="An account with no reason to touch it queried it all night.",
    )
    await incident_service.transition(db, case, vocab.RISK_ASSESSMENT, actor_user_id=USER, now=NOW)
    await incident_service.transition(db, case, vocab.RESPONSE_PENDING, actor_user_id=USER, now=NOW)
    await incident_service.transition(db, case, vocab.APPROVED, actor_user_id=USER, now=NOW)

    action = _action(case, requires_approval=True, status="proposed")
    world.actions.append(action)

    with pytest.raises(ApprovalRequiredError):
        await response_service.mark_action_failed(
            db, case, action.id, reason="gave up without trying",
            actor_user_id=USER, now=NOW,
        )
    assert action.status == "proposed"


@pytest.mark.asyncio
async def test_an_already_resolved_action_cannot_be_reported_as_failed_again(monkeypatch):
    """A reviewer's rejection is an end state exactly like a completed or blocked
    action -- it cannot afterwards be re-cast as a failed attempt."""
    world = World()
    wire(monkeypatch, world)
    db = _DB()
    case = await open_incident(
        db, world, title="Unauthorized access to the customer database",
        description="An account with no reason to touch it queried it all night.",
    )
    await incident_service.transition(db, case, vocab.RISK_ASSESSMENT, actor_user_id=USER, now=NOW)
    await incident_service.transition(db, case, vocab.RESPONSE_PENDING, actor_user_id=USER, now=NOW)
    await incident_service.transition(db, case, vocab.APPROVED, actor_user_id=USER, now=NOW)

    action = _action(case, requires_approval=True, status="rejected")
    world.actions.append(action)

    with pytest.raises(ActionBlockedError):
        await response_service.mark_action_failed(
            db, case, action.id, reason="trying anyway", actor_user_id=USER, now=NOW,
        )
    assert action.status == "rejected"


@pytest.mark.asyncio
async def test_an_approved_action_can_still_be_reported_as_failed(monkeypatch):
    """The fix must not become a blanket lockdown: a genuinely approved action that
    was attempted and did not work must still be reportable as failed."""
    world = World()
    wire(monkeypatch, world)
    db = _DB()
    case = await open_incident(
        db, world, title="Unauthorized access to the customer database",
        description="An account with no reason to touch it queried it all night.",
    )
    await incident_service.transition(db, case, vocab.RISK_ASSESSMENT, actor_user_id=USER, now=NOW)
    await incident_service.transition(db, case, vocab.RESPONSE_PENDING, actor_user_id=USER, now=NOW)
    await incident_service.transition(db, case, vocab.APPROVED, actor_user_id=USER, now=NOW)

    action = _action(case, requires_approval=True, status="approved")
    world.actions.append(action)

    result = await response_service.mark_action_failed(
        db, case, action.id, reason="no admin access to the identity provider",
        actor_user_id=USER, now=NOW,
    )
    assert result.status == "failed"
    assert "no admin access" in result.blocked_reason
