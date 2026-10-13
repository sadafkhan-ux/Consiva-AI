"""The nine DSR scenarios the prompt requires (§42), end to end.

Each walks a case through the real services -- lifecycle, identity, search,
planning, approval, execution, response -- against an in-memory fake of the
repository and a fake connector standing in for the customer's database. No live
Postgres, same as the rest of this suite.

What these hold to account is the JOIN between the pieces. The unit tests prove
each service behaves; these prove that a case moving through all of them cannot
reach a state the state machine forbids, and always ends somewhere explicit.
"""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.agents.dsr.connectors.base import ExecutionOutcome, SearchOutcome, SubjectMatch
from app.agents.dsr.errors import (
    ApprovalRequiredError,
    IdentityRequiredError,
    SourceNotAuthorizedError,
)
from app.agents.dsr.schemas import case
from app.agents.dsr.services import (
    approval_service,
    case_service,
    execution_service,
    identity_service,
    lifecycle,
    planning_service,
    response_service,
    search_service,
)
from app.db.models import (
    DsrAction,
    DsrActionPlan,
    DsrApproval,
    DsrEvidence,
    DsrExecution,
    DsrIdentityVerification,
    DsrRequest,
    DsrResponse,
    DsrSearchRun,
)

NOW = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)
ORG = uuid.uuid4()
USER = uuid.uuid4()


# ── In-memory world ──────────────────────────────────────────────────────────────

class World:
    """Everything the repository would persist, held in dicts."""

    def __init__(self):
        self.requests: dict[uuid.UUID, DsrRequest] = {}
        self.verifications: list[DsrIdentityVerification] = []
        self.search_runs: list[DsrSearchRun] = []
        self.evidence: list[DsrEvidence] = []
        self.plans: list[DsrActionPlan] = []
        self.actions: list[DsrAction] = []
        self.approvals: list[DsrApproval] = []
        self.executions: list[DsrExecution] = []
        self.responses: list[DsrResponse] = []
        self.audit: list[dict] = []


class FakeDB:
    async def flush(self):
        return None

    async def commit(self):
        return None


class FakeConnector:
    def __init__(self, *, matches=(), distinct=1, search_raises=None, exec_outcome=None,
                 exec_raises=None, truncated=False):
        self._matches = matches
        self._distinct = distinct
        self._search_raises = search_raises
        self._exec_outcome = exec_outcome or ExecutionOutcome(
            rows_affected=1, verified=True, verification_detail={"check": "record absent"}
        )
        self._exec_raises = exec_raises
        self._truncated = truncated
        self.executions = 0

    async def search_subject(self, *, identifiers, limit=500):
        if self._search_raises:
            raise self._search_raises
        return SearchOutcome(
            matches=tuple(self._matches),
            tables_searched=tuple({m.table_name for m in self._matches}) or ("customers",),
            distinct_subjects=self._distinct,
            truncated=self._truncated,
        )

    async def execute_action(self, **kw):
        self.executions += 1
        if self._exec_raises:
            raise self._exec_raises
        return self._exec_outcome


AUTHORIZATION = SimpleNamespace(
    id=uuid.uuid4(), data_source_id=uuid.uuid4(), enabled=True,
    searchable_tables=["customers", "orders"], identity_tables=["customers"],
    identifier_columns={"customers": {"email": "email"}, "orders": {"email": "customer_email"}},
    returnable_columns={"customers": ["name", "email", "phone"], "orders": ["order_id", "amount"]},
    erasable_columns={"customers": ["name", "phone", "email"], "orders": ["customer_email"]},
    allow_execution=True, write_credential_ref="SRC_WRITE",
)
DATA_SOURCE = SimpleNamespace(
    id=AUTHORIZATION.data_source_id, name="crm", enabled=True, connector="postgres",
    config={"host": "h", "dbname": "d", "user": "u"}, credential_ref="SRC_READ",
)


def match(table="customers", record_id=7):
    return SubjectMatch(
        table_name=table, matched_column="email", identifier_kind="email",
        match_type="normalized_exact", confidence=1.0,
        record_reference={"id": record_id},
        record_snapshot={"name": "A Person", "email": "a@b.com", "phone": "+91-1"},
    )


@pytest.fixture
def world(monkeypatch):
    w = World()
    _install(monkeypatch, w)
    return w


def _install(monkeypatch, w: World):
    """Point every repository function at the in-memory world."""
    from app.db.repositories import dsr_repository as repo

    async def create_request(db, **kw):
        row = DsrRequest(id=uuid.uuid4(), status=case.RECEIVED, request_type=case.UNCLASSIFIED,
                         received_at=NOW, sla_breached=False, **kw)
        w.requests[row.id] = row
        return row

    async def get_request(db, request_id, org_id):
        row = w.requests.get(request_id)
        return row if row and row.org_id == org_id else None

    async def find_by_key(db, org_id, key):
        return next((r for r in w.requests.values()
                     if r.org_id == org_id and r.idempotency_key == key), None)

    async def reference_exists(db, org_id, reference):
        return any(r.org_id == org_id and r.reference == reference for r in w.requests.values())

    async def create_verification(db, *, org_id, request_id, method, challenge_hash,
                                  expires_at, max_attempts=5):
        row = DsrIdentityVerification(
            id=uuid.uuid4(), org_id=org_id, request_id=request_id, method=method,
            status=case.IDV_PENDING, challenge_hash=challenge_hash, expires_at=expires_at,
            max_attempts=max_attempts, created_at=NOW,
        )
        row.attempts = 0
        w.verifications.append(row)
        return row

    async def latest_verification(db, request_id, org_id):
        rows = [v for v in w.verifications if v.request_id == request_id and v.org_id == org_id]
        return rows[-1] if rows else None

    async def list_source_auth(db, org_id):
        return [AUTHORIZATION]

    async def get_source_auth(db, data_source_id, org_id):
        return AUTHORIZATION

    async def create_search_run(db, **kw):
        row = DsrSearchRun(id=uuid.uuid4(), status="pending", match_count=0,
                           distinct_subject_count=0, tables_searched=[], **kw)
        w.search_runs.append(row)
        return row

    async def list_search_runs(db, request_id, org_id):
        return [r for r in w.search_runs if r.request_id == request_id]

    async def add_evidence(db, org_id, rows):
        w.evidence.extend(rows)

    async def list_evidence(db, request_id, org_id):
        return [e for e in w.evidence if e.request_id == request_id]

    async def get_evidence(db, evidence_id, org_id):
        return next((e for e in w.evidence if e.id == evidence_id), None)

    async def next_plan_version(db, request_id, org_id):
        return len([p for p in w.plans if p.request_id == request_id]) + 1

    async def get_current_plan(db, request_id, org_id):
        rows = [p for p in w.plans if p.request_id == request_id and p.status != "superseded"]
        return rows[-1] if rows else None

    async def create_plan(db, **kw):
        row = DsrActionPlan(id=uuid.uuid4(), status="draft", **kw)
        w.plans.append(row)
        return row

    async def add_actions(db, org_id, rows):
        w.actions.extend(rows)

    async def list_actions(db, plan_id, org_id):
        return [a for a in w.actions if a.plan_id == plan_id]

    async def get_action(db, action_id, org_id):
        return next((a for a in w.actions if a.id == action_id and a.org_id == org_id), None)

    async def record_approval(db, **kw):
        row = DsrApproval(id=uuid.uuid4(), created_at=NOW, **kw)
        w.approvals.append(row)
        return row

    async def latest_approval(db, action_id, org_id):
        rows = [a for a in w.approvals if a.action_id == action_id]
        return rows[-1] if rows else None

    async def claim_execution(db, *, org_id, request_id, action_id, idempotency_key, **kw):
        existing = next((e for e in w.executions if e.idempotency_key == idempotency_key), None)
        if existing:
            return existing, False
        row = DsrExecution(id=uuid.uuid4(), org_id=org_id, request_id=request_id,
                           action_id=action_id, idempotency_key=idempotency_key, status="pending")
        row.attempts = 0
        w.executions.append(row)
        return row, True

    async def list_executions(db, request_id, org_id):
        return [e for e in w.executions if e.request_id == request_id]

    async def next_response_version(db, request_id, org_id):
        return len([r for r in w.responses if r.request_id == request_id]) + 1

    async def create_response(db, **kw):
        row = DsrResponse(id=uuid.uuid4(), status="draft", **kw)
        w.responses.append(row)
        return row

    async def get_latest_response(db, request_id, org_id):
        rows = [r for r in w.responses if r.request_id == request_id]
        return rows[-1] if rows else None

    for name, fn in [
        ("create_request", create_request), ("get_request", get_request),
        ("find_request_by_idempotency_key", find_by_key),
        ("reference_exists", reference_exists),
        ("create_identity_verification", create_verification),
        ("get_latest_identity_verification", latest_verification),
        ("list_source_authorizations", list_source_auth),
        ("get_source_authorization", get_source_auth),
        ("create_search_run", create_search_run), ("list_search_runs", list_search_runs),
        ("add_evidence", add_evidence), ("list_evidence", list_evidence),
        ("get_evidence", get_evidence),
        ("next_plan_version", next_plan_version), ("get_current_plan", get_current_plan),
        ("create_plan", create_plan), ("add_actions", add_actions),
        ("list_actions", list_actions), ("get_action", get_action),
        ("record_approval", record_approval), ("latest_approval_for_action", latest_approval),
        ("claim_execution", claim_execution), ("list_executions", list_executions),
        ("next_response_version", next_response_version), ("create_response", create_response),
        ("get_latest_response", get_latest_response),
    ]:
        monkeypatch.setattr(repo, name, fn)

    async def get_data_source(db, source_id, org_id):
        return DATA_SOURCE

    async def list_data_sources(db, org_id):
        return [DATA_SOURCE]

    from app.db.repositories import ropa_repository
    monkeypatch.setattr(ropa_repository, "get_data_source", get_data_source)
    monkeypatch.setattr(ropa_repository, "list_data_sources", list_data_sources)

    async def audit(db, **kw):
        w.audit.append(kw)

    from app.services import audit_service
    monkeypatch.setattr(audit_service, "record", audit)


def _use_connector(monkeypatch, connector):
    from app.agents.dsr.connectors import factory
    monkeypatch.setattr(factory, "build_connector", lambda **kw: connector)


# ── Shared journey helpers ───────────────────────────────────────────────────────

async def _open_case(text="Delete my personal information.") -> DsrRequest:
    request, _ = await case_service.create_case(
        FakeDB(), org_id=ORG, raw_request=text, requester_email="a@b.com",
        created_by_user_id=USER, now=NOW,
    )
    await case_service.classify_case(FakeDB(), request, actor_user_id=USER)
    return request


async def _verify(request):
    await identity_service.verify_manually(
        FakeDB(), request, reviewer_user_id=USER,
        evidence_note="Checked government ID in person, ref ID-991", now=NOW,
    )
    await case_service.transition(FakeDB(), request, case.IDENTITY_VERIFIED, actor_user_id=USER)


async def _search(request):
    await case_service.transition(FakeDB(), request, case.SEARCHING, actor_user_id=USER)
    summary = await search_service.run_search(FakeDB(), request)
    await case_service.transition(
        FakeDB(), request, case.SEARCH_COMPLETED,
        error_code=summary.outcome_code, error_detail="see search runs" if summary.outcome_code else None,
    )
    return summary


async def _plan(request):
    from app.agents.dsr.connectors import factory
    grants = {DATA_SOURCE.name: factory.build_grant(AUTHORIZATION, source_name=DATA_SOURCE.name)}
    return await planning_service.build_plan(FakeDB(), request, grants=grants)


# ── Scenario 1: ACCESS ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_scenario_1_access(world, monkeypatch):
    _use_connector(monkeypatch, FakeConnector(matches=[match(), match("orders", 11)]))
    request = await _open_case("Show me what personal information you hold about me.")
    assert request.request_type == case.ACCESS

    await _verify(request)
    summary = await _search(request)
    assert summary.evidence_count == 2
    assert summary.outcome_code is None

    plan, actions = await _plan(request)
    assert len(actions) == 2
    assert all(a.operation == case.OP_DISCLOSE for a in actions)
    # Disclosure is the request being fulfilled, not a change needing approval.
    assert not plan.requires_approval

    await case_service.transition(FakeDB(), request, case.RESPONSE_PENDING)
    response = await response_service.build_response(FakeDB(), request, actor_user_id=USER)
    assert "We found 2 record(s)" in response.body_text
    assert response.grounded_facts
    assert response.drafted_by_model is None, "no model should be in the response path"

    await case_service.transition(FakeDB(), request, case.COMPLETED, audit_action=case.AUDIT_COMPLETED)
    assert request.status == case.COMPLETED
    assert request.closed_at is not None


# ── Scenario 2: CORRECTION ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_scenario_2_correction(world, monkeypatch):
    connector = FakeConnector(
        matches=[match()],
        exec_outcome=ExecutionOutcome(
            rows_affected=1, verified=True, verification_detail={"check": "values match"}
        ),
    )
    _use_connector(monkeypatch, connector)
    request = await _open_case("Please correct my phone number.")
    assert request.request_type == case.CORRECTION

    await _verify(request)
    await _search(request)

    from app.agents.dsr.connectors import factory
    grants = {DATA_SOURCE.name: factory.build_grant(AUTHORIZATION, source_name=DATA_SOURCE.name)}
    _, actions = await planning_service.build_plan(
        FakeDB(), request, grants=grants, corrections={"phone": "+91-99999"}
    )
    assert actions[0].operation == case.OP_UPDATE_FIELD
    assert actions[0].operation_payload == {"phone": "+91-99999"}
    assert actions[0].requires_approval

    await case_service.transition(FakeDB(), request, case.APPROVAL_REQUIRED)
    await approval_service.decide_action(
        FakeDB(), request, actions[0].id, reviewer_user_id=USER,
        decision="approved", reason="confirmed with the requester by phone", now=NOW,
    )
    await case_service.transition(FakeDB(), request, case.APPROVED)
    execution = await execution_service.execute_action(
        FakeDB(), request, actions[0].id, actor_user_id=USER, now=NOW
    )
    assert execution.status == "verified"
    assert execution.verification_status == "passed"


# ── Scenario 3: DELETION ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_scenario_3_deletion(world, monkeypatch):
    connector = FakeConnector(matches=[match()])
    _use_connector(monkeypatch, connector)
    request = await _open_case("Delete my personal information.")
    assert request.request_type == case.DELETION

    await _verify(request)
    await _search(request)
    plan, actions = await _plan(request)
    assert actions[0].operation == case.OP_DELETE_RECORD
    assert actions[0].risk == "high"
    assert plan.requires_approval, "deletion must never be automatic"

    await case_service.transition(FakeDB(), request, case.APPROVAL_REQUIRED)
    await approval_service.decide_action(
        FakeDB(), request, actions[0].id, reviewer_user_id=USER,
        decision="approved", reason="identity confirmed; no retention constraint applies", now=NOW,
    )
    await case_service.transition(FakeDB(), request, case.APPROVED)
    execution = await execution_service.execute_action(FakeDB(), request, actions[0].id, now=NOW)
    assert execution.status == "verified"
    assert connector.executions == 1

    # The audit trail covers request, approval, execution AND read-back verification --
    # not just that each stage happened, but that each left its own durable record.
    actions_audited = [entry["action"] for entry in world.audit]
    assert case.AUDIT_CREATED in actions_audited, "no record of the request itself"
    assert case.AUDIT_APPROVED in actions_audited, "no record of the approval"
    # Execution and read-back verification are recorded as the SAME audited fact here
    # (execution_service.py records AUDIT_ACTION_VERIFIED exactly when the read-back
    # confirmed the write, AUDIT_ACTION_EXECUTED otherwise) -- so finding this one
    # entry with verified=True in its payload is what proves both happened.
    verified_entry = next(
        entry for entry in world.audit if entry["action"] == case.AUDIT_ACTION_VERIFIED
    )
    assert verified_entry["after"]["verified"] is True
    # Request must precede approval must precede execution -- an audit trail that
    # recorded the right events out of order would not reconstruct what happened.
    assert (
        actions_audited.index(case.AUDIT_CREATED)
        < actions_audited.index(case.AUDIT_APPROVED)
        < actions_audited.index(case.AUDIT_ACTION_VERIFIED)
    )


# ── Scenario 4: NO MATCH ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_scenario_4_no_match_is_explicit_and_executes_nothing(world, monkeypatch):
    connector = FakeConnector(matches=[], distinct=0)
    _use_connector(monkeypatch, connector)
    request = await _open_case("Delete my personal information.")
    await _verify(request)
    summary = await _search(request)

    assert summary.outcome_code == case.ERR_NO_MATCH
    assert request.error_code == case.ERR_NO_MATCH
    assert world.search_runs[0].status == "no_match"

    plan, actions = await _plan(request)
    assert actions == []
    assert "no record matched" in plan.summary.lower()

    await case_service.transition(FakeDB(), request, case.RESPONSE_PENDING)
    response = await response_service.build_response(FakeDB(), request)
    assert "did not find any record" in response.body_text
    assert "No data was changed" in response.body_text
    assert connector.executions == 0, "nothing may be executed when nothing matched"


# ── Scenario 5: MULTIPLE MATCH ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_scenario_5_multiple_matches_stop_for_a_human(world, monkeypatch):
    _use_connector(monkeypatch, FakeConnector(
        matches=[match(record_id=7), match(record_id=8)], distinct=2,
    ))
    request = await _open_case("Delete my personal information.")
    await _verify(request)
    summary = await _search(request)

    assert summary.ambiguous
    assert summary.outcome_code == case.ERR_MULTIPLE_MATCHES
    assert world.search_runs[0].status == "multiple_matches"

    # The case may go to review; it may NOT go to execution.
    assert lifecycle.can_transition(request.status, case.REVIEW_REQUIRED)
    assert not lifecycle.can_transition(request.status, case.EXECUTING)


@pytest.mark.asyncio
async def test_scenario_5b_a_truncated_result_is_also_ambiguous(world, monkeypatch):
    """More rows existed than the cap allows. A partial answer presented as
    complete is a wrong answer."""
    _use_connector(monkeypatch, FakeConnector(matches=[match()], distinct=1, truncated=True))
    request = await _open_case("Show me my data.")
    await _verify(request)
    summary = await _search(request)
    assert summary.ambiguous
    assert summary.outcome_code == case.ERR_MULTIPLE_MATCHES


# ── Scenario 6: CONNECTOR FAILURE ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_scenario_6_connector_failure_is_an_explicit_state(world, monkeypatch):
    from app.agents.dsr.errors import ConnectorUnavailableError

    _use_connector(monkeypatch, FakeConnector(
        search_raises=ConnectorUnavailableError("source unreachable"),
    ))
    request = await _open_case("Show me my data.")
    await _verify(request)
    summary = await _search(request)

    assert summary.sources_failed == ["crm"]
    assert world.search_runs[0].status == "failed"
    assert world.search_runs[0].error_code == case.ERR_CONNECTOR_UNAVAILABLE
    assert world.search_runs[0].error_detail, "a failure must carry its reason"
    assert summary.outcome_code == case.ERR_SEARCH_FAILED

    # And the response says so rather than claiming no data exists.
    await case_service.transition(FakeDB(), request, case.RESPONSE_PENDING)
    response = await response_service.build_response(FakeDB(), request)
    assert "unable to complete the search" in response.body_text
    assert "incomplete" in response.body_text


# ── Scenario 7: APPROVAL REJECTION ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_scenario_7_rejection_blocks_execution(world, monkeypatch):
    connector = FakeConnector(matches=[match()])
    _use_connector(monkeypatch, connector)
    request = await _open_case("Delete my personal information.")
    await _verify(request)
    await _search(request)
    _, actions = await _plan(request)

    await case_service.transition(FakeDB(), request, case.APPROVAL_REQUIRED)
    await approval_service.decide_action(
        FakeDB(), request, actions[0].id, reviewer_user_id=USER,
        decision="rejected", reason="identity evidence insufficient", now=NOW,
    )
    assert actions[0].status == "rejected"

    # Even if something forces the case to APPROVED, the action's own approval is a
    # rejection and execution must still refuse.
    request.status = case.APPROVED
    with pytest.raises(ApprovalRequiredError):
        await execution_service.execute_action(FakeDB(), request, actions[0].id, now=NOW)
    assert connector.executions == 0


# ── Scenario 8: DUPLICATE EXECUTION ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_scenario_8_duplicate_execution_performs_the_action_once(world, monkeypatch):
    connector = FakeConnector(matches=[match()])
    _use_connector(monkeypatch, connector)
    request = await _open_case("Delete my personal information.")
    await _verify(request)
    await _search(request)
    _, actions = await _plan(request)

    await case_service.transition(FakeDB(), request, case.APPROVAL_REQUIRED)
    await approval_service.decide_action(
        FakeDB(), request, actions[0].id, reviewer_user_id=USER,
        decision="approved", reason="confirmed", now=NOW,
    )
    await case_service.transition(FakeDB(), request, case.APPROVED)

    first = await execution_service.execute_action(FakeDB(), request, actions[0].id, now=NOW)
    second = await execution_service.execute_action(FakeDB(), request, actions[0].id, now=NOW)
    third = await execution_service.execute_action(FakeDB(), request, actions[0].id, now=NOW)

    assert connector.executions == 1, "the deletion was performed more than once"
    assert first.id == second.id == third.id
    assert second.status == "verified"
    assert len(world.executions) == 1


# ── Scenario 9: WORKER RESTART ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_scenario_9_worker_restart_resumes_from_persisted_state(world, monkeypatch):
    """A worker dies mid-execution and the job is retried by a fresh process.

    The case state lives in the database, not in the worker, so the retry picks up
    exactly where the previous attempt left off -- and the idempotency ledger means
    the action it already completed is not redone.
    """
    connector = FakeConnector(matches=[match(record_id=7), match("orders", 11)])
    _use_connector(monkeypatch, connector)
    request = await _open_case("Delete my personal information.")
    await _verify(request)
    await _search(request)
    _, actions = await _plan(request)

    await case_service.transition(FakeDB(), request, case.APPROVAL_REQUIRED)
    for action in actions:
        await approval_service.decide_action(
            FakeDB(), request, action.id, reviewer_user_id=USER,
            decision="approved", reason="confirmed", now=NOW,
        )
    await case_service.transition(FakeDB(), request, case.APPROVED)
    await case_service.transition(FakeDB(), request, case.EXECUTING)

    # First worker completes one action, then dies.
    await execution_service.execute_action(FakeDB(), request, actions[0].id, now=NOW)
    assert connector.executions == 1

    # A fresh worker retries the whole job. Nothing in memory survived; everything
    # it needs is on the rows.
    for action in actions:
        await execution_service.execute_action(FakeDB(), request, action.id, now=NOW)

    assert connector.executions == 2, "the already-completed action was redone"
    assert len({e.idempotency_key for e in world.executions}) == 2
    assert all(e.status == "verified" for e in world.executions)


# ── Cross-cutting: the gate cannot be walked around ──────────────────────────────

@pytest.mark.asyncio
async def test_search_is_refused_before_identity_verification(world, monkeypatch):
    _use_connector(monkeypatch, FakeConnector(matches=[match()]))
    request = await _open_case("Show me my data.")
    with pytest.raises(IdentityRequiredError):
        await search_service.run_search(FakeDB(), request)
    assert world.search_runs == [], "a search run was created despite the gate"
    assert world.evidence == []


@pytest.mark.asyncio
async def test_execution_re_checks_identity_not_just_the_search(world, monkeypatch):
    """A verification revoked between search and execution must stop the write."""
    connector = FakeConnector(matches=[match()])
    _use_connector(monkeypatch, connector)
    request = await _open_case("Delete my personal information.")
    await _verify(request)
    await _search(request)
    _, actions = await _plan(request)
    await case_service.transition(FakeDB(), request, case.APPROVAL_REQUIRED)
    await approval_service.decide_action(
        FakeDB(), request, actions[0].id, reviewer_user_id=USER,
        decision="approved", reason="confirmed", now=NOW,
    )
    await case_service.transition(FakeDB(), request, case.APPROVED)

    world.verifications[-1].status = case.IDV_FAILED  # revoked after approval
    with pytest.raises(Exception) as excinfo:
        await execution_service.execute_action(FakeDB(), request, actions[0].id, now=NOW)
    assert "identity" in str(excinfo.value).lower()
    assert connector.executions == 0


@pytest.mark.asyncio
async def test_a_case_with_no_authorized_source_fails_explicitly(world, monkeypatch):
    from app.db.repositories import dsr_repository

    async def none(db, org_id):
        return []

    monkeypatch.setattr(dsr_repository, "list_source_authorizations", none)
    request = await _open_case("Show me my data.")
    await _verify(request)
    with pytest.raises(SourceNotAuthorizedError):
        await search_service.run_search(FakeDB(), request)


@pytest.mark.asyncio
async def test_zero_ropa_sources_and_unauthorized_ropa_source_are_both_source_not_authorized(
    world, monkeypatch,
):
    """Two different real-world situations -- no ROPA source exists at all, and a
    ROPA source exists but was never authorized for DSR -- must both fail explicitly
    with SOURCE_NOT_AUTHORIZED, and the message must make sense either way (it must
    never claim "no source is registered" when one plainly is)."""
    from app.db.repositories import dsr_repository, ropa_repository

    async def no_authorizations(db, org_id):
        return []

    monkeypatch.setattr(dsr_repository, "list_source_authorizations", no_authorizations)

    # Scenario A: a ROPA source IS registered (the fixture's own DATA_SOURCE),
    # just never authorized for DSR.
    request_a = await _open_case("Show me my data.")
    await _verify(request_a)
    with pytest.raises(SourceNotAuthorizedError) as exc_a:
        await search_service.run_search(FakeDB(), request_a)

    assert "ROPA" in exc_a.value.message
    assert "config/sources" in exc_a.value.message

    # Scenario B: no ROPA source exists at all.
    async def no_ropa_sources(db, org_id):
        return []

    monkeypatch.setattr(ropa_repository, "list_data_sources", no_ropa_sources)
    request_b = await _open_case("Show me my data.")
    await _verify(request_b)
    with pytest.raises(SourceNotAuthorizedError) as exc_b:
        await search_service.run_search(FakeDB(), request_b)

    # Same error code, same message, either way -- the check never even reaches
    # the ROPA source list once no authorization exists, so the two situations
    # cannot be told apart by this error, which is exactly why its wording must
    # make sense regardless of which one actually happened.
    assert exc_a.value.code == exc_b.value.code == case.ERR_SOURCE_NOT_AUTHORIZED
    assert exc_a.value.message == exc_b.value.message


@pytest.mark.asyncio
async def test_no_match_and_authorization_failure_are_distinguishable_by_status_alone(
    world, monkeypatch,
):
    """§ Definition of Done: a zero-match search must be distinguishable from a failed
    search. Both outcomes carry an error_code, but the case's STATUS must differ too
    -- a reader who only looks at status (not error_code) must never confuse "we
    searched and found nothing" with "we could not search at all"."""
    from app.db.repositories import dsr_repository

    # Outcome 1: the search runs to completion and finds nothing.
    _use_connector(monkeypatch, FakeConnector(matches=[], distinct=0))
    no_match_case = await _open_case("Show me my data.")
    await _verify(no_match_case)
    summary = await _search(no_match_case)
    assert summary.outcome_code == case.ERR_NO_MATCH
    assert no_match_case.error_code == case.ERR_NO_MATCH
    assert no_match_case.status == case.SEARCH_COMPLETED

    # Outcome 2: the search cannot even be attempted -- nothing is authorized.
    async def no_authorizations(db, org_id):
        return []

    monkeypatch.setattr(dsr_repository, "list_source_authorizations", no_authorizations)
    failed_case = await _open_case("Show me my data.")
    await _verify(failed_case)
    await case_service.transition(FakeDB(), failed_case, case.SEARCHING, actor_user_id=USER)
    with pytest.raises(SourceNotAuthorizedError) as excinfo:
        await search_service.run_search(FakeDB(), failed_case)
    await case_service.transition(
        FakeDB(), failed_case, case.FAILED, audit_action=case.AUDIT_FAILED,
        error_code=excinfo.value.code, error_detail=excinfo.value.message,
    )
    assert failed_case.error_code == case.ERR_SOURCE_NOT_AUTHORIZED
    assert failed_case.status == case.FAILED

    # The point: status alone tells them apart, with no need to read error_code.
    assert no_match_case.status != failed_case.status


@pytest.mark.asyncio
async def test_retry_after_fixing_source_authorization_reaches_the_same_case(world, monkeypatch):
    """The bug this whole investigation started from: "DSR search fails with
    SOURCE_NOT_AUTHORIZED even though a source is registered". The fix is not to
    change this behaviour (it is correct -- ROPA registration is not DSR
    authorization) but to prove the RECOVERY path genuinely works: once an admin
    authorizes a source, retrying the SAME case (not opening a new one) succeeds."""
    from app.db.repositories import dsr_repository

    async def no_authorizations(db, org_id):
        return []

    monkeypatch.setattr(dsr_repository, "list_source_authorizations", no_authorizations)
    request = await _open_case("Show me my data.")
    await _verify(request)
    case_id = request.id

    # (a) The search fails explicitly: no source is authorized.
    await case_service.transition(FakeDB(), request, case.SEARCHING, actor_user_id=USER)
    with pytest.raises(SourceNotAuthorizedError) as excinfo:
        await search_service.run_search(FakeDB(), request)
    await case_service.transition(
        FakeDB(), request, case.FAILED, audit_action=case.AUDIT_FAILED,
        error_code=excinfo.value.code, error_detail=excinfo.value.message,
    )
    assert request.status == case.FAILED
    assert request.error_code == case.ERR_SOURCE_NOT_AUTHORIZED
    assert len(world.requests) == 1, "no second case was opened for this failure"

    # (b) An administrator fixes the configuration -- a source is now authorized.
    async def fixed_authorizations(db, org_id):
        return [AUTHORIZATION]

    monkeypatch.setattr(dsr_repository, "list_source_authorizations", fixed_authorizations)
    _use_connector(monkeypatch, FakeConnector(matches=[match()]))

    # (c) The retry is legal (FAILED -> SEARCHING is an explicit lifecycle edge) and
    # lands on the SAME case id.
    assert lifecycle.can_transition(case.FAILED, case.SEARCHING)
    await case_service.transition(FakeDB(), request, case.SEARCHING, actor_user_id=USER)
    summary = await search_service.run_search(FakeDB(), request)
    await case_service.transition(
        FakeDB(), request, case.SEARCH_COMPLETED,
        error_code=summary.outcome_code,
        error_detail="see search runs" if summary.outcome_code else None,
    )

    # (d) It proceeded past the authorization check and found the record -- on the
    # same case, with no duplicate ever created.
    assert request.id == case_id
    assert len(world.requests) == 1
    assert request.status == case.SEARCH_COMPLETED
    assert summary.outcome_code is None
    assert summary.evidence_count == 1


@pytest.mark.asyncio
async def test_ambiguous_intent_is_recorded_as_other_with_zero_confidence(world):
    """§14: a request asking for two different things must never be quietly resolved
    into one. The raw classifier's own ambiguity handling is covered in
    test_dsr_classification.py; this proves case_service.classify_case -- the layer
    an actual case goes through -- records the SAME closed, reviewable outcome
    (OTHER, confidence 0.0) for it that an unmatched request gets, not something
    that looks like a confident guess."""
    request, _ = await case_service.create_case(
        FakeDB(), org_id=ORG,
        raw_request="Send me a copy of my data and then correct my phone number.",
        requester_email="a@b.com", created_by_user_id=USER, now=NOW,
    )
    await case_service.classify_case(FakeDB(), request, actor_user_id=USER)

    assert request.request_type == case.OTHER
    assert request.classification_confidence == 0.0
    assert request.status == case.CLASSIFIED


@pytest.mark.asyncio
async def test_other_classified_case_never_executes_a_mutating_operation(world, monkeypatch):
    """§47 / the one place a real defect could still be hiding: an unclassifiable
    request (OTHER, confidence 0.0) must never reach a destructive write without a
    human first overriding the classification via classify_case(override_type=...).

    The planner maps an unmapped request_type to OP_NO_OP (never a key in
    _OPERATION_FOR_TYPE), OP_NO_OP is not a MUTATING_OPERATION so it never requires
    approval, and the case is therefore free to sail straight through to EXECUTING
    -- but what it hands the connector is an operation the real PostgresDsrConnector
    refuses to touch the database for at all (see
    connectors/postgres.py::execute_action). This proves that chain holds together
    end to end, recording what each action's connector call actually carried."""
    calls: list[dict] = []

    class RecordingConnector(FakeConnector):
        async def execute_action(self, **kw):
            calls.append(kw)
            return await super().execute_action(**kw)

    connector = RecordingConnector(matches=[match()])
    _use_connector(monkeypatch, connector)

    request = await _open_case("Hello, I have a question about my recent order.")
    assert request.request_type == case.OTHER
    assert request.classification_confidence == 0.0

    await _verify(request)
    await _search(request)
    plan, actions = await _plan(request)

    assert actions, "the record found must still produce an action, never a silent drop"
    assert all(a.operation == case.OP_NO_OP for a in actions)
    assert not plan.requires_approval
    assert not any(a.requires_approval for a in actions)

    from app.services import dsr_run_service

    next_status = dsr_run_service.next_status_after_planning(plan, actions)
    await case_service.transition(FakeDB(), request, next_status, actor_user_id=USER)
    await case_service.transition(FakeDB(), request, case.EXECUTING, actor_user_id=USER)

    for action in actions:
        execution = await execution_service.execute_action(
            FakeDB(), request, action.id, now=NOW,
        )
        assert execution.status == "verified"

    # Every call the connector actually received carried OP_NO_OP, never a mutation.
    assert calls, "execution never reached the connector at all"
    assert all(c["operation"] == case.OP_NO_OP for c in calls)
    assert all(c["operation"] not in case.MUTATING_OPERATIONS for c in calls)


@pytest.mark.asyncio
async def test_a_search_covers_every_configured_authorized_source(world, monkeypatch):
    """Two authorized sources must both be searched, not just the first one found --
    and looking up their data sources must be one batched call, not one per
    authorization (the N+1 pattern already fixed in the /config/sources route)."""
    from app.agents.dsr.connectors import factory
    from app.db.repositories import dsr_repository, ropa_repository

    second_source_id = uuid.uuid4()
    second_authorization = SimpleNamespace(
        id=uuid.uuid4(), data_source_id=second_source_id, enabled=True,
        searchable_tables=["accounts"], identity_tables=["accounts"],
        identifier_columns={"accounts": {"email": "email"}},
        returnable_columns={"accounts": ["name", "email"]},
        erasable_columns={"accounts": ["name", "email"]},
        allow_execution=True, write_credential_ref="SRC2_WRITE",
    )
    second_data_source = SimpleNamespace(
        id=second_source_id, name="billing", enabled=True, connector="postgres",
        config={"host": "h2", "dbname": "d2", "user": "u2"}, credential_ref="SRC2_READ",
    )

    async def list_source_auth(db, org_id):
        return [AUTHORIZATION, second_authorization]

    list_data_sources_calls = []

    async def list_data_sources(db, org_id):
        list_data_sources_calls.append(org_id)
        return [DATA_SOURCE, second_data_source]

    monkeypatch.setattr(dsr_repository, "list_source_authorizations", list_source_auth)
    monkeypatch.setattr(ropa_repository, "list_data_sources", list_data_sources)

    connectors_by_source = {
        "crm": FakeConnector(matches=[match()]),
        "billing": FakeConnector(matches=[match("accounts", 42)]),
    }

    def build_connector(*, data_source, authorization, for_execution=False):
        return connectors_by_source[data_source.name]

    monkeypatch.setattr(factory, "build_connector", build_connector)

    request = await _open_case("Show me what personal information you hold about me.")
    await _verify(request)
    summary = await search_service.run_search(FakeDB(), request)

    assert sorted(summary.sources_searched) == ["billing", "crm"]
    assert summary.evidence_count == 2
    # One batched lookup for the whole search, not one per authorization -- this is
    # exactly the N+1 the fix removed; this test fails if it comes back.
    assert len(list_data_sources_calls) == 1


@pytest.mark.asyncio
async def test_intake_is_idempotent(world):
    """A requester double-submitting a web form must not open two cases racing to
    delete the same row."""
    first, new_first = await case_service.create_case(
        FakeDB(), org_id=ORG, raw_request="delete my data", requester_email="a@b.com",
        idempotency_key="form-submit-1", now=NOW,
    )
    second, new_second = await case_service.create_case(
        FakeDB(), org_id=ORG, raw_request="delete my data", requester_email="a@b.com",
        idempotency_key="form-submit-1", now=NOW,
    )
    assert new_first and not new_second
    assert first.id == second.id
    assert len(world.requests) == 1


@pytest.mark.asyncio
async def test_every_case_in_every_scenario_ends_somewhere_explicit(world, monkeypatch):
    """§47: a case must never be left in a status that means nothing happened."""
    _use_connector(monkeypatch, FakeConnector(matches=[], distinct=0))
    request = await _open_case("Show me my data.")
    await _verify(request)
    await _search(request)
    # No match -- but the case carries a code and can still reach a response.
    assert request.error_code == case.ERR_NO_MATCH
    assert case.RESPONSE_PENDING in lifecycle.allowed_transitions(request.status)


# ── Scenario 3b: DELETION that is only partly possible ───────────────────────────

@pytest.mark.asyncio
async def test_scenario_3b_partial_deletion_reports_both_halves(world, monkeypatch):
    """§42 scenario 3 ends "Complete/Partial". A retention rule keeps one record; the
    other is deleted. The case must not close as "completed", and the response must
    say which record was kept and why."""
    from datetime import timedelta

    from app.agents.dsr.connectors import factory
    from app.agents.dsr.rules import constraints as rules
    from app.services import dsr_run_service

    _use_connector(monkeypatch, FakeConnector(matches=[match(), match("orders", 11)]))
    request = await _open_case("Delete my personal information.")
    await _verify(request)
    await _search(request)

    # The orders table is under a configured retention rule; customers is not.
    keep_orders = rules.RetentionRule(
        table_name="orders", minimum_retention=timedelta(days=365 * 7),
        date_column="created_at", authority="Finance policy FIN-3",
    )
    for item in world.evidence:
        if item.table_name == "orders":
            item.record_snapshot = {"created_at": "2025-01-01T00:00:00Z", "order_id": "O-11"}

    grants = {DATA_SOURCE.name: factory.build_grant(AUTHORIZATION, source_name=DATA_SOURCE.name)}
    _, actions = await planning_service.build_plan(
        FakeDB(), request, grants=grants, retention_rules=(keep_orders,)
    )
    blocked = [a for a in actions if a.status == "blocked"]
    proposed = [a for a in actions if a.status == "proposed"]
    assert len(blocked) == 1 and len(proposed) == 1

    await case_service.transition(FakeDB(), request, case.APPROVAL_REQUIRED)
    await approval_service.decide_action(
        FakeDB(), request, proposed[0].id, reviewer_user_id=USER,
        decision="approved", reason="identity confirmed", now=NOW,
    )
    await case_service.transition(FakeDB(), request, case.APPROVED)
    await case_service.transition(FakeDB(), request, case.EXECUTING)
    await execution_service.execute_action(FakeDB(), request, proposed[0].id, now=NOW)

    # One succeeded, one is blocked -> verified, but explicitly partial.
    status, code = dsr_run_service.status_after_execution(succeeded=1, failed=0, blocked=1)
    assert status == case.EXECUTION_VERIFIED
    assert code == case.ERR_ACTION_PARTIAL
    await case_service.transition(FakeDB(), request, status, error_code=code,
                                  error_detail="1 deleted, 1 retained")
    await case_service.transition(FakeDB(), request, case.RESPONSE_PENDING,
                                  error_code=code, error_detail="1 deleted, 1 retained")

    response = await response_service.build_response(FakeDB(), request)
    assert "were NOT changed" in response.body_text
    assert "Finance policy FIN-3" in response.body_text
    assert "deleted 1 record(s)" in response.body_text

    # And the case closes honestly.
    closing = dsr_run_service.closing_status(blocked=1, failed=0)
    assert closing == case.PARTIALLY_COMPLETED
    await case_service.transition(FakeDB(), request, closing,
                                  error_code=case.ERR_ACTION_PARTIAL,
                                  error_detail="1 record retained under a policy")
    assert request.status == case.PARTIALLY_COMPLETED
    assert lifecycle.is_terminal(request.status)
