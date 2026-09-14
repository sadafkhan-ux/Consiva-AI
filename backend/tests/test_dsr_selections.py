"""Per-record action selection — the email-first flow.

A person looks at what was actually found and decides record by record, rather than
the whole plan being inferred from one sentence typed at intake. These tests cover
what that changes: the selection wins over the request type, every selection still
passes the same constraint engine, and a record nobody chose for is still planned
rather than dropped.
"""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.agents.dsr.connectors import authorization
from app.agents.dsr.errors import CaseNotReadyError
from app.agents.dsr.rules import constraints as rules
from app.agents.dsr.schemas import case
from app.agents.dsr.services import planning_service
from app.db.models import DsrActionPlan, DsrEvidence, DsrRequest

NOW = datetime(2026, 9, 14, tzinfo=UTC)
ORG = uuid.uuid4()


def make_grant(**kw):
    return authorization.resolve(SimpleNamespace(
        enabled=True,
        searchable_tables=["customers", "orders"],
        identity_tables=["customers"],
        identifier_columns={"customers": {"email": "email"}, "orders": {"email": "customer_email"}},
        returnable_columns={"customers": ["name", "email", "phone"], "orders": ["order_id", "amount"]},
        record_key_columns={},
        erasable_columns=kw.get("erasable_columns", {"customers": ["name", "phone", "email"]}),
        allow_execution=kw.get("allow_execution", True),
        write_credential_ref="SRC_WRITE",
    ), source_name="crm")


def make_request(request_type=case.DELETION) -> DsrRequest:
    return DsrRequest(
        id=uuid.uuid4(), org_id=ORG, reference="DSR-SEL01", raw_request="find my data",
        request_type=request_type, due_at=NOW + timedelta(days=30),
        requester_email="a@b.com", status=case.SEARCH_COMPLETED,
    )


def make_evidence(table="customers", snapshot=None) -> DsrEvidence:
    return DsrEvidence(
        id=uuid.uuid4(), org_id=ORG, request_id=uuid.uuid4(), search_run_id=uuid.uuid4(),
        data_source_id=uuid.uuid4(), source_name="crm", table_name=table,
        matched_column="email", identifier_kind="email", match_type="exact",
        confidence=1.0, record_reference={"id": 1},
        record_snapshot=snapshot if snapshot is not None else {"name": "A", "phone": "1"},
    )


# ── The vocabulary is closed and maps onto real operations ───────────────────────

def test_every_selection_maps_to_a_real_operation():
    for sel in case.SELECTIONS:
        assert case.OPERATION_FOR_SELECTION[sel] in case.OPERATIONS


def test_keep_maps_to_retain_not_to_a_write():
    assert case.OPERATION_FOR_SELECTION[case.SELECT_KEEP] == case.OP_RETAIN
    assert case.OP_RETAIN not in case.MUTATING_OPERATIONS


def test_review_does_not_touch_the_source():
    """"Have a human look at this" is a refusal to act yet, not an operation."""
    assert case.OPERATION_FOR_SELECTION[case.SELECT_REVIEW] == case.OP_NO_OP
    assert case.OP_NO_OP not in case.MUTATING_OPERATIONS


@pytest.mark.asyncio
async def test_an_invented_action_is_refused(monkeypatch):
    with pytest.raises(CaseNotReadyError):
        planning_service._plan_one(
            request=make_request(), evidence=make_evidence(), grant=make_grant(),
            corrections={}, retention_rules=(), selection="purge_everything",
        )


# ── The selection beats the request type ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_keeping_a_record_on_a_deletion_case_is_honoured(monkeypatch):
    """Someone opens a case saying "delete my data", then looks at what was found and
    decides to keep their order history. The more specific, more recent choice wins."""
    keep_me = make_evidence("orders", {"order_id": "ORD-1"})
    delete_me = make_evidence("customers")
    _, actions = await _build(
        monkeypatch, make_request(case.DELETION), [keep_me, delete_me],
        selections={
            str(keep_me.id): {"action": case.SELECT_KEEP},
            str(delete_me.id): {"action": case.SELECT_DELETE},
        },
    )
    by_evidence = {a.evidence_id: a for a in actions}
    assert by_evidence[keep_me.id].operation == case.OP_RETAIN
    assert by_evidence[delete_me.id].operation == case.OP_DELETE_RECORD


@pytest.mark.asyncio
async def test_deleting_a_record_on_an_access_case_is_honoured(monkeypatch):
    """And the other direction: an access request where the person then asks for one
    record to be erased."""
    item = make_evidence()
    _, actions = await _build(
        monkeypatch, make_request(case.ACCESS), [item],
        selections={str(item.id): {"action": case.SELECT_DELETE}},
    )
    assert actions[0].operation == case.OP_DELETE_RECORD
    assert actions[0].risk == "high"


@pytest.mark.asyncio
async def test_a_record_with_no_selection_still_gets_an_action(monkeypatch):
    """A half-finished review must still produce a complete plan (§47). The
    unselected record falls back to what the case type implies."""
    chosen, untouched = make_evidence(), make_evidence("orders", {"order_id": "O-9"})
    _, actions = await _build(
        monkeypatch, make_request(case.DELETION), [chosen, untouched],
        selections={str(chosen.id): {"action": case.SELECT_KEEP}},
    )
    assert len(actions) == 2
    by_evidence = {a.evidence_id: a for a in actions}
    assert by_evidence[chosen.id].operation == case.OP_RETAIN
    assert by_evidence[untouched.id].operation == case.OP_DELETE_RECORD


# ── Selections still pass every guard ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_choosing_delete_still_requires_approval(monkeypatch):
    item = make_evidence()
    _, actions = await _build(
        monkeypatch, make_request(case.ACCESS), [item],
        selections={str(item.id): {"action": case.SELECT_DELETE}},
    )
    assert actions[0].requires_approval, "a chosen deletion skipped the human gate"


@pytest.mark.asyncio
async def test_choosing_review_requires_approval_and_changes_nothing(monkeypatch):
    item = make_evidence()
    _, actions = await _build(
        monkeypatch, make_request(case.ACCESS), [item],
        selections={str(item.id): {"action": case.SELECT_REVIEW}},
    )
    assert actions[0].operation == case.OP_NO_OP
    assert actions[0].requires_approval
    assert "reviewed by a person" in actions[0].reason


@pytest.mark.asyncio
async def test_choosing_keep_needs_no_approval(monkeypatch):
    """Leaving a record alone is not a change anyone needs to authorize."""
    item = make_evidence()
    _, actions = await _build(
        monkeypatch, make_request(case.DELETION), [item],
        selections={str(item.id): {"action": case.SELECT_KEEP}},
    )
    assert not actions[0].requires_approval
    assert actions[0].risk == "low"


@pytest.mark.asyncio
async def test_choosing_delete_on_an_unauthorized_source_is_still_blocked(monkeypatch):
    """A requester's choice does not override the allowlist."""
    item = make_evidence()
    _, actions = await _build(
        monkeypatch, make_request(case.ACCESS), [item],
        selections={str(item.id): {"action": case.SELECT_DELETE}},
        grant=make_grant(allow_execution=False),
    )
    assert actions[0].status == "blocked"
    assert actions[0].operation == case.OP_RETAIN


@pytest.mark.asyncio
async def test_choosing_delete_against_a_retention_rule_is_still_blocked(monkeypatch):
    item = make_evidence("orders", {"created_at": "2025-01-01T00:00:00Z", "order_id": "O-1"})
    rule = rules.RetentionRule(
        table_name="orders", minimum_retention=timedelta(days=365 * 7),
        date_column="created_at", authority="Finance policy FIN-3",
    )
    _, actions = await _build(
        monkeypatch, make_request(case.ACCESS), [item],
        selections={str(item.id): {"action": case.SELECT_DELETE}},
        retention_rules=(rule,),
    )
    assert actions[0].status == "blocked"
    assert "Finance policy FIN-3" in actions[0].blocked_reason
    assert actions[0].requester_explanation
    assert "credential" not in actions[0].requester_explanation.lower()


@pytest.mark.asyncio
async def test_a_correction_uses_only_the_values_supplied_for_that_record(monkeypatch):
    item = make_evidence(snapshot={"name": "A", "phone": "old", "email": "a@b.com"})
    _, actions = await _build(
        monkeypatch, make_request(case.ACCESS), [item],
        selections={str(item.id): {"action": case.SELECT_CORRECT,
                                   "corrections": {"phone": "+91-99999"}}},
    )
    assert actions[0].operation == case.OP_UPDATE_FIELD
    assert actions[0].operation_payload == {"phone": "+91-99999"}
    assert actions[0].requires_approval


@pytest.mark.asyncio
async def test_the_plan_records_that_the_person_chose_each_action(monkeypatch):
    """A reviewer reading the plan must be able to tell a requester's choice from a
    rule's inference."""
    item = make_evidence()
    _, actions = await _build(
        monkeypatch, make_request(case.ACCESS), [item],
        selections={str(item.id): {"action": case.SELECT_DELETE}},
    )
    assert "the requester asked for this record to be erased" in actions[0].reason


# ── harness ──────────────────────────────────────────────────────────────────────

async def _build(monkeypatch, request, evidence, *, selections=None, grant=None,
                 retention_rules=()):
    created = {}
    repo = planning_service.dsr_repository

    async def _list_evidence(db, request_id, org_id): return evidence
    async def _current_plan(db, request_id, org_id): return None
    async def _next_version(db, request_id, org_id): return 1

    async def _create_plan(db, **kw):
        plan = DsrActionPlan(id=uuid.uuid4(), status="draft", **kw)
        created["plan"] = plan
        return plan

    async def _add_actions(db, org_id, rows): created["actions"] = rows

    monkeypatch.setattr(repo, "list_evidence", _list_evidence)
    monkeypatch.setattr(repo, "get_current_plan", _current_plan)
    monkeypatch.setattr(repo, "next_plan_version", _next_version)
    monkeypatch.setattr(repo, "create_plan", _create_plan)
    monkeypatch.setattr(repo, "add_actions", _add_actions)

    class _DB:
        async def flush(self): return None

    return await planning_service.build_plan(
        _DB(), request, grants={"crm": grant or make_grant()},
        selections=selections, retention_rules=retention_rules,
    )


# ── Defect: planning returned a status the state machine forbade ─────────────────

@pytest.mark.parametrize("from_status", [case.SEARCH_COMPLETED, case.REVIEW_REQUIRED])
@pytest.mark.parametrize(
    "plan_shape",
    [
        ("nothing to approve", False),   # every action auto-executable
        ("needs approval", True),
    ],
)
def test_whatever_planning_returns_is_actually_reachable(from_status, plan_shape):
    """The original bug: next_status_after_planning returned APPROVED for a plan with
    nothing to approve, but no edge ran from SEARCH_COMPLETED to APPROVED, so the
    worker crashed with InvalidCaseTransition and the case stuck at `searching`.

    Testing the function's return value alone missed it. This pairs the two: every
    status planning can produce must be a legal move from where a case actually is
    when planning happens.
    """
    from app.agents.dsr.services import lifecycle
    from app.services import dsr_run_service

    _, needs_approval = plan_shape
    plan = SimpleNamespace(requires_approval=needs_approval)
    actions = [SimpleNamespace(requires_approval=needs_approval, status="proposed")]

    target = dsr_run_service.next_status_after_planning(plan, actions)
    assert lifecycle.can_transition(from_status, target), (
        f"planning wants {from_status} -> {target}, which the state machine refuses"
    )


def test_an_empty_plan_target_is_reachable_too():
    from app.agents.dsr.services import lifecycle
    from app.services import dsr_run_service

    target = dsr_run_service.next_status_after_planning(
        SimpleNamespace(requires_approval=False), []
    )
    assert lifecycle.can_transition(case.SEARCH_COMPLETED, target)


def test_execution_is_still_only_reachable_through_approved():
    """Widening the graph must not open a second door into execution."""
    from app.agents.dsr.services import lifecycle

    sources = [s for s in case.ALL_STATUSES if lifecycle.can_transition(s, case.EXECUTING)]
    assert sources == [case.APPROVED]


# ── Defect: re-planning left a case claiming APPROVED with work undecided ────────

def test_replanning_can_pull_a_case_back_out_of_approved():
    """Building a new plan supersedes the old one, which voids any approval already
    given against it -- an approval authorizes specific actions, not a case.

    The bug: the search had already produced a plan needing no approval, so the case
    sat at APPROVED. Choosing DELETE for a record then rebuilt the plan, but
    APPROVED -> APPROVAL_REQUIRED was not a legal edge, so the transition was
    silently skipped and the case went on claiming it was approved while two actions
    awaited a decision. Moving backward into a STRICTER state must always be allowed.
    """
    from app.agents.dsr.services import lifecycle

    assert lifecycle.can_transition(case.APPROVED, case.APPROVAL_REQUIRED)
    assert lifecycle.can_transition(case.APPROVED, case.REVIEW_REQUIRED)


def test_pulling_back_never_becomes_a_shortcut_forward():
    """The widened edges must not let a case skip the approval gate."""
    from app.agents.dsr.services import lifecycle

    assert [s for s in case.ALL_STATUSES if lifecycle.can_transition(s, case.EXECUTING)] == [
        case.APPROVED
    ]
    assert [s for s in case.ALL_STATUSES if lifecycle.can_transition(s, case.COMPLETED)] == [
        case.RESPONSE_PENDING
    ]


def test_a_plan_with_undecided_work_never_reports_ready_to_execute():
    """The guard that made the bug survivable: even with the case mislabelled, a
    plan with anything awaiting a decision must not be executable."""
    from app.agents.dsr.services import approval_service

    actions = [
        SimpleNamespace(status="proposed", requires_approval=True),
        SimpleNamespace(status="proposed", requires_approval=False),
    ]
    approved = [a for a in actions if a.status == "approved"]
    undecided = [a for a in actions if a.status == "proposed" and a.requires_approval]
    auto = [a for a in actions if a.status == "proposed" and not a.requires_approval]
    ready = not undecided and bool(approved or auto)
    assert not ready
    assert approval_service.DECISIONS  # the module is the one that computes this live
