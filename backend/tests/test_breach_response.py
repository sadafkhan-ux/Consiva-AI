"""Response planning, approval and controlled execution (§21-§25, §50).

The theme: Consiva cannot disable an account, and the system says so rather than
implying otherwise. An attestation is recorded as somebody's word; only a connector
read-back counts as verification.
"""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.agents.breach.errors import (
    ActionBlockedError,
    ApprovalRequiredError,
    IncidentNotReadyError,
)
from app.agents.breach.schemas import incident as vocab
from app.agents.breach.services import response_service as rs
from app.db.models import IncidentAction, IncidentCase, IncidentExecution

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
ORG = uuid.uuid4()
USER = uuid.uuid4()


def make_case(status=vocab.APPROVED, incident_type=vocab.TYPE_CREDENTIAL_COMPROMISE):
    return IncidentCase(
        id=uuid.uuid4(), org_id=ORG, reference="INC-RESP01", title="t",
        description="d", source=vocab.SOURCE_SIEM, detected_at=NOW,
        status=status, incident_type=incident_type,
    )


def make_action(case, **kw):
    return IncidentAction(
        id=kw.get("id", uuid.uuid4()), org_id=ORG, incident_id=case.id,
        action_kind=kw.get("action_kind", vocab.ACT_DISABLE_ACCOUNT),
        execution_mode=kw.get("execution_mode", vocab.EXECUTION_MODE_TRACKED),
        title="Disable the compromised account", rationale="r", expected_result="e",
        target=kw.get("target", "alice@corp"),
        risk=kw.get("risk", "high"),
        requires_approval=kw.get("requires_approval", True),
        status=kw.get("status", "approved"),
        blocked_reason=kw.get("blocked_reason"),
    )


def make_approval(decision=vocab.DECISION_APPROVE, expires_at=NOW + timedelta(hours=1)):
    return SimpleNamespace(decision=decision, expires_at=expires_at)


class _DB:
    async def flush(self):
        return None


# ── Planning proposes; it does not act ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_plan_contains_only_proposals(monkeypatch):
    case = make_case()
    written = _wire_planning(monkeypatch)
    actions = await rs.build_response_plan(_DB(), case)
    assert actions
    assert all(a.status == "proposed" for a in actions)
    # Nothing in the playbook is something Consiva can perform itself.
    assert all(a.execution_mode == vocab.EXECUTION_MODE_TRACKED for a in actions)
    assert "recommendations only" in written["audit"]["after"]["note"]


@pytest.mark.asyncio
async def test_each_incident_type_gets_a_relevant_playbook(monkeypatch):
    _wire_planning(monkeypatch)
    for kind, expected in [
        (vocab.TYPE_CREDENTIAL_COMPROMISE, vocab.ACT_DISABLE_ACCOUNT),
        (vocab.TYPE_MISCONFIGURATION, vocab.ACT_PATCH_MISCONFIGURATION),
        (vocab.TYPE_MALWARE, vocab.ACT_ISOLATE_SERVICE),
        (vocab.TYPE_DATA_LEAKAGE, vocab.ACT_REVIEW_NOTIFICATION_DUTY),
    ]:
        actions = await rs.build_response_plan(_DB(), make_case(incident_type=kind))
        assert expected in {a.action_kind for a in actions}, f"{kind} missing {expected}"


@pytest.mark.asyncio
async def test_every_incident_is_told_to_preserve_evidence_or_brief_someone(monkeypatch):
    _wire_planning(monkeypatch)
    for kind in vocab.CLASSIFIABLE_TYPES:
        actions = await rs.build_response_plan(_DB(), make_case(incident_type=kind))
        kinds = {a.action_kind for a in actions}
        assert kinds, f"{kind} produced no plan at all"
        assert vocab.ACT_NOTIFY_INTERNAL in kinds


@pytest.mark.asyncio
async def test_replanning_does_not_duplicate_decided_actions(monkeypatch):
    """An incident is re-planned as understanding changes; losing a reviewer's
    decisions each time would make the plan useless."""
    case = make_case()
    existing = [SimpleNamespace(action_kind=vocab.ACT_DISABLE_ACCOUNT,
                                title="Disable the compromised account")]
    _wire_planning(monkeypatch, existing=existing)
    actions = await rs.build_response_plan(_DB(), case)
    assert not any(
        a.action_kind == vocab.ACT_DISABLE_ACCOUNT
        and a.title == "Disable the compromised account"
        for a in actions
    )


@pytest.mark.asyncio
async def test_high_impact_actions_require_approval(monkeypatch):
    _wire_planning(monkeypatch)
    actions = await rs.build_response_plan(_DB(), make_case())
    for action in actions:
        if action.action_kind in vocab.HIGH_IMPACT_ACTIONS:
            assert action.requires_approval, f"{action.action_kind} skipped the gate"


@pytest.mark.asyncio
async def test_preserving_logs_does_not_require_approval(monkeypatch):
    """Copying logs somewhere safe harms nothing, and requiring sign-off would lose
    evidence while somebody looks for an approver."""
    _wire_planning(monkeypatch)
    actions = await rs.build_response_plan(_DB(), make_case(incident_type=vocab.TYPE_MALWARE))
    logs = next(a for a in actions if a.action_kind == vocab.ACT_PRESERVE_LOGS)
    assert not logs.requires_approval


@pytest.mark.asyncio
async def test_an_action_needs_a_rationale_a_reviewer_can_evaluate(monkeypatch):
    _wire_planning(monkeypatch)
    with pytest.raises(IncidentNotReadyError):
        await rs.add_action(
            _DB(), make_case(), action_kind=vocab.ACT_OTHER, title="do the thing",
            rationale="   ", expected_result="done",
        )


# ── Approval currency ────────────────────────────────────────────────────────────

def test_a_rejected_decision_authorises_nothing():
    assert not rs.is_approval_current(make_approval(vocab.DECISION_REJECT), now=NOW)


def test_an_expired_approval_authorises_nothing():
    """Containment approved on Monday's understanding should not license Friday's
    action."""
    stale = make_approval(expires_at=NOW - timedelta(seconds=1))
    assert not rs.is_approval_current(stale, now=NOW)


def test_no_approval_authorises_nothing():
    assert not rs.is_approval_current(None, now=NOW)


def test_a_current_approval_authorises():
    assert rs.is_approval_current(make_approval(), now=NOW)


@pytest.mark.asyncio
async def test_approving_a_high_risk_containment_requires_a_reason(monkeypatch):
    """Disabling the wrong account during an incident is its own incident."""
    case = make_case()
    action = make_action(case, status="proposed", risk="high")
    _wire_approval(monkeypatch, action)
    with pytest.raises(IncidentNotReadyError):
        await rs.decide_action(
            _DB(), case, action.id, reviewer_user_id=USER,
            decision=vocab.DECISION_APPROVE,
        )


@pytest.mark.asyncio
async def test_rejecting_requires_a_reason(monkeypatch):
    case = make_case()
    action = make_action(case, status="proposed")
    _wire_approval(monkeypatch, action)
    with pytest.raises(IncidentNotReadyError):
        await rs.decide_action(
            _DB(), case, action.id, reviewer_user_id=USER,
            decision=vocab.DECISION_REJECT, reason="  ",
        )


@pytest.mark.asyncio
async def test_a_decision_cannot_be_recorded_against_started_work(monkeypatch):
    case = make_case()
    action = make_action(case, status="in_progress")
    _wire_approval(monkeypatch, action)
    with pytest.raises(IncidentNotReadyError):
        await rs.decide_action(
            _DB(), case, action.id, reviewer_user_id=USER,
            decision=vocab.DECISION_REJECT, reason="too late",
        )


@pytest.mark.asyncio
async def test_an_approval_stamps_an_expiry(monkeypatch):
    case = make_case()
    action = make_action(case, status="proposed", risk="medium",
                         action_kind=vocab.ACT_REVOKE_SESSION)
    recorded = _wire_approval(monkeypatch, action)
    await rs.decide_action(
        _DB(), case, action.id, reviewer_user_id=USER,
        decision=vocab.DECISION_APPROVE, reason="confirmed with the account owner",
        now=NOW,
    )
    assert recorded["approval"].expires_at == NOW + rs.APPROVAL_TTL
    assert action.status == "approved"


# ── §50: an attestation is not a verification ────────────────────────────────────

@pytest.mark.asyncio
async def test_a_tracked_action_is_recorded_as_attested_not_verified(monkeypatch):
    """Consiva did not watch the account being disabled and cannot check that it was.
    The record says exactly that."""
    case = make_case()
    action = make_action(case)
    execution = _new_execution(case, action)
    _wire_execution(monkeypatch, action=action, approval=make_approval(),
                    claim=(execution, True))

    result = await rs.record_tracked_execution(
        _DB(), case, action.id, performed_by="Priya (SecOps)",
        attestation="Disabled alice@corp in Okta at 12:04 and confirmed in the admin UI",
        actor_user_id=USER, now=NOW,
    )
    assert result.verification_status == "attested"
    assert result.verification_status != "read_back"
    assert "cannot confirm its effect" in result.verification_detail["note"]
    assert result.performed_by == "Priya (SecOps)"
    assert action.status == "completed"


@pytest.mark.asyncio
async def test_an_attestation_must_name_who_did_it(monkeypatch):
    case = make_case()
    action = make_action(case)
    _wire_execution(monkeypatch, action=action, approval=make_approval())
    with pytest.raises(IncidentNotReadyError):
        await rs.record_tracked_execution(
            _DB(), case, action.id, performed_by="  ", attestation="did it",
            actor_user_id=USER, now=NOW,
        )


@pytest.mark.asyncio
async def test_an_attestation_must_say_what_was_done(monkeypatch):
    """A tick with no description is not a record of anything."""
    case = make_case()
    action = make_action(case)
    _wire_execution(monkeypatch, action=action, approval=make_approval())
    with pytest.raises(IncidentNotReadyError):
        await rs.record_tracked_execution(
            _DB(), case, action.id, performed_by="Priya", attestation="",
            actor_user_id=USER, now=NOW,
        )


# ── The §24 pre-flight ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_acting_without_approval_is_refused(monkeypatch):
    case = make_case()
    action = make_action(case, requires_approval=True)
    _wire_execution(monkeypatch, action=action, approval=None)
    with pytest.raises(ApprovalRequiredError):
        await rs.record_tracked_execution(
            _DB(), case, action.id, performed_by="P", attestation="a",
            actor_user_id=USER, now=NOW,
        )


@pytest.mark.asyncio
async def test_acting_on_an_expired_approval_is_refused(monkeypatch):
    case = make_case()
    action = make_action(case)
    _wire_execution(monkeypatch, action=action,
                    approval=make_approval(expires_at=NOW - timedelta(hours=2)))
    with pytest.raises(ApprovalRequiredError):
        await rs.record_tracked_execution(
            _DB(), case, action.id, performed_by="P", attestation="a",
            actor_user_id=USER, now=NOW,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [vocab.REPORTED, vocab.INVESTIGATING, vocab.RISK_ASSESSMENT,
     vocab.RESPONSE_PENDING, vocab.APPROVAL_REQUIRED],
)
async def test_containment_is_refused_before_the_incident_is_approved(monkeypatch, status):
    case = make_case(status=status)
    action = make_action(case)
    _wire_execution(monkeypatch, action=action, approval=make_approval())
    with pytest.raises(ApprovalRequiredError):
        await rs.record_tracked_execution(
            _DB(), case, action.id, performed_by="P", attestation="a",
            actor_user_id=USER, now=NOW,
        )


@pytest.mark.asyncio
async def test_a_blocked_action_cannot_be_performed(monkeypatch):
    case = make_case()
    action = make_action(case, status="blocked", blocked_reason="owner unreachable")
    _wire_execution(monkeypatch, action=action, approval=make_approval())
    with pytest.raises(ActionBlockedError):
        await rs.record_tracked_execution(
            _DB(), case, action.id, performed_by="P", attestation="a",
            actor_user_id=USER, now=NOW,
        )


# ── §25: idempotency ─────────────────────────────────────────────────────────────

def test_the_same_action_yields_the_same_key():
    case = make_case()
    action_id = uuid.uuid4()
    a = make_action(case, id=action_id)
    b = make_action(case, id=action_id)
    assert rs.idempotency_key(a) == rs.idempotency_key(b)


def test_a_different_target_yields_a_different_key():
    """Disabling alice@corp and bob@corp are different actions even under one plan."""
    case = make_case()
    action_id = uuid.uuid4()
    a = make_action(case, id=action_id, target="alice@corp")
    b = make_action(case, id=action_id, target="bob@corp")
    assert rs.idempotency_key(a) != rs.idempotency_key(b)


@pytest.mark.asyncio
async def test_recording_the_same_action_twice_does_not_duplicate_it(monkeypatch):
    """Rotating a credential twice can lock out the very people trying to respond."""
    case = make_case()
    action = make_action(case)
    already = _new_execution(case, action)
    already.status = "succeeded"
    already.verification_status = "attested"
    already.performed_by = "Priya"

    _wire_execution(monkeypatch, action=action, approval=make_approval(),
                    claim=(already, False))

    result = await rs.record_tracked_execution(
        _DB(), case, action.id, performed_by="Someone else",
        attestation="did it again", actor_user_id=USER, now=NOW,
    )
    assert result is already
    assert result.performed_by == "Priya", "the second attempt overwrote the first record"
    assert result.attempts == 0, "the second attempt was counted as a new one"


# ── Failure is recorded, not swallowed ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_failed_action_records_why(monkeypatch):
    """An action somebody tried and could not complete differs from one nobody
    started, and the difference is usually the point at which to escalate."""
    case = make_case()
    action = make_action(case)
    _wire_execution(monkeypatch, action=action, approval=make_approval())
    result = await rs.mark_action_failed(
        _DB(), case, action.id, reason="no admin access to the identity provider",
        actor_user_id=USER,
    )
    assert result.status == "failed"
    assert "no admin access" in result.blocked_reason


@pytest.mark.asyncio
async def test_a_failure_must_say_why(monkeypatch):
    case = make_case()
    action = make_action(case)
    _wire_execution(monkeypatch, action=action, approval=make_approval())
    with pytest.raises(IncidentNotReadyError):
        await rs.mark_action_failed(
            _DB(), case, action.id, reason="", actor_user_id=USER
        )


# ── harness ──────────────────────────────────────────────────────────────────────

def _wire_planning(monkeypatch, existing=None):
    captured = {}
    repo = rs.incident_repository

    async def _list(db, i, o): return existing or []
    async def _add(db, org_id, rows): captured["rows"] = rows
    async def _audit(db, **kw): captured["audit"] = kw

    monkeypatch.setattr(repo, "list_actions", _list)
    monkeypatch.setattr(repo, "add_actions", _add)
    monkeypatch.setattr(rs.audit_service, "record", _audit)
    return captured


def _wire_approval(monkeypatch, action):
    recorded = {}
    repo = rs.incident_repository

    async def _get(db, aid, org): return action

    async def _record(db, **kw):
        recorded["approval"] = SimpleNamespace(**kw)
        return recorded["approval"]

    async def _audit(db, **kw): return None

    monkeypatch.setattr(repo, "get_action", _get)
    monkeypatch.setattr(repo, "record_approval", _record)
    monkeypatch.setattr(rs.audit_service, "record", _audit)
    return recorded


def _new_execution(case, action) -> IncidentExecution:
    row = IncidentExecution(
        id=uuid.uuid4(), org_id=ORG, incident_id=case.id, action_id=action.id,
        idempotency_key="k", execution_mode=vocab.EXECUTION_MODE_TRACKED,
        status="pending",
    )
    row.attempts = 0
    return row


def _wire_execution(monkeypatch, *, action, approval, claim=None):
    repo = rs.incident_repository

    async def _get(db, aid, org): return action
    async def _latest(db, aid, org): return approval
    async def _claim(db, **kw): return claim or (_new_execution(SimpleNamespace(id=action.incident_id), action), True)
    async def _audit(db, **kw): return None

    monkeypatch.setattr(repo, "get_action", _get)
    monkeypatch.setattr(repo, "latest_approval_for_action", _latest)
    monkeypatch.setattr(repo, "claim_execution", _claim)
    monkeypatch.setattr(rs.audit_service, "record", _audit)
