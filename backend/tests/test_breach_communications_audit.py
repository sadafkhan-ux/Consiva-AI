"""Regression tests for the 2026-10 audit of Agent 4's communications/report/SLA/audit
lane.

CR-1  compose_report (and therefore generate_report/_default_body for internal
      audiences) silently dropped a reviewer-rejected containment action from the
      Response section -- it appeared in neither `completed`, `failed` nor
      `outstanding`, so a report could describe a response as fully resolved while a
      proposed action had in fact been rejected. Same class of omission as the
      `closing_status` gap a sibling lane fixed in response_service.py/routes/
      incidents.py's close_incident; fixed here the same way DSR's response_service
      fixed it for its own `rejected` actions: a generic, factual sentence, never the
      reviewer's private rejection reason.

CR-2  approve_communication only refused to record a second decision once a
      communication had been marked `sent`. An already-`approved` or already-
      `rejected` draft could be decided again -- silently overwriting
      `approved_by_user_id`/`approved_at`, flipping a rejected external draft to
      approved with no trace beyond the audit log, and recording a second
      IncidentApproval row as though it were the first decision. Same class of bug
      DSR's approval_service.decide_action guards against for DsrApproval.

This file reuses the in-memory World/wire harness from test_breach_end_to_end.py so
no live database is needed, the same way test_breach_fixes_regression.py does.
"""

import uuid

import pytest

from app.agents.breach.errors import ApprovalRequiredError, IncidentNotReadyError
from app.agents.breach.schemas import incident as vocab
from app.agents.breach.services import communication_service, response_service
from app.api.v1.routes import incidents as routes
from app.core.security import CurrentUser
from app.db.models import IncidentAction
from tests.test_breach_end_to_end import NOW, ORG, USER, World, _assign_id, _DB, open_incident, wire

_USER = CurrentUser(user_id=str(USER), org_id=str(ORG))


def _proposed_action(case, **overrides):
    defaults = dict(
        org_id=ORG, incident_id=case.id, action_kind=vocab.ACT_PRESERVE_LOGS,
        execution_mode=vocab.EXECUTION_MODE_TRACKED, title="Preserve logs",
        rationale="evidence", expected_result="logs retained", risk="low",
        requires_approval=False, status="proposed",
    )
    defaults.update(overrides)
    return _assign_id(IncidentAction(**defaults))


# ── CR-1: a rejected containment action must not vanish from the report ──────────

@pytest.mark.asyncio
async def test_report_mentions_a_rejected_action_without_leaking_the_reason(monkeypatch):
    world = World()
    wire(monkeypatch, world)
    db = _DB()
    case = await open_incident(
        db, world, title="Suspicious export from the billing service",
        description="A one-off manual data pull nobody can yet explain.",
    )

    action = _proposed_action(case)
    world.actions.append(action)

    private_reason = "Internal only: redundant given disable_account's own audit trail."
    await response_service.decide_action(
        db, case, action.id, reviewer_user_id=USER, decision=vocab.DECISION_REJECT,
        reason=private_reason, now=NOW,
    )
    assert action.status == "rejected"

    report = await communication_service.generate_report(db, case, actor_user_id=USER)

    assert "1 action(s) were reviewed and not carried out." in report.body_text, (
        "a rejected action must be stated in the Response section, not silently "
        "dropped (it appears in none of completed/failed/outstanding otherwise)"
    )
    assert private_reason not in report.body_text, (
        "the reviewer's private rejection reason is for the audit trail, never for "
        "an external/report reader"
    )


@pytest.mark.asyncio
async def test_report_with_no_rejected_actions_says_nothing_about_rejection(monkeypatch):
    """The fix must not fire on incidents with no rejected action."""
    world = World()
    wire(monkeypatch, world)
    db = _DB()
    case = await open_incident(
        db, world, title="Suspicious export from the billing service",
        description="A one-off manual data pull nobody can yet explain.",
    )
    report = await communication_service.generate_report(db, case, actor_user_id=USER)
    assert "reviewed and not carried out" not in report.body_text


# ── CR-2: a communication cannot be decided twice ─────────────────────────────────

@pytest.mark.asyncio
async def test_an_approved_communication_cannot_be_approved_again(monkeypatch):
    world = World()
    wire(monkeypatch, world)
    db = _DB()
    case = await open_incident(
        db, world, title="Customer records exposed publicly",
        description="A storage bucket holding customer records was publicly readable.",
    )
    draft = await communication_service.draft_communication(
        db, case, audience=vocab.COMM_AFFECTED_INDIVIDUAL,
        subject="About a recent security incident", actor_user_id=USER,
    )
    first_reviewer = USER
    await communication_service.approve_communication(
        db, case, draft.id, reviewer_user_id=first_reviewer,
        decision=vocab.DECISION_APPROVE, now=NOW,
    )
    assert draft.status == "approved"
    assert draft.approved_by_user_id == first_reviewer

    second_reviewer = uuid.uuid4()
    with pytest.raises(IncidentNotReadyError):
        await communication_service.approve_communication(
            db, case, draft.id, reviewer_user_id=second_reviewer,
            decision=vocab.DECISION_APPROVE, now=NOW,
        )
    # The first reviewer's decision must survive the rejected second call untouched.
    assert draft.approved_by_user_id == first_reviewer
    assert len(world.approvals) == 1, "a refused second decision must not be recorded"


@pytest.mark.asyncio
async def test_a_rejected_communication_cannot_be_flipped_to_approved(monkeypatch):
    world = World()
    wire(monkeypatch, world)
    db = _DB()
    case = await open_incident(
        db, world, title="Customer records exposed publicly",
        description="A storage bucket holding customer records was publicly readable.",
    )
    draft = await communication_service.draft_communication(
        db, case, audience=vocab.COMM_REGULATOR,
        subject="Notification of a security incident", actor_user_id=USER,
    )
    await communication_service.approve_communication(
        db, case, draft.id, reviewer_user_id=USER,
        decision=vocab.DECISION_REJECT, reason="Not yet ready to notify.", now=NOW,
    )
    assert draft.status == "rejected"

    with pytest.raises(IncidentNotReadyError):
        await communication_service.approve_communication(
            db, case, draft.id, reviewer_user_id=USER,
            decision=vocab.DECISION_APPROVE, now=NOW,
        )
    assert draft.status == "rejected", (
        "a rejected draft must not silently become approved with no trace of the "
        "original rejection"
    )


@pytest.mark.asyncio
async def test_an_external_communication_still_cannot_be_sent_without_approval(monkeypatch):
    """The duplicate-decision guard above must not interfere with the existing,
    already-covered approve-then-send happy path."""
    world = World()
    wire(monkeypatch, world)
    db = _DB()
    case = await open_incident(
        db, world, title="Customer records exposed publicly",
        description="A storage bucket holding customer records was publicly readable.",
    )
    draft = await communication_service.draft_communication(
        db, case, audience=vocab.COMM_CUSTOMER,
        subject="About a recent security incident", actor_user_id=USER,
    )
    with pytest.raises((ApprovalRequiredError, IncidentNotReadyError)):
        await communication_service.mark_communication_sent(
            db, case, draft.id, actor_user_id=USER, now=NOW
        )
    await communication_service.approve_communication(
        db, case, draft.id, reviewer_user_id=USER,
        decision=vocab.DECISION_APPROVE, now=NOW,
    )
    sent = await communication_service.mark_communication_sent(
        db, case, draft.id, actor_user_id=USER, now=NOW
    )
    assert sent.status == "sent"


# ── Audit trail: org-scoping of GET /{incident_id}/audit ──────────────────────────

@pytest.mark.asyncio
async def test_audit_route_is_scoped_to_the_incidents_own_org_and_tenant(monkeypatch):
    """`list_incident_audit` filters by org_id and incident_id both; prove the route
    never returns another tenant's or another incident's audit rows."""
    world = World()
    wire(monkeypatch, world)
    db = _DB()
    case = await open_incident(
        db, world, title="Suspicious export from the billing service",
        description="A one-off manual data pull nobody can yet explain.",
    )
    other_org = uuid.uuid4()
    other_case = await open_incident(
        db, world, title="Unrelated incident in another tenant",
        description="Belongs to a different organisation entirely.",
    )
    other_case.org_id = other_org  # simulate a genuinely different tenant's incident

    from app.db.repositories import incident_repository as repo
    from app.db.models import AuditLog

    audit_rows = [
        AuditLog(
            id=uuid.uuid4(), org_id=ORG, actor_user_id=USER, action="incident.created",
            entity_type=vocab.AUDIT_ENTITY, entity_id=case.id, before=None, after={},
            created_at=NOW,
        ),
        AuditLog(
            id=uuid.uuid4(), org_id=other_org, actor_user_id=USER, action="incident.created",
            entity_type=vocab.AUDIT_ENTITY, entity_id=other_case.id, before=None, after={},
            created_at=NOW,
        ),
    ]

    async def list_incident_audit(db, incident_id, org_id, *, limit=500):
        return [
            r for r in audit_rows
            if r.entity_id == incident_id and r.org_id == org_id
        ]

    monkeypatch.setattr(repo, "list_incident_audit", list_incident_audit)

    result = await routes.get_audit(case.id, user=_USER, db=db)
    assert len(result) == 1
    assert result[0]["action"] == "incident.created"

    # The other tenant's user must not be able to reach this incident at all --
    # get_incident_or_raise (org-scoped) runs before list_incident_audit does.
    other_user = CurrentUser(user_id=str(USER), org_id=str(other_org))
    from app.agents.breach.errors import IncidentNotFoundError
    with pytest.raises(IncidentNotFoundError):
        await routes.get_audit(case.id, user=other_user, db=db)
