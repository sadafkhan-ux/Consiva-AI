"""Constraint evaluation and action planning (prompt §20, §21).

The properties under test are the ones that make a partial fulfilment honest: no
evidence row is ever dropped, a blocked action stays visible with its reason, and
the engine never asserts a legal requirement of its own.
"""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.agents.dsr.connectors import authorization
from app.agents.dsr.rules import constraints as rules
from app.agents.dsr.schemas import case
from app.agents.dsr.services import planning_service
from app.db.models import DsrEvidence, DsrRequest

NOW = datetime(2026, 9, 12, tzinfo=UTC)
ORG = uuid.uuid4()


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


def make_request(request_type=case.DELETION) -> DsrRequest:
    return DsrRequest(
        id=uuid.uuid4(), org_id=ORG, reference="DSR-PLAN01",
        raw_request="delete my data", request_type=request_type,
        due_at=NOW + timedelta(days=30), requester_email="a@b.com", status=case.SEARCH_COMPLETED,
    )


def make_evidence(table="customers", snapshot=None, source="crm") -> DsrEvidence:
    return DsrEvidence(
        id=uuid.uuid4(), org_id=ORG, request_id=uuid.uuid4(), search_run_id=uuid.uuid4(),
        source_name=source, table_name=table, matched_column="email", identifier_kind="email",
        match_type="normalized_exact", confidence=1.0, record_reference={"id": 7},
        record_snapshot=snapshot if snapshot is not None else {"name": "A", "email": "a@b.com"},
    )


# ── System fact vs policy interpretation stay separate ───────────────────────────

def test_unauthorized_execution_is_a_system_fact_not_a_policy_claim():
    found = rules.evaluate(
        operation=case.OP_DELETE_RECORD, table_name="customers",
        grant=make_grant(allow_execution=False), now=NOW,
    )
    blocker = next(c for c in found if c.effect == rules.EFFECT_BLOCK)
    assert blocker.kind == rules.KIND_SYSTEM
    assert blocker.code == case.ERR_SOURCE_NOT_AUTHORIZED


def test_retention_block_is_attributed_to_whoever_configured_it():
    """The engine never says 'the law requires this'. It says whose rule it was."""
    rule = rules.RetentionRule(
        table_name="invoices", minimum_retention=timedelta(days=365 * 7),
        date_column="created_at", authority="Finance policy FIN-3",
    )
    found = rules.evaluate(
        operation=case.OP_DELETE_RECORD, table_name="invoices", grant=make_grant(),
        record_snapshot={"created_at": "2024-01-01T00:00:00Z"},
        retention_rules=(rule,), now=NOW,
    )
    blocker = next(c for c in found if c.effect == rules.EFFECT_BLOCK)
    assert blocker.kind == rules.KIND_POLICY
    assert "Finance policy FIN-3" in blocker.reason
    assert blocker.source == "retention_policy"


def test_elapsed_retention_allows_and_records_why():
    rule = rules.RetentionRule(
        table_name="invoices", minimum_retention=timedelta(days=30),
        date_column="created_at", authority="Finance policy FIN-3",
    )
    found = rules.evaluate(
        operation=case.OP_DELETE_RECORD, table_name="invoices", grant=make_grant(),
        record_snapshot={"created_at": "2020-01-01T00:00:00Z"},
        retention_rules=(rule,), now=NOW,
    )
    assert any(c.code == "RETENTION_SATISFIED" and c.effect == rules.EFFECT_ALLOW for c in found)


@pytest.mark.parametrize("snapshot", [{}, {"created_at": None}, {"created_at": "not-a-date"}])
def test_unreadable_retention_date_reviews_rather_than_guessing(snapshot):
    """Allowing risks deleting something the org said to keep; blocking risks
    refusing a lawful erasure on a technicality. A human decides."""
    rule = rules.RetentionRule(
        table_name="invoices", minimum_retention=timedelta(days=365),
        date_column="created_at", authority="Finance policy FIN-3",
    )
    found = rules.evaluate(
        operation=case.OP_DELETE_RECORD, table_name="invoices", grant=make_grant(),
        record_snapshot=snapshot, retention_rules=(rule,), now=NOW,
    )
    review = next(c for c in found if c.code == case.ERR_POLICY_REVIEW_REQUIRED)
    assert review.effect == rules.EFFECT_REVIEW


def test_one_block_beats_any_number_of_allows():
    """Not a score: a retention requirement is not outweighed by three passes."""
    found = (
        rules.Constraint("A", rules.KIND_SYSTEM, rules.EFFECT_ALLOW, "ok", "x"),
        rules.Constraint("B", rules.KIND_SYSTEM, rules.EFFECT_ALLOW, "ok", "x"),
        rules.Constraint("C", rules.KIND_POLICY, rules.EFFECT_BLOCK, "no", "x"),
        rules.Constraint("D", rules.KIND_SYSTEM, rules.EFFECT_ALLOW, "ok", "x"),
    )
    assert rules.verdict(found) == rules.EFFECT_BLOCK


def test_every_blocking_reason_is_reported_not_just_the_first():
    found = (
        rules.Constraint("A", rules.KIND_SYSTEM, rules.EFFECT_BLOCK, "first reason", "x"),
        rules.Constraint("B", rules.KIND_POLICY, rules.EFFECT_BLOCK, "second reason", "x"),
    )
    reason = rules.blocking_reason(found)
    assert "first reason" in reason and "second reason" in reason


def test_deletion_always_requires_human_review():
    found = rules.evaluate(
        operation=case.OP_DELETE_RECORD, table_name="customers", grant=make_grant(), now=NOW
    )
    assert any(c.code == "HIGH_IMPACT" and c.effect == rules.EFFECT_REVIEW for c in found)


def test_disclosure_needs_no_execution_rights():
    found = rules.evaluate(
        operation=case.OP_DISCLOSE, table_name="customers",
        grant=make_grant(allow_execution=False), now=NOW,
    )
    assert rules.verdict(found) == rules.EFFECT_ALLOW


# ── Planning: nothing is ever dropped ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_every_evidence_row_produces_exactly_one_action(monkeypatch):
    """An action that vanished from the plan is a record the requester was never
    told about (§47)."""
    evidence = [make_evidence(), make_evidence(table="invoices"), make_evidence()]
    _, actions = await _build(monkeypatch, make_request(), evidence, {"crm": make_grant()})
    assert len(actions) == len(evidence)
    assert {a.evidence_id for a in actions} == {e.id for e in evidence}


@pytest.mark.asyncio
async def test_blocked_action_stays_in_the_plan_with_its_reason(monkeypatch):
    """This is what lets a response say 'we deleted four and kept one, because...'"""
    rule = rules.RetentionRule(
        table_name="invoices", minimum_retention=timedelta(days=365 * 7),
        date_column="created_at", authority="Finance policy FIN-3",
    )
    evidence = [
        make_evidence(table="customers"),
        make_evidence(table="invoices", snapshot={"created_at": "2025-01-01T00:00:00Z", "amount": 10}),
    ]
    plan, actions = await _build(
        monkeypatch, make_request(case.DELETION), evidence, {"crm": make_grant()},
        retention_rules=(rule,),
    )
    blocked = [a for a in actions if a.status == "blocked"]
    assert len(blocked) == 1
    assert blocked[0].table_name == "invoices"
    assert "Finance policy FIN-3" in blocked[0].blocked_reason
    assert blocked[0].operation == case.OP_RETAIN
    assert "blocked" in plan.summary.lower()


@pytest.mark.asyncio
async def test_a_deletion_plan_never_produces_an_executed_action(monkeypatch):
    """Planning proposes. Nothing in this module executes anything."""
    plan, actions = await _build(
        monkeypatch, make_request(case.DELETION), [make_evidence()], {"crm": make_grant()}
    )
    assert all(a.status in ("proposed", "blocked") for a in actions)
    assert plan.status == "draft"


@pytest.mark.asyncio
async def test_deletion_actions_require_approval(monkeypatch):
    _, actions = await _build(
        monkeypatch, make_request(case.DELETION), [make_evidence()], {"crm": make_grant()}
    )
    assert all(a.requires_approval for a in actions)
    assert actions[0].risk == "high"


@pytest.mark.asyncio
async def test_access_disclosure_does_not_require_approval(monkeypatch):
    """Reading a record to answer an access request is the request being fulfilled,
    not a change that needs reviewing."""
    _, actions = await _build(
        monkeypatch, make_request(case.ACCESS), [make_evidence()], {"crm": make_grant()}
    )
    assert actions[0].operation == case.OP_DISCLOSE
    assert not actions[0].requires_approval
    assert actions[0].risk == "low"


@pytest.mark.asyncio
async def test_evidence_from_a_deauthorized_source_still_produces_an_action(monkeypatch):
    """The source lost its authorization between search and planning. The record is
    still reported; it just cannot be acted on."""
    _, actions = await _build(
        monkeypatch, make_request(case.DELETION), [make_evidence(source="gone")], grants={}
    )
    assert len(actions) == 1
    assert actions[0].status == "blocked"
    assert actions[0].operation == case.OP_RETAIN
    assert "not currently authorized" in actions[0].blocked_reason


@pytest.mark.asyncio
async def test_correction_only_touches_columns_the_case_asked_for(monkeypatch):
    """Guessing what someone meant to change is not something a correction may do."""
    evidence = [make_evidence(snapshot={"name": "A", "phone": "old", "email": "a@b.com"})]
    _, actions = await _build(
        monkeypatch, make_request(case.CORRECTION), evidence, {"crm": make_grant()},
        corrections={"phone": "+91-99999", "salary": 1},  # salary was never asked for
    )
    assert actions[0].operation == case.OP_UPDATE_FIELD
    assert actions[0].operation_payload == {"phone": "+91-99999"}


@pytest.mark.asyncio
async def test_correction_to_an_unwritable_column_is_blocked(monkeypatch):
    evidence = [make_evidence(snapshot={"account_balance": 100, "email": "a@b.com"})]
    _, actions = await _build(
        monkeypatch, make_request(case.CORRECTION), evidence,
        {"crm": make_grant(erasable_columns={"customers": ["phone"]})},
        corrections={"account_balance": 0},
    )
    assert actions[0].status == "blocked"
    assert "allowlist" in actions[0].blocked_reason


@pytest.mark.asyncio
async def test_empty_evidence_yields_a_plan_that_says_so(monkeypatch):
    """No match is an outcome with a response, not an empty object."""
    plan, actions = await _build(monkeypatch, make_request(case.ACCESS), [], {"crm": make_grant()})
    assert actions == []
    assert "no record matched" in plan.summary.lower()


@pytest.mark.asyncio
async def test_every_action_carries_a_reason_and_an_expected_result(monkeypatch):
    """§21: an action a reviewer cannot evaluate is not reviewable."""
    evidence = [make_evidence(), make_evidence(table="invoices")]
    for request_type in (case.ACCESS, case.DELETION, case.CORRECTION):
        _, actions = await _build(
            monkeypatch, make_request(request_type), evidence, {"crm": make_grant()},
            corrections={"phone": "x"},
        )
        for action in actions:
            assert action.reason.strip()
            assert action.expected_result.strip()
            assert action.risk in ("low", "medium", "high")


# ── harness ──────────────────────────────────────────────────────────────────────

async def _build(monkeypatch, request, evidence, grants, *, corrections=None, retention_rules=()):
    """Drive build_plan against in-memory repository stubs."""
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

    async def _add_actions(db, rows):
        created["actions"] = rows

    repo = planning_service.dsr_repository
    monkeypatch.setattr(repo, "list_evidence", _list_evidence)
    monkeypatch.setattr(repo, "get_current_plan", _current_plan)
    monkeypatch.setattr(repo, "next_plan_version", _next_version)
    monkeypatch.setattr(repo, "create_plan", _create_plan)
    monkeypatch.setattr(repo, "add_actions", _add_actions)

    class _DB:
        async def flush(self):
            return None

    return await planning_service.build_plan(
        _DB(), request, grants=grants, corrections=corrections,
        retention_rules=retention_rules,
    )
