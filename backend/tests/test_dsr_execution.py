"""Approval and controlled execution (prompt §22-§25, §42 scenarios 7 and 8).

The properties here are the ones where being wrong changes a customer's data:
execution requires a current approval, an idempotency key is claimed before any
side effect, and a write that does not read back correctly is never reported as a
success.
"""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.agents.dsr.connectors.base import ExecutionOutcome
from app.agents.dsr.errors import (
    ActionBlockedError,
    ActionFailedError,
    ApprovalRequiredError,
    CaseNotReadyError,
    VerificationFailedError,
)
from app.agents.dsr.schemas import case
from app.agents.dsr.services import approval_service, execution_service
from app.db.models import DsrAction, DsrApproval, DsrExecution, DsrRequest

NOW = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)
ORG = uuid.uuid4()


def make_request(status=case.APPROVED) -> DsrRequest:
    return DsrRequest(
        id=uuid.uuid4(), org_id=ORG, reference="DSR-EXEC01", raw_request="delete my data",
        request_type=case.DELETION, due_at=NOW + timedelta(days=30),
        requester_email="a@b.com", status=status,
    )


def make_action(request, **kw) -> DsrAction:
    return DsrAction(
        id=kw.get("id", uuid.uuid4()), org_id=ORG, request_id=request.id, plan_id=uuid.uuid4(),
        evidence_id=kw.get("evidence_id"), data_source_id=kw.get("data_source_id", uuid.uuid4()),
        source_name="crm", table_name=kw.get("table_name", "customers"),
        record_reference=kw.get("record_reference", {"id": 7}),
        operation=kw.get("operation", case.OP_DELETE_RECORD),
        operation_payload=kw.get("operation_payload", {}),
        reason="requested erasure", expected_result="record removed",
        risk=kw.get("risk", "high"), requires_approval=kw.get("requires_approval", True),
        status=kw.get("status", "approved"), blocked_reason=kw.get("blocked_reason"),
    )


def make_approval(decision="approved", expires_at=NOW + timedelta(days=1)) -> DsrApproval:
    return DsrApproval(
        id=uuid.uuid4(), org_id=ORG, request_id=uuid.uuid4(), action_id=uuid.uuid4(),
        decision=decision, reviewer_user_id=uuid.uuid4(), expires_at=expires_at,
    )


# ── Approval currency ────────────────────────────────────────────────────────────

def test_a_rejected_decision_authorizes_nothing():
    assert not approval_service.is_approval_current(make_approval("rejected"), now=NOW)


def test_an_expired_approval_authorizes_nothing():
    """A deletion approved months ago and never run must not silently execute
    against data that has since changed."""
    stale = make_approval("approved", expires_at=NOW - timedelta(seconds=1))
    assert not approval_service.is_approval_current(stale, now=NOW)


def test_a_current_approval_authorizes():
    assert approval_service.is_approval_current(make_approval("approved"), now=NOW)


def test_no_approval_authorizes_nothing():
    assert not approval_service.is_approval_current(None, now=NOW)


# ── Idempotency keys ─────────────────────────────────────────────────────────────

def test_same_action_yields_the_same_key():
    request = make_request()
    action_id = uuid.uuid4()
    a = make_action(request, id=action_id)
    b = make_action(request, id=action_id)
    assert execution_service.idempotency_key(a) == execution_service.idempotency_key(b)


def test_a_different_record_yields_a_different_key():
    request = make_request()
    action_id = uuid.uuid4()
    a = make_action(request, id=action_id, record_reference={"id": 7})
    b = make_action(request, id=action_id, record_reference={"id": 8})
    assert execution_service.idempotency_key(a) != execution_service.idempotency_key(b)


def test_a_different_payload_yields_a_different_key():
    """A re-planned action with new values is genuinely different work."""
    request = make_request()
    action_id = uuid.uuid4()
    a = make_action(request, id=action_id, operation=case.OP_UPDATE_FIELD,
                    operation_payload={"phone": "111"})
    b = make_action(request, id=action_id, operation=case.OP_UPDATE_FIELD,
                    operation_payload={"phone": "222"})
    assert execution_service.idempotency_key(a) != execution_service.idempotency_key(b)


def test_payload_key_order_does_not_change_the_key():
    request = make_request()
    action_id = uuid.uuid4()
    a = make_action(request, id=action_id, operation=case.OP_UPDATE_FIELD,
                    operation_payload={"phone": "1", "name": "A"})
    b = make_action(request, id=action_id, operation=case.OP_UPDATE_FIELD,
                    operation_payload={"name": "A", "phone": "1"})
    assert execution_service.idempotency_key(a) == execution_service.idempotency_key(b)


# ── §42 scenario 8: duplicate execution ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_second_execution_returns_the_first_result_without_re_executing(monkeypatch):
    request = make_request()
    action = make_action(request)
    already = DsrExecution(
        id=uuid.uuid4(), org_id=ORG, request_id=request.id, action_id=action.id,
        idempotency_key="k", status="verified", rows_affected=1,
        verification_status="passed", verified_at=NOW,
    )
    connector = RecordingConnector()
    _wire(monkeypatch, action=action, approval=make_approval(), claim=(already, False),
          connector=connector)

    result = await execution_service.execute_action(
        _DB(), request, action.id, actor_user_id=uuid.uuid4(), now=NOW
    )
    assert result is already
    assert result.status == "verified"
    assert connector.calls == 0, "the action was performed a second time"


@pytest.mark.asyncio
async def test_the_key_is_claimed_before_the_connector_is_built(monkeypatch):
    """Two concurrent callers must race on the unique index, not on who finishes
    their pre-flight checks first."""
    request = make_request()
    action = make_action(request)
    order: list[str] = []

    connector = RecordingConnector(on_call=lambda: order.append("connector"))

    async def _claim(db, **kw):
        order.append("claim")
        return _new_execution(request, action), True

    _wire(monkeypatch, action=action, approval=make_approval(), connector=connector)
    monkeypatch.setattr(execution_service.dsr_repository, "claim_execution", _claim)

    await execution_service.execute_action(_DB(), request, action.id, now=NOW)
    assert order[0] == "claim", f"connector ran before the claim: {order}"


# ── §42 scenario 7: approval rejection blocks execution ──────────────────────────

@pytest.mark.asyncio
async def test_execution_without_approval_is_refused(monkeypatch):
    request = make_request()
    action = make_action(request, requires_approval=True)
    connector = RecordingConnector()
    _wire(monkeypatch, action=action, approval=None, connector=connector)
    with pytest.raises(ApprovalRequiredError):
        await execution_service.execute_action(_DB(), request, action.id, now=NOW)
    assert connector.calls == 0


@pytest.mark.asyncio
async def test_execution_after_a_rejection_is_refused(monkeypatch):
    request = make_request()
    action = make_action(request)
    connector = RecordingConnector()
    _wire(monkeypatch, action=action, approval=make_approval("rejected"), connector=connector)
    with pytest.raises(ApprovalRequiredError):
        await execution_service.execute_action(_DB(), request, action.id, now=NOW)
    assert connector.calls == 0


@pytest.mark.asyncio
async def test_execution_with_an_expired_approval_is_refused(monkeypatch):
    request = make_request()
    action = make_action(request)
    connector = RecordingConnector()
    _wire(monkeypatch, action=action, connector=connector,
          approval=make_approval("approved", expires_at=NOW - timedelta(hours=1)))
    with pytest.raises(ApprovalRequiredError):
        await execution_service.execute_action(_DB(), request, action.id, now=NOW)
    assert connector.calls == 0


@pytest.mark.asyncio
async def test_a_blocked_action_cannot_be_executed(monkeypatch):
    request = make_request()
    action = make_action(request, status="blocked", blocked_reason="retention rule FIN-3")
    connector = RecordingConnector()
    _wire(monkeypatch, action=action, approval=make_approval(), connector=connector)
    with pytest.raises(ActionBlockedError):
        await execution_service.execute_action(_DB(), request, action.id, now=NOW)
    assert connector.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status", [case.RECEIVED, case.SEARCH_COMPLETED, case.REVIEW_REQUIRED, case.APPROVAL_REQUIRED],
)
async def test_execution_is_refused_from_a_pre_approval_case_status(monkeypatch, status):
    request = make_request(status=status)
    action = make_action(request)
    connector = RecordingConnector()
    _wire(monkeypatch, action=action, approval=make_approval(), connector=connector)
    with pytest.raises(ApprovalRequiredError):
        await execution_service.execute_action(_DB(), request, action.id, now=NOW)
    assert connector.calls == 0


# ── Verification is a separate claim ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_unverified_write_is_not_reported_as_success(monkeypatch):
    request = make_request()
    action = make_action(request)
    connector = RecordingConnector(raises=VerificationFailedError("read-back disagreed"))
    execution = _new_execution(request, action)
    _wire(monkeypatch, action=action, approval=make_approval(), connector=connector,
          claim=(execution, True))

    with pytest.raises(VerificationFailedError):
        await execution_service.execute_action(_DB(), request, action.id, now=NOW)

    assert execution.status == "failed"
    assert execution.verification_status == "failed"
    assert execution.error_code == case.ERR_VERIFICATION_FAILED
    assert execution.error_detail, "a failure must leave its reason on the row"
    assert action.status == "failed"


@pytest.mark.asyncio
async def test_verified_write_records_both_facts(monkeypatch):
    request = make_request()
    action = make_action(request)
    execution = _new_execution(request, action)
    connector = RecordingConnector(outcome=ExecutionOutcome(
        rows_affected=1, verified=True, verification_detail={"check": "record absent"},
    ))
    _wire(monkeypatch, action=action, approval=make_approval(), connector=connector,
          claim=(execution, True))

    result = await execution_service.execute_action(_DB(), request, action.id, now=NOW)
    assert result.rows_affected == 1
    assert result.verification_status == "passed"
    assert result.status == "verified"
    assert result.verified_at == NOW
    assert action.status == "executed"


@pytest.mark.asyncio
async def test_an_unexpected_error_still_lands_on_the_row(monkeypatch):
    """§47: an execution row left at 'running' forever is exactly the silent loss
    the prompt forbids."""
    request = make_request()
    action = make_action(request)
    execution = _new_execution(request, action)
    connector = RecordingConnector(raises=RuntimeError("driver exploded"))
    _wire(monkeypatch, action=action, approval=make_approval(), connector=connector,
          claim=(execution, True))

    with pytest.raises(ActionFailedError):
        await execution_service.execute_action(_DB(), request, action.id, now=NOW)
    assert execution.status == "failed"
    assert execution.error_code == case.ERR_ACTION_FAILED
    assert execution.completed_at == NOW


@pytest.mark.asyncio
async def test_a_moved_target_is_refused(monkeypatch):
    """The record reference no longer matches the evidence it came from -- the row
    may have been replaced since the search."""
    request = make_request()
    action = make_action(request, evidence_id=uuid.uuid4(), record_reference={"id": 7})
    evidence = SimpleNamespace(record_reference={"id": 99}, record_snapshot={})
    connector = RecordingConnector()
    _wire(monkeypatch, action=action, approval=make_approval(), connector=connector,
          evidence=evidence)
    with pytest.raises(ActionBlockedError):
        await execution_service.execute_action(_DB(), request, action.id, now=NOW)
    assert connector.calls == 0


# ── Approval decisions ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_rejecting_requires_a_reason(monkeypatch):
    request = make_request()
    action = make_action(request, status="proposed")
    _wire_approval(monkeypatch, action)
    with pytest.raises(CaseNotReadyError):
        await approval_service.decide_action(
            _DB(), request, action.id, reviewer_user_id=uuid.uuid4(),
            decision="rejected", reason="  ",
        )


@pytest.mark.asyncio
async def test_approving_a_high_risk_action_requires_a_reason(monkeypatch):
    request = make_request()
    action = make_action(request, status="proposed", risk="high")
    _wire_approval(monkeypatch, action)
    with pytest.raises(CaseNotReadyError):
        await approval_service.decide_action(
            _DB(), request, action.id, reviewer_user_id=uuid.uuid4(), decision="approved",
        )


@pytest.mark.asyncio
async def test_a_blocked_action_cannot_be_approved_away(monkeypatch):
    """A reviewer may escalate or reject, but approval cannot override a retention
    rule or a missing authorization."""
    request = make_request()
    action = make_action(request, status="blocked", blocked_reason="retention FIN-3")
    _wire_approval(monkeypatch, action)
    with pytest.raises(CaseNotReadyError):
        await approval_service.decide_action(
            _DB(), request, action.id, reviewer_user_id=uuid.uuid4(),
            decision="approved", reason="customer insisted",
        )


@pytest.mark.asyncio
async def test_a_decision_cannot_be_recorded_against_started_work(monkeypatch):
    request = make_request()
    action = make_action(request, status="executed")
    _wire_approval(monkeypatch, action)
    with pytest.raises(CaseNotReadyError):
        await approval_service.decide_action(
            _DB(), request, action.id, reviewer_user_id=uuid.uuid4(),
            decision="rejected", reason="too late",
        )


@pytest.mark.asyncio
async def test_an_approval_stamps_an_expiry(monkeypatch):
    request = make_request()
    action = make_action(request, status="proposed", risk="medium")
    recorded = _wire_approval(monkeypatch, action)
    await approval_service.decide_action(
        _DB(), request, action.id, reviewer_user_id=uuid.uuid4(),
        decision="approved", reason="verified with the requester", now=NOW,
    )
    assert recorded["approval"].expires_at == NOW + approval_service.APPROVAL_TTL
    assert action.status == "approved"


@pytest.mark.asyncio
async def test_an_unknown_decision_is_refused(monkeypatch):
    request = make_request()
    action = make_action(request, status="proposed")
    _wire_approval(monkeypatch, action)
    with pytest.raises(CaseNotReadyError):
        await approval_service.decide_action(
            _DB(), request, action.id, reviewer_user_id=uuid.uuid4(), decision="maybe",
        )


# ── harness ──────────────────────────────────────────────────────────────────────

class _DB:
    async def flush(self):
        return None


class RecordingConnector:
    def __init__(self, *, outcome=None, raises=None, on_call=None):
        self._outcome = outcome or ExecutionOutcome(
            rows_affected=1, verified=True, verification_detail={}
        )
        self._raises = raises
        self._on_call = on_call
        self.calls = 0

    async def execute_action(self, **kw):
        self.calls += 1
        if self._on_call:
            self._on_call()
        if self._raises:
            raise self._raises
        return self._outcome


def _new_execution(request, action) -> DsrExecution:
    row = DsrExecution(
        id=uuid.uuid4(), org_id=ORG, request_id=request.id, action_id=action.id,
        idempotency_key="k", status="pending",
    )
    row.attempts = 0
    return row


def _wire(monkeypatch, *, action, approval, connector, claim=None, evidence=None):
    repo = execution_service.dsr_repository

    async def _get_action(db, action_id, org_id):
        return action

    async def _latest_approval(db, action_id, org_id):
        return approval

    async def _claim(db, **kw):
        return claim if claim else (_new_execution(SimpleNamespace(id=action.request_id), action), True)

    async def _get_evidence(db, evidence_id, org_id):
        return evidence

    async def _get_source_auth(db, data_source_id, org_id):
        return SimpleNamespace(
            enabled=True, searchable_tables=["customers"], identity_tables=["customers"],
            identifier_columns={"customers": {"email": "email"}},
            returnable_columns={"customers": ["name"]},
            erasable_columns={"customers": ["name", "phone"]},
            allow_execution=True, write_credential_ref="SRC_WRITE",
        )

    monkeypatch.setattr(repo, "get_action", _get_action)
    monkeypatch.setattr(repo, "latest_approval_for_action", _latest_approval)
    monkeypatch.setattr(repo, "claim_execution", _claim)
    monkeypatch.setattr(repo, "get_evidence", _get_evidence)
    monkeypatch.setattr(repo, "get_source_authorization", _get_source_auth)

    async def _get_data_source(db, source_id, org_id):
        return SimpleNamespace(id=source_id, name="crm", enabled=True, connector="postgres",
                               config={"host": "h", "dbname": "d", "user": "u"},
                               credential_ref="SRC_READ")

    monkeypatch.setattr(execution_service.ropa_repository, "get_data_source", _get_data_source)
    monkeypatch.setattr(execution_service.factory, "build_connector", lambda **kw: connector)

    async def _identity_ok(db, request):
        return SimpleNamespace(status=case.IDV_VERIFIED)

    monkeypatch.setattr(execution_service.identity_service, "assert_identity_satisfied", _identity_ok)

    async def _audit(db, **kw):
        return None

    monkeypatch.setattr(execution_service.audit_service, "record", _audit)


def _wire_approval(monkeypatch, action) -> dict:
    recorded: dict = {}
    repo = approval_service.dsr_repository

    async def _get_action(db, action_id, org_id):
        return action

    async def _record_approval(db, **kw):
        approval = DsrApproval(
            id=uuid.uuid4(), org_id=kw["org_id"], request_id=kw["request_id"],
            action_id=kw.get("action_id"), plan_id=kw.get("plan_id"),
            decision=kw["decision"], reason=kw.get("reason"),
            reviewer_user_id=kw["reviewer_user_id"], expires_at=kw.get("expires_at"),
        )
        recorded["approval"] = approval
        return approval

    async def _audit(db, **kw):
        return None

    monkeypatch.setattr(repo, "get_action", _get_action)
    monkeypatch.setattr(repo, "record_approval", _record_approval)
    monkeypatch.setattr(approval_service.audit_service, "record", _audit)
    return recorded
