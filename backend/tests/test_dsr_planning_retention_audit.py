"""Action planning, retention enforcement, and action-decision integrity audit.

Covers the DSR "Action Planning, Retention, and Approval" lane:

  * A DSR action must not be decided twice -- the same class of bug the Purpose
    Classifier's `FindingAlreadyDecidedError` closes for a consent finding, here for
    `DsrAction`. Before this file's fix, `approval_service.decide_action` only
    guarded "executed"/"executing" and "approve a blocked action"; an action already
    APPROVED or REJECTED could be decided again with no conflict, silently
    overwriting the first reviewer's call.

  * A correction's update payload is genuinely validated -- a column outside the
    source's erasable allowlist is BLOCKED (reported in the plan), never silently
    dropped or silently written; an empty correction is blocked too, both via the
    constraint engine at plan time and via `assert_writable_columns` at execution
    time.

  * A retention rule that applies to a table actually blocks the mutating action,
    and the blocked reason cites the rule's `authority` (the legal basis text), not
    a generic message.

  * Disclosure (OP_DISCLOSE, used for access/export/information requests) only ever
    returns columns the source's authorization marked returnable -- even if a row
    somehow carried more data than was selected for.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.agents.dsr.connectors import authorization
from app.agents.dsr.connectors.postgres import PostgresDsrConfig, PostgresDsrConnector
from app.agents.dsr.errors import CaseNotReadyError
from app.agents.dsr.rules import constraints as rules
from app.agents.dsr.schemas import case
from app.agents.dsr.services import approval_service, planning_service
from app.db.models import DsrAction, DsrApproval, DsrEvidence, DsrRequest

NOW = datetime(2026, 9, 12, tzinfo=UTC)
ORG = uuid.uuid4()


# ══════════════════════════════════════════════════════════════════════════════
# 1. Duplicate / conflicting decisions on the same action
# ══════════════════════════════════════════════════════════════════════════════

def make_request(status=case.APPROVAL_REQUIRED) -> DsrRequest:
    return DsrRequest(
        id=uuid.uuid4(), org_id=ORG, reference="DSR-AUDIT01", raw_request="delete my data",
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
        status=kw.get("status", "proposed"), blocked_reason=kw.get("blocked_reason"),
    )


class _DB:
    async def flush(self):
        return None


def _wire_approval(monkeypatch, action) -> dict:
    """Same harness shape as test_dsr_execution.py's `_wire_approval` (not imported
    from there, since that file is outside this lane and may be edited concurrently
    by another agent in this session)."""
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
        recorded.setdefault("audit_calls", []).append(kw)
        return None

    monkeypatch.setattr(repo, "get_action", _get_action)
    monkeypatch.setattr(repo, "record_approval", _record_approval)
    monkeypatch.setattr(approval_service.audit_service, "record", _audit)
    return recorded


@pytest.mark.asyncio
async def test_an_approved_action_cannot_be_approved_again(monkeypatch):
    """A double-click or a retried request re-submitting the same decision must not
    be treated as a fresh one -- this is the idempotency half of the bug."""
    request = make_request()
    action = make_action(request, status="proposed", risk="medium")
    _wire_approval(monkeypatch, action)
    await approval_service.decide_action(
        _DB(), request, action.id, reviewer_user_id=uuid.uuid4(),
        decision="approved", reason="verified with the requester", now=NOW,
    )
    assert action.status == "approved"

    with pytest.raises(CaseNotReadyError, match="already been decided"):
        await approval_service.decide_action(
            _DB(), request, action.id, reviewer_user_id=uuid.uuid4(),
            decision="approved", reason="again", now=NOW,
        )
    assert action.status == "approved"


@pytest.mark.asyncio
async def test_an_approved_action_cannot_be_silently_flipped_to_rejected(monkeypatch):
    """THE bug: before the fix, a second reviewer (or a stale UI) calling decide
    with a DIFFERENT decision on an already-approved action silently overwrote the
    first reviewer's call with no conflict raised at all -- a HIGH-risk deletion
    that was approved could be rejected after the fact (or vice versa) and the
    action's status would just flip, with only the audit log distinguishing the
    two decisions."""
    request = make_request()
    action = make_action(request, status="proposed", risk="high")
    recorded = _wire_approval(monkeypatch, action)
    await approval_service.decide_action(
        _DB(), request, action.id, reviewer_user_id=uuid.uuid4(),
        decision="approved", reason="first reviewer signs off", now=NOW,
    )
    assert action.status == "approved"
    audit_calls_after_first = len(recorded.get("audit_calls", []))

    with pytest.raises(CaseNotReadyError, match="already been decided"):
        await approval_service.decide_action(
            _DB(), request, action.id, reviewer_user_id=uuid.uuid4(),
            decision="rejected", reason="second reviewer disagrees", now=NOW,
        )

    # The conflict is raised BEFORE anything is recorded: no second approval row,
    # no second audit entry, and the action's status is exactly what the first,
    # legitimate decision set it to.
    assert action.status == "approved"
    assert len(recorded.get("audit_calls", [])) == audit_calls_after_first


@pytest.mark.asyncio
async def test_a_rejected_action_cannot_be_approved_after_the_fact(monkeypatch):
    request = make_request()
    action = make_action(request, status="proposed", risk="low")
    _wire_approval(monkeypatch, action)
    await approval_service.decide_action(
        _DB(), request, action.id, reviewer_user_id=uuid.uuid4(),
        decision="rejected", reason="requester withdrew this item", now=NOW,
    )
    assert action.status == "rejected"

    with pytest.raises(CaseNotReadyError, match="already been decided"):
        await approval_service.decide_action(
            _DB(), request, action.id, reviewer_user_id=uuid.uuid4(),
            decision="approved", reason="changed my mind", now=NOW,
        )
    assert action.status == "rejected"


@pytest.mark.asyncio
async def test_request_more_information_reopens_the_action_for_a_fresh_decision(monkeypatch):
    """The guard must not block the documented escape hatch: `escalated` and
    `request_more_information` deliberately return an action to "proposed" so it can
    be decided again once the extra information arrives -- that is how a
    too-early decision is reopened, not a conflict."""
    request = make_request()
    action = make_action(request, status="proposed", risk="high")
    _wire_approval(monkeypatch, action)
    await approval_service.decide_action(
        _DB(), request, action.id, reviewer_user_id=uuid.uuid4(),
        decision="request_more_information", reason="need the requester to confirm", now=NOW,
    )
    assert action.status == "proposed"

    # Now a real decision can be recorded -- this must NOT raise.
    await approval_service.decide_action(
        _DB(), request, action.id, reviewer_user_id=uuid.uuid4(),
        decision="approved", reason="confirmed by the requester", now=NOW,
    )
    assert action.status == "approved"


# ══════════════════════════════════════════════════════════════════════════════
# 2. Correction payloads are validated, not silently dropped or silently applied
# ══════════════════════════════════════════════════════════════════════════════

def make_grant(**kw):
    auth = SimpleNamespace(
        enabled=True,
        searchable_tables=kw.get("searchable_tables", ["customers", "invoices"]),
        identity_tables=["customers"],
        identifier_columns={"customers": {"email": "email"}, "invoices": {"email": "customer_email"}},
        returnable_columns={"customers": ["name", "email", "phone"], "invoices": ["amount", "created_at"]},
        erasable_columns=kw.get("erasable_columns", {"customers": ["name", "phone", "email"]}),
        allow_execution=kw.get("allow_execution", True),
        write_credential_ref=kw.get("write_credential_ref", "SRC_WRITE"),
    )
    return authorization.resolve(auth, source_name="crm")


def make_evidence(table="customers", snapshot=None, source="crm") -> DsrEvidence:
    return DsrEvidence(
        id=uuid.uuid4(), org_id=ORG, request_id=uuid.uuid4(), search_run_id=uuid.uuid4(),
        data_source_id=uuid.uuid4(),
        source_name=source, table_name=table, matched_column="email", identifier_kind="email",
        match_type="normalized_exact", confidence=1.0, record_reference={"id": 7},
        record_snapshot=snapshot if snapshot is not None else {"name": "A", "email": "a@b.com"},
    )


def make_case_request(request_type=case.CORRECTION) -> DsrRequest:
    return DsrRequest(
        id=uuid.uuid4(), org_id=ORG, reference="DSR-AUDIT02",
        raw_request="correct my phone number", request_type=request_type,
        due_at=NOW + timedelta(days=30), requester_email="a@b.com", status=case.SEARCH_COMPLETED,
    )


async def _build(monkeypatch, request, evidence, grants, *, corrections=None, retention_rules=()):
    """Drive build_plan against in-memory repository stubs (same shape as
    test_dsr_planning.py's own harness)."""
    created = {}

    async def _list_evidence(db, request_id, org_id):
        return evidence

    async def _current_plan(db, request_id, org_id):
        return None

    async def _next_version(db, request_id, org_id):
        return 1

    async def _create_plan(db, *, org_id, request_id, version, summary, constraints_evaluated,
                            requires_approval):
        from app.db.models import DsrActionPlan
        plan = DsrActionPlan(
            id=uuid.uuid4(), org_id=org_id, request_id=request_id, version=version,
            summary=summary, constraints_evaluated=constraints_evaluated,
            requires_approval=requires_approval, status="draft",
        )
        created["plan"] = plan
        return plan

    async def _add_actions(db, org_id, rows):
        created["actions"] = rows

    repo = planning_service.dsr_repository
    monkeypatch.setattr(repo, "list_evidence", _list_evidence)
    monkeypatch.setattr(repo, "get_current_plan", _current_plan)
    monkeypatch.setattr(repo, "next_plan_version", _next_version)
    monkeypatch.setattr(repo, "create_plan", _create_plan)
    monkeypatch.setattr(repo, "add_actions", _add_actions)

    return await planning_service.build_plan(
        _DB(), request, grants=grants, corrections=corrections, retention_rules=retention_rules,
    )


@pytest.mark.asyncio
async def test_a_valid_correction_is_accepted_and_still_needs_human_approval(monkeypatch):
    evidence = [make_evidence(snapshot={"name": "A", "phone": "old", "email": "a@b.com"})]
    _, actions = await _build(
        monkeypatch, make_case_request(case.CORRECTION), evidence, {"crm": make_grant()},
        corrections={"phone": "+91-99999"},
    )
    action = actions[0]
    assert action.operation == case.OP_UPDATE_FIELD
    assert action.operation_payload == {"phone": "+91-99999"}
    assert action.status == "proposed"
    assert action.blocked_reason is None
    # A write is never auto-applied, even when nothing blocked it.
    assert action.requires_approval is True


@pytest.mark.asyncio
async def test_a_column_outside_the_allowlist_is_blocked_not_silently_dropped(monkeypatch):
    """§47: a blocked action stays in the plan carrying its reason. Silently
    dropping the unauthorized column and writing only the rest would look like
    success while quietly refusing part of what the requester asked for."""
    evidence = [make_evidence(snapshot={"account_balance": 100, "email": "a@b.com"})]
    _, actions = await _build(
        monkeypatch, make_case_request(case.CORRECTION), evidence,
        {"crm": make_grant(erasable_columns={"customers": ["phone"]})},
        corrections={"account_balance": 0},
    )
    action = actions[0]
    assert action.status == "blocked"
    assert "account_balance" in action.blocked_reason
    assert "allowlist" in action.blocked_reason
    # Nothing was quietly written -- a blocked action is retained (never silently
    # dropped from the plan, §47), and the reason names exactly which column and
    # table were refused.
    assert action.operation == case.OP_RETAIN


@pytest.mark.asyncio
async def test_an_empty_correction_payload_is_blocked(monkeypatch):
    """A correction that ends up naming zero columns (nothing supplied, or
    everything supplied was not actually on the record) must be refused, not
    treated as a no-op success."""
    evidence = [make_evidence(snapshot={"name": "A", "email": "a@b.com"})]
    _, actions = await _build(
        monkeypatch, make_case_request(case.CORRECTION), evidence, {"crm": make_grant()},
        corrections={},
    )
    action = actions[0]
    assert action.status == "blocked"
    assert "no columns to change" in action.blocked_reason


def test_assert_writable_columns_refuses_an_empty_payload_at_execution_time():
    """The same refusal is reachable at the actual write boundary (postgres.py
    calls `assert_writable_columns` immediately before building the UPDATE), not
    just inside the planner -- defense in depth, not a dead code path."""
    grant = make_grant()
    with pytest.raises(Exception) as exc_info:
        grant.assert_writable_columns("customers", {})
    assert "empty update" in str(exc_info.value)


def test_assert_writable_columns_refuses_an_unauthorized_column_at_execution_time():
    grant = make_grant(erasable_columns={"customers": ["phone"]})
    with pytest.raises(Exception) as exc_info:
        grant.assert_writable_columns("customers", {"account_balance": 0})
    assert "account_balance" in str(exc_info.value)


# ══════════════════════════════════════════════════════════════════════════════
# 3. Retention rules block the mutating action and cite the authority
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_a_deletion_plan_against_a_retained_table_is_blocked_citing_the_authority(monkeypatch):
    """The exact scenario the lane asks for: a table with an active retention rule,
    a deletion plan against it -> blocked, citing the specific rule's authority
    (the legal basis text), not a generic 'blocked' message."""
    rule = rules.RetentionRule(
        table_name="invoices", minimum_retention=timedelta(days=365 * 7),
        date_column="created_at", authority="Companies Act 2013, s.128 (7-year books of account)",
    )
    evidence = [make_evidence(
        table="invoices", snapshot={"created_at": "2024-06-01T00:00:00Z", "amount": 500},
    )]
    _, actions = await _build(
        monkeypatch, make_case_request(case.DELETION), evidence, {"crm": make_grant()},
        retention_rules=(rule,),
    )
    action = actions[0]
    assert action.status == "blocked"
    assert action.operation == case.OP_RETAIN
    assert "Companies Act 2013, s.128 (7-year books of account)" in action.blocked_reason
    # The requester-facing text discloses the policy basis in full too (§20) --
    # it is not a configuration detail to withhold from them.
    assert "Companies Act 2013, s.128 (7-year books of account)" in action.requester_explanation


@pytest.mark.asyncio
async def test_a_retention_rule_for_a_different_table_does_not_block(monkeypatch):
    """A rule is scoped to its own table; it must not over-block unrelated data."""
    rule = rules.RetentionRule(
        table_name="invoices", minimum_retention=timedelta(days=365 * 7),
        date_column="created_at", authority="Finance policy FIN-3",
    )
    evidence = [make_evidence(table="customers", snapshot={"name": "A", "email": "a@b.com"})]
    _, actions = await _build(
        monkeypatch, make_case_request(case.DELETION), evidence, {"crm": make_grant()},
        retention_rules=(rule,),
    )
    action = actions[0]
    assert action.status == "proposed"
    assert action.operation == case.OP_DELETE_RECORD


# ══════════════════════════════════════════════════════════════════════════════
# 4. Disclosure (access/export/information) withholds unauthorized columns
# ══════════════════════════════════════════════════════════════════════════════

class _FakeConnection:
    def __init__(self, rows):
        self._rows = rows
        self.queries: list[tuple[str, tuple]] = []

    async def fetch(self, sql, *args):
        self.queries.append((sql, args))
        return list(self._rows)

    async def close(self):
        return None


def _stub_connect(conn):
    async def _connect(*, write: bool):
        return conn
    return _connect


def _make_connector(**kw):
    auth = SimpleNamespace(
        id=uuid.uuid4(), enabled=True,
        searchable_tables=kw.get("searchable_tables", ["customers"]),
        identity_tables=kw.get("identity_tables", ["customers"]),
        identifier_columns=kw.get("identifier_columns", {"customers": {"email": "email"}}),
        returnable_columns=kw.get("returnable_columns", {"customers": ["name", "email"]}),
        erasable_columns=kw.get("erasable_columns", {}),
        allow_execution=kw.get("allow_execution", False),
        write_credential_ref=kw.get("write_credential_ref", None),
    )
    grant = authorization.resolve(auth, source_name="testdb")
    config = PostgresDsrConfig(
        host="db.internal", port=5432, dbname="app",
        read_user="dsr_read", read_password="r-secret",
    )
    return PostgresDsrConnector(config=config, grant=grant)


@pytest.mark.asyncio
async def test_disclosure_withholds_a_column_outside_the_returnable_allowlist(monkeypatch):
    """OP_DISCLOSE (access/export/information, per planning_service's
    _OPERATION_FOR_TYPE) must never hand back more than the source's authorization
    marked returnable. The row carries a column ("ssn") the administrator never
    authorized for disclosure; even though it reached this process (simulating a
    driver or a future projection change that returns more than requested), the
    snapshot built for the requester must not contain it -- `_to_match` filters by
    `returnable_columns` independently of the projection."""
    conn = _FakeConnection(rows=[
        {"id": 1, "email": "a@b.com", "name": "A", "ssn": "123-45-6789"},
    ])
    connector = _make_connector(returnable_columns={"customers": ["name", "email"]})
    monkeypatch.setattr(connector, "_connect", _stub_connect(conn))

    outcome = await connector.search_subject(identifiers={"email": "a@b.com"})
    assert len(outcome.matches) == 1
    snapshot = outcome.matches[0].record_snapshot
    assert "ssn" not in snapshot
    assert snapshot == {"name": "A", "email": "a@b.com"}


@pytest.mark.asyncio
async def test_disclosure_of_a_table_with_nothing_returnable_reports_the_match_with_no_snapshot(monkeypatch):
    """An empty returnable_columns list is a configuration answer ('this table may be
    matched on, but nothing from it may be disclosed'), not a bug -- the match is
    still reported (never dropped, §47) but with no snapshot."""
    conn = _FakeConnection(rows=[{"id": 1, "email": "a@b.com", "name": "A"}])
    connector = _make_connector(returnable_columns={"customers": []})
    monkeypatch.setattr(connector, "_connect", _stub_connect(conn))

    outcome = await connector.search_subject(identifiers={"email": "a@b.com"})
    assert len(outcome.matches) == 1
    assert outcome.matches[0].record_snapshot is None
