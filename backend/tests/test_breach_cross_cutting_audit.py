"""Regression tests for two cross-cutting fixes applied after the 2026-10 parallel
Breach Response (Agent 4) audit lanes finished:

1. A reviewer's free-text reason, a closure summary, and an attestation are all
   human-written text that can carry a pasted credential -- the exact risk this
   agent's own evidence-redaction exists for (`investigation_service.redact_text`).
   Unlike `evidence.detail`, these fields reached the persisted row and the audit
   trail completely unredacted: `incident_service.transition`'s `error_detail`/
   `detail`, `incident_service.set_finding`'s `reason`, `response_service.
   decide_action`'s `reason`, `response_service.mark_action_failed`'s `reason` (on
   `action.blocked_reason`), `response_service.record_tracked_execution`'s
   `attestation`, `communication_service.approve_communication`'s `reason`, and
   `routes/incidents.py`'s `close_incident` closure summary. All seven are now
   redacted before they are persisted or audited.

2. `communication_service.approve_communication`/`mark_communication_sent` raised
   `IncidentNotReadyError` (409) for a missing/cross-incident communication id,
   inconsistent with every equivalent lookup elsewhere in this agent (e.g.
   `response_service.decide_action`/`attest`/`mark_action_failed`, all 404). Now
   `IncidentNotFoundError` (404), matching the rest of the agent.

Reuses the in-memory World/wire harness from test_breach_end_to_end.py, the same
way the lane audit files do, so no live database is needed.
"""

import pytest

from app.agents.breach.errors import IncidentNotFoundError
from app.agents.breach.schemas import incident as vocab
from app.agents.breach.services import communication_service, incident_service, response_service
from tests.test_breach_end_to_end import NOW, ORG, USER, World, _assign_id, _DB, open_incident, wire
from app.db.models import IncidentAction

_CREDENTIAL = "postgres://admin:hunter2pass@db.internal:5432/prod"


def _action(case, **kw) -> IncidentAction:
    return _assign_id(IncidentAction(
        org_id=ORG, incident_id=case.id,
        action_kind=kw.pop("action_kind", vocab.ACT_DISABLE_ACCOUNT),
        execution_mode=kw.pop("execution_mode", vocab.EXECUTION_MODE_TRACKED),
        title=kw.pop("title", "Disable the compromised account"),
        rationale=kw.pop("rationale", "credential known compromised"),
        expected_result=kw.pop("expected_result", "the account can no longer authenticate"),
        risk=kw.pop("risk", "medium"),
        requires_approval=kw.pop("requires_approval", True),
        status=kw.pop("status", "proposed"),
        **kw,
    ))


@pytest.mark.asyncio
async def test_a_credential_pasted_into_a_rejection_reason_is_redacted(monkeypatch):
    world = World()
    wire(monkeypatch, world)
    db = _DB()
    case = await open_incident(db, world, title="t", description="d")
    action = _action(case)
    world.actions.append(action)

    await response_service.decide_action(
        db, case, action.id, reviewer_user_id=USER,
        decision=vocab.DECISION_REJECT, now=NOW,
        reason=f"Not needed -- confirmed via {_CREDENTIAL}",
    )

    last_audit = world.audit[-1]
    assert "hunter2pass" not in last_audit["after"]["reason"]
    assert "[redacted]" in last_audit["after"]["reason"]
    assert world.approvals[-1].reason is not None and "hunter2pass" not in world.approvals[-1].reason


@pytest.mark.asyncio
async def test_a_credential_pasted_into_a_failed_action_reason_is_redacted(monkeypatch):
    world = World()
    wire(monkeypatch, world)
    db = _DB()
    case = await open_incident(db, world, title="t", description="d")
    await incident_service.transition(db, case, vocab.RISK_ASSESSMENT, actor_user_id=USER, now=NOW)
    await incident_service.transition(db, case, vocab.RESPONSE_PENDING, actor_user_id=USER, now=NOW)
    await incident_service.transition(db, case, vocab.APPROVED, actor_user_id=USER, now=NOW)
    action = _action(case, status="proposed", requires_approval=True)
    world.actions.append(action)
    await response_service.decide_action(
        db, case, action.id, reviewer_user_id=USER,
        decision=vocab.DECISION_APPROVE, now=NOW, reason="Proportionate to the evidence.",
    )
    await incident_service.transition(db, case, vocab.RESPONDING, actor_user_id=USER, now=NOW)

    await response_service.mark_action_failed(
        db, case, action.id, reason=f"Tried but {_CREDENTIAL} was already rotated", actor_user_id=USER, now=NOW,
    )

    assert "hunter2pass" not in action.blocked_reason
    assert "[redacted]" in action.blocked_reason


@pytest.mark.asyncio
async def test_a_credential_pasted_into_an_attestation_is_redacted(monkeypatch):
    world = World()
    wire(monkeypatch, world)
    db = _DB()
    case = await open_incident(db, world, title="t", description="d")
    await incident_service.transition(db, case, vocab.RISK_ASSESSMENT, actor_user_id=USER, now=NOW)
    await incident_service.transition(db, case, vocab.RESPONSE_PENDING, actor_user_id=USER, now=NOW)
    await incident_service.transition(db, case, vocab.APPROVED, actor_user_id=USER, now=NOW)
    action = _action(case, status="proposed", requires_approval=True)
    world.actions.append(action)
    await response_service.decide_action(
        db, case, action.id, reviewer_user_id=USER,
        decision=vocab.DECISION_APPROVE, now=NOW, reason="Proportionate to the evidence.",
    )
    await incident_service.transition(db, case, vocab.RESPONDING, actor_user_id=USER, now=NOW)

    execution = await response_service.record_tracked_execution(
        db, case, action.id, performed_by="J. Reviewer",
        attestation=f"Disabled the account; old connection string was {_CREDENTIAL}",
        actor_user_id=USER, now=NOW,
    )

    assert "hunter2pass" not in execution.attestation
    assert "[redacted]" in execution.attestation


@pytest.mark.asyncio
async def test_a_credential_pasted_into_a_closure_summary_is_redacted(monkeypatch):
    world = World()
    wire(monkeypatch, world)
    db = _DB()
    case = await open_incident(db, world, title="t", description="d")
    await incident_service.transition(db, case, vocab.RISK_ASSESSMENT, actor_user_id=USER, now=NOW)
    await incident_service.transition(db, case, vocab.RESPONSE_PENDING, actor_user_id=USER, now=NOW)
    await incident_service.transition(db, case, vocab.REVIEW_REQUIRED, actor_user_id=USER, now=NOW)
    await incident_service.transition(db, case, vocab.CLOSURE_REVIEW, actor_user_id=USER, now=NOW)

    await incident_service.transition(
        db, case, vocab.CLOSED, actor_user_id=USER, now=NOW,
        audit_action=vocab.AUDIT_CLOSED,
        detail={"closure_summary": f"Root cause: {_CREDENTIAL}"},
    )

    assert "hunter2pass" not in world.audit[-1]["after"]["closure_summary"]


@pytest.mark.asyncio
async def test_a_credential_pasted_into_a_communication_rejection_reason_is_redacted(monkeypatch):
    world = World()
    wire(monkeypatch, world)
    db = _DB()
    case = await open_incident(db, world, title="t", description="d")
    comm = await communication_service.draft_communication(
        db, case, audience=vocab.COMM_INTERNAL, subject="Status update", actor_user_id=USER,
    )

    await communication_service.approve_communication(
        db, case, comm.id, reviewer_user_id=USER, decision=vocab.DECISION_REJECT,
        reason=f"Inaccurate -- see {_CREDENTIAL}", now=NOW,
    )

    assert "hunter2pass" not in world.audit[-1]["after"]["reason"]
    assert world.approvals[-1].reason is not None and "hunter2pass" not in world.approvals[-1].reason


@pytest.mark.asyncio
async def test_approving_a_nonexistent_communication_is_404_not_409(monkeypatch):
    import uuid

    world = World()
    wire(monkeypatch, world)
    db = _DB()
    case = await open_incident(db, world, title="t", description="d")

    with pytest.raises(IncidentNotFoundError):
        await communication_service.approve_communication(
            db, case, uuid.uuid4(), reviewer_user_id=USER,
            decision=vocab.DECISION_APPROVE, now=NOW,
        )


@pytest.mark.asyncio
async def test_marking_a_nonexistent_communication_sent_is_404_not_409(monkeypatch):
    import uuid

    world = World()
    wire(monkeypatch, world)
    db = _DB()
    case = await open_incident(db, world, title="t", description="d")

    with pytest.raises(IncidentNotFoundError):
        await communication_service.mark_communication_sent(
            db, case, uuid.uuid4(), actor_user_id=USER, now=NOW,
        )
