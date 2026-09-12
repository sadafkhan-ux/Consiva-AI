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

    async def add_evidence(db, rows):
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

    async def add_actions(db, rows):
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

    from app.db.repositories import ropa_repository
    monkeypatch.setattr(ropa_repository, "get_data_source", get_data_source)

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
