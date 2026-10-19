"""Push evidence: Consiva runs the subject search itself and POSTs the matched
records to the DSR agent for analysis, instead of the agent connecting to the
customer's database directly.

Mirrors test_dsr_end_to_end.py's style: an in-memory fake of the repository, no
live Postgres. What these hold to account:

  * the identity/state gate (409, not /search's 403/409 split)
  * the documented limits (413, never a silent truncation)
  * idempotency on client_reference
  * a plan is built from pushed evidence exactly as from a connector-driven search
  * the execution-report path closes the case the same way execute_queued_actions
    already does
  * nothing about a row's VALUES ever reaches a log line
"""

import logging
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

from app.agents.dsr.connectors import authorization
from app.agents.dsr.errors import CaseNotReadyError, DsrNotFoundError, EvidenceTooLargeError
from app.agents.dsr.rules import constraints as rules
from app.agents.dsr.schemas import case
from app.agents.dsr.schemas.evidence_push import EvidencePush, PushedSubject
from app.agents.dsr.services import evidence_push_service
from app.api.v1.routes import dsr as dsr_routes
from app.config import get_settings
from app.db.models import DsrAction, DsrActionPlan, DsrEvidence, DsrRequest, DsrSearchRun
from app.services import dsr_run_service

NOW = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)
ORG = uuid.uuid4()
USER = uuid.uuid4()


def make_push(**kw) -> EvidencePush:
    defaults = {
        "source_name": "legacy-crm",
        "subject": {"email": "a@b.com"},
        "tables": [{
            "table_name": "customers",
            "match_count": 1,
            "columns": ["id", "email", "name"],
            "identifier_columns": {"email": "email"},
            "record_key_columns": ["id"],
            "rows": [{"id": 7, "email": "a@b.com", "name": "Alice"}],
            "truncated": False,
        }],
    }
    defaults.update(kw)
    return EvidencePush.model_validate(defaults)


def make_request(status=case.IDENTITY_VERIFIED, request_type=case.DELETION) -> DsrRequest:
    return DsrRequest(
        id=uuid.uuid4(), org_id=ORG, reference="DSR-PUSH01", raw_request="delete my data",
        request_type=request_type, due_at=NOW + timedelta(days=30),
        requester_email="a@b.com", status=status,
    )


# ── Schema boundary: unknown columns rejected, subject requires an identifier ────

def test_a_row_naming_an_undeclared_column_is_rejected():
    with pytest.raises(ValidationError, match="not declared"):
        make_push(tables=[{
            "table_name": "customers", "match_count": 1,
            "columns": ["id", "email"], "identifier_columns": {"email": "email"},
            "record_key_columns": ["id"],
            "rows": [{"id": 7, "email": "a@b.com", "ssn": "123-45-6789"}],
        }])


def test_subject_needs_at_least_one_identifier():
    with pytest.raises(ValidationError, match="at least one"):
        PushedSubject()


def test_match_count_must_agree_with_rows_sent_when_not_truncated():
    with pytest.raises(ValidationError, match="match_count"):
        make_push(tables=[{
            "table_name": "customers", "match_count": 5,
            "columns": ["id"], "identifier_columns": {"id": "reference"},
            "record_key_columns": ["id"], "rows": [{"id": 7}], "truncated": False,
        }])


def test_identifier_columns_must_be_a_subset_of_columns():
    with pytest.raises(ValidationError, match="identifier_columns"):
        make_push(tables=[{
            "table_name": "customers", "match_count": 1,
            "columns": ["id"], "identifier_columns": {"email": "email"},
            "record_key_columns": ["id"], "rows": [{"id": 7}],
        }])


# ── Limits: rejected whole, never truncated (413) ────────────────────────────────

def test_over_the_table_limit_is_rejected_with_the_limit_named():
    push = make_push(tables=[
        {
            "table_name": f"t{i}", "match_count": 0, "columns": ["id"],
            "identifier_columns": {"id": "reference"}, "record_key_columns": ["id"], "rows": [],
        }
        for i in range(evidence_push_service.MAX_TABLES_PER_PUSH + 1)
    ])
    with pytest.raises(EvidenceTooLargeError, match=str(evidence_push_service.MAX_TABLES_PER_PUSH)):
        evidence_push_service.enforce_limits(push)


def test_over_the_per_table_row_limit_is_rejected():
    too_many = evidence_push_service.MAX_ROWS_PER_TABLE + 1
    push = make_push(tables=[{
        "table_name": "customers", "match_count": too_many, "columns": ["id"],
        "identifier_columns": {"id": "reference"}, "record_key_columns": ["id"],
        "rows": [{"id": i} for i in range(too_many)], "truncated": True,
    }])
    with pytest.raises(EvidenceTooLargeError, match=str(evidence_push_service.MAX_ROWS_PER_TABLE)):
        evidence_push_service.enforce_limits(push)


def test_over_the_total_row_limit_across_tables_is_rejected():
    per_table = 500
    n_tables = (evidence_push_service.MAX_ROWS_PER_PUSH // per_table) + 2
    push = make_push(tables=[
        {
            "table_name": f"t{i}", "match_count": per_table, "columns": ["id"],
            "identifier_columns": {"id": "reference"}, "record_key_columns": ["id"],
            "rows": [{"id": j} for j in range(per_table)], "truncated": False,
        }
        for i in range(n_tables)
    ])
    with pytest.raises(EvidenceTooLargeError, match=str(evidence_push_service.MAX_ROWS_PER_PUSH)):
        evidence_push_service.enforce_limits(push)


def test_a_push_within_every_limit_is_accepted():
    evidence_push_service.enforce_limits(make_push())  # does not raise


# ── The gate: identity + "can reach SEARCHING", both answered with 409 ──────────

def test_states_that_may_receive_pushed_evidence_matches_the_spec():
    """failed is included -- the whole point: a case with SOURCE_NOT_AUTHORIZED
    (no registered source) must be recoverable via a push."""
    assert evidence_push_service.STATES_THAT_MAY_RECEIVE_PUSHED_EVIDENCE == {
        case.IDENTITY_VERIFIED, case.CLASSIFIED, case.REVIEW_REQUIRED, case.FAILED,
    }


@pytest.mark.asyncio
async def test_push_is_refused_409_for_a_case_not_identity_verified(monkeypatch):
    request = make_request(status=case.IDENTITY_VERIFIED)
    from app.db.repositories import dsr_repository as repo

    async def _no_verification(db, request_id, org_id):
        return None

    monkeypatch.setattr(repo, "get_latest_identity_verification", _no_verification)
    with pytest.raises(CaseNotReadyError):
        await evidence_push_service.assert_case_accepts_push(_DB(), request)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [case.RECEIVED, case.SEARCHING, case.SEARCH_COMPLETED,
                                     case.APPROVED, case.EXECUTING, case.COMPLETED])
async def test_push_is_refused_409_from_an_ineligible_case_status(monkeypatch, status):
    request = make_request(status=status)
    _wire_verified(monkeypatch)
    with pytest.raises(CaseNotReadyError):
        await evidence_push_service.assert_case_accepts_push(_DB(), request)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status", [case.IDENTITY_VERIFIED, case.CLASSIFIED, case.REVIEW_REQUIRED, case.FAILED]
)
async def test_push_is_accepted_from_every_eligible_status(monkeypatch, status):
    request = make_request(status=status)
    _wire_verified(monkeypatch)
    await evidence_push_service.assert_case_accepts_push(_DB(), request)  # does not raise


def test_subject_mismatch_is_refused():
    request = make_request()
    with pytest.raises(CaseNotReadyError, match="matches none"):
        evidence_push_service.assert_subject_matches_case(
            request, PushedSubject(email="someone-else@example.com")
        )


def test_subject_match_is_case_and_whitespace_insensitive():
    request = make_request()
    request.requester_email = " A@B.com "
    evidence_push_service.assert_subject_matches_case(
        request, PushedSubject(email="a@b.com ")
    )  # does not raise


# ── Grant synthesis: no write_credential_ref needed when execution is external ──

def test_pushed_grant_waives_the_write_credential_requirement():
    push = make_push()
    grant = evidence_push_service.grant_for_push(push)
    assert grant.external_execution is True
    assert grant.write_credential_ref is None
    assert grant.allow_execution is True

    found = rules.evaluate(operation=case.OP_DELETE_RECORD, table_name="customers", grant=grant, now=NOW)
    # A real connector-execution grant with no write_credential_ref would BLOCK here
    # (test_dsr_planning.py's test_unauthorized_execution_is_a_system_fact_not_a_policy_claim
    # proves that for allow_execution=False; this proves external_execution changes the
    # write-credential branch specifically, not the allow_execution branch).
    assert not any(c.effect == rules.EFFECT_BLOCK and c.code == case.ERR_SOURCE_NOT_AUTHORIZED
                   for c in found)


def test_a_grant_with_no_external_execution_flag_keeps_requiring_a_write_credential():
    """A duck-typed grant with no `external_execution` attribute at all (the
    test harnesses elsewhere in this suite build plain SimpleNamespace grants)
    behaves exactly as it always has."""
    auth = SimpleNamespace(
        enabled=True, searchable_tables=["customers"], identity_tables=[],
        identifier_columns={}, returnable_columns={"customers": ["name"]},
        erasable_columns={"customers": ["name"]}, allow_execution=True,
        write_credential_ref=None,
    )
    grant = authorization.resolve(auth, source_name="crm")
    found = rules.evaluate(operation=case.OP_DELETE_RECORD, table_name="customers", grant=grant, now=NOW)
    blocker = next(c for c in found if c.effect == rules.EFFECT_BLOCK)
    assert blocker.code == case.ERR_SOURCE_NOT_AUTHORIZED


# ── Evidence rows built from a pushed table ──────────────────────────────────────

def test_evidence_rows_carry_the_matched_identifier_and_only_the_declared_columns():
    request = make_request()
    run = DsrSearchRun(id=uuid.uuid4(), org_id=ORG, request_id=request.id, source_name="legacy-crm",
                        status="running", match_count=0, distinct_subject_count=0, tables_searched=[])
    push = make_push()
    rows = evidence_push_service.evidence_rows_for_push(request, run, push)
    assert len(rows) == 1
    row = rows[0]
    assert isinstance(row, DsrEvidence)
    assert row.data_source_id is None
    assert row.matched_column == "email"
    assert row.identifier_kind == "email"
    assert row.record_reference == {"id": 7}
    assert row.record_snapshot == {"id": 7, "email": "a@b.com", "name": "Alice"}


# ── Encryption at rest (reuses ROPA's Fernet key, same functions) ────────────────

@pytest.fixture(autouse=True)
def _reset_settings_cache(monkeypatch):
    get_settings.cache_clear()
    yield
    monkeypatch.undo()
    get_settings.cache_clear()


def test_pushed_payload_round_trips_encrypted(monkeypatch):
    monkeypatch.setenv("ROPA_CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    get_settings.cache_clear()

    push = make_push()
    ciphertext = evidence_push_service.encrypt_push_payload(push)
    assert "Alice" not in ciphertext and "a@b.com" not in ciphertext

    restored = evidence_push_service.decrypt_push_payload(ciphertext)
    assert restored[0]["rows"][0]["name"] == "Alice"


# ── Routes: same scope as the rest of DSR, no new scope minted ──────────────────

def _deps(route) -> set:
    return {d.call for d in route.dependant.dependencies}


def test_push_evidence_routes_use_the_same_session_authentication_as_search():
    from app.core.security import get_current_user

    by_path = {r.path: _deps(r) for r in dsr_routes.router.routes}
    for path in (
        "/api/v1/dsr/requests/{request_id}/evidence",
        "/api/v1/dsr/requests/{request_id}/execution-report",
        "/api/v1/dsr/requests/{request_id}/evidence/purge",
    ):
        # The signed-in user, exactly like /search: no separate key, token or scope for pushing.
        assert get_current_user in by_path[path], path


# ── harness for the orchestration (dsr_run_service) ──────────────────────────────

class _DB:
    async def flush(self):
        return None


class World:
    def __init__(self):
        self.search_runs: list[DsrSearchRun] = []
        self.evidence: list[DsrEvidence] = []
        self.plans: list[DsrActionPlan] = []
        self.actions: list[DsrAction] = []
        self.pushes: list = []
        self.executions: list = []
        self.audit: list[dict] = []


def _wire_verified(monkeypatch):
    from app.db.repositories import dsr_repository as repo

    async def _verified(db, request_id, org_id):
        return SimpleNamespace(status=case.IDV_VERIFIED)

    monkeypatch.setattr(repo, "get_latest_identity_verification", _verified)


def _install(monkeypatch, w: World, *, retention_rules=()):
    from app.db.repositories import dsr_repository as repo

    _wire_verified(monkeypatch)

    async def find_run_by_key(db, org_id, key):
        return next((r for r in w.search_runs if getattr(r, "idempotency_key", None) == key), None)

    async def create_search_run(db, **kw):
        row = DsrSearchRun(id=uuid.uuid4(), status="pending", match_count=0,
                           distinct_subject_count=0, tables_searched=[], **kw)
        w.search_runs.append(row)
        return row

    async def create_evidence_push(db, **kw):
        row = SimpleNamespace(id=uuid.uuid4(), **kw)
        w.pushes.append(row)
        return row

    async def add_evidence(db, org_id, rows):
        w.evidence.extend(rows)

    async def list_evidence(db, request_id, org_id):
        return [e for e in w.evidence if e.request_id == request_id]

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

    async def list_source_authorizations(db, org_id):
        return []  # the whole scenario this feature exists for: no registered source

    async def list_retention_rules(db, org_id, **kw):
        return list(retention_rules)

    async def claim_execution(db, *, org_id, request_id, action_id, idempotency_key, **kw):
        existing = next((e for e in w.executions if e.idempotency_key == idempotency_key), None)
        if existing:
            return existing, False
        from app.db.models import DsrExecution
        row = DsrExecution(id=uuid.uuid4(), org_id=org_id, request_id=request_id,
                           action_id=action_id, idempotency_key=idempotency_key, status="pending")
        row.attempts = 0
        w.executions.append(row)
        return row, True

    for name, fn in [
        ("find_search_run_by_idempotency_key", find_run_by_key),
        ("create_search_run", create_search_run),
        ("create_evidence_push", create_evidence_push),
        ("add_evidence", add_evidence), ("list_evidence", list_evidence),
        ("next_plan_version", next_plan_version), ("get_current_plan", get_current_plan),
        ("create_plan", create_plan), ("add_actions", add_actions), ("list_actions", list_actions),
        ("list_source_authorizations", list_source_authorizations),
        ("list_retention_rules", list_retention_rules),
        ("claim_execution", claim_execution),
    ]:
        monkeypatch.setattr(repo, name, fn)

    async def audit(db, **kw):
        w.audit.append(kw)

    from app.services import audit_service
    monkeypatch.setattr(audit_service, "record", audit)

    monkeypatch.setenv("ROPA_CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    get_settings.cache_clear()


@pytest.fixture
def world(monkeypatch):
    w = World()
    _install(monkeypatch, w)
    return w


# ── Plan generation from pushed evidence ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_ingest_builds_a_plan_from_pushed_evidence(world):
    request = make_request(request_type=case.DELETION)
    run, plan, actions = await dsr_run_service.ingest_pushed_evidence(
        _DB(), request, make_push(),
        actor_user_id=USER, now=NOW,
    )
    assert run.ingest_mode == "push"
    assert run.status == "completed"
    assert run.match_count == 1
    assert plan is not None
    assert len(actions) == 1
    # A deletion request against pushed evidence proposes OP_DELETE_RECORD and
    # still requires approval -- the human gate is not skipped because the
    # execution happens outside this process.
    assert actions[0].operation == case.OP_DELETE_RECORD
    assert actions[0].requires_approval is True
    assert actions[0].data_source_id is None
    assert request.status in (case.APPROVAL_REQUIRED, case.APPROVED)


@pytest.mark.asyncio
async def test_a_retried_push_with_the_same_client_reference_is_a_no_op(world):
    request = make_request()
    push = make_push(client_reference="consiva-job-42")
    run1, _plan1, actions1 = await dsr_run_service.ingest_pushed_evidence(
        _DB(), request, push,
        actor_user_id=USER, now=NOW,
    )
    run_count_after_first = len(world.search_runs)

    run2, _plan2, actions2 = await dsr_run_service.ingest_pushed_evidence(
        _DB(), request, push,
        actor_user_id=USER, now=NOW,
    )
    assert run2.id == run1.id
    assert len(world.search_runs) == run_count_after_first, "a replay must not create a second run"
    assert [a.id for a in actions2] == [a.id for a in actions1]


@pytest.mark.asyncio
async def test_truncated_evidence_routes_to_review_required_without_a_plan(world):
    request = make_request()
    push = make_push(truncated=True)
    _run, plan, actions = await dsr_run_service.ingest_pushed_evidence(
        _DB(), request, push,
        actor_user_id=USER, now=NOW,
    )
    assert plan is None
    assert actions == []
    assert request.status == case.REVIEW_REQUIRED
    assert request.error_code == case.ERR_MULTIPLE_MATCHES


@pytest.mark.asyncio
async def test_no_matched_rows_still_reaches_an_explicit_outcome(world):
    request = make_request(request_type=case.ACCESS)
    push = make_push(tables=[])
    run, _plan, actions = await dsr_run_service.ingest_pushed_evidence(
        _DB(), request, push,
        actor_user_id=USER, now=NOW,
    )
    assert run.status == "no_match"
    assert run.error_code == case.ERR_NO_MATCH
    assert actions == []
    assert request.status == case.RESPONSE_PENDING  # never a silent stop (§47)


@pytest.mark.asyncio
async def test_recovers_a_case_stuck_failed_by_source_not_authorized(world):
    """The motivating scenario: an org with no registered source can never use
    /search (every case ends FAILED/SOURCE_NOT_AUTHORIZED); a push must still
    work from there."""
    request = make_request(status=case.FAILED, request_type=case.ACCESS)
    request.error_code = case.ERR_SOURCE_NOT_AUTHORIZED
    run, plan, _actions = await dsr_run_service.ingest_pushed_evidence(
        _DB(), request, make_push(),
        actor_user_id=USER, now=NOW,
    )
    assert run.status == "completed"
    assert plan is not None


# ── Execution manifest + execution-report ────────────────────────────────────────

async def _approved_push_case(world, *, operation=case.OP_DELETE_RECORD) -> tuple[DsrRequest, DsrAction]:
    request = make_request(request_type=case.DELETION, status=case.IDENTITY_VERIFIED)
    _, _plan, actions = await dsr_run_service.ingest_pushed_evidence(
        _DB(), request, make_push(),
        actor_user_id=USER, now=NOW,
    )
    action = actions[0]
    action.status = "approved"
    request.status = case.APPROVED
    return request, action


@pytest.mark.asyncio
async def test_execute_issues_a_manifest_instead_of_a_job_for_pushed_evidence(world):
    request, action = await _approved_push_case(world)
    manifest = await dsr_run_service.issue_execution_manifest(_DB(), request, actor_user_id=USER)
    assert [a.id for a in manifest] == [action.id]
    assert request.status == case.EXECUTING
    assert any(a["action"] == case.AUDIT_EXECUTION_MANIFEST_ISSUED for a in world.audit)


@pytest.mark.asyncio
async def test_execution_report_closes_the_case_when_everything_succeeds(world):
    request, action = await _approved_push_case(world)
    await dsr_run_service.issue_execution_manifest(_DB(), request, actor_user_id=USER)

    result = await dsr_run_service.record_execution_report(
        _DB(), request,
        [SimpleNamespace(action_id=action.id, status="done", rows_affected=1,
                         occurred_at=NOW, error_text=None)],
        actor_user_id=USER, now=NOW,
    )
    assert result["closed"] is True
    assert action.status == "executed"
    assert request.status == case.RESPONSE_PENDING


@pytest.mark.asyncio
async def test_execution_report_is_idempotent_per_action(world):
    request, action = await _approved_push_case(world)
    await dsr_run_service.issue_execution_manifest(_DB(), request, actor_user_id=USER)
    item = SimpleNamespace(action_id=action.id, status="done", rows_affected=1,
                           occurred_at=NOW, error_text=None)

    await dsr_run_service.record_execution_report(_DB(), request, [item], actor_user_id=USER, now=NOW)
    executions_after_first = len(world.executions)

    # Case is now RESPONSE_PENDING; record_execution_report requires EXECUTING, so
    # simulate a retried report arriving before the case moved on by resetting the
    # observable precondition this function itself checks.
    request.status = case.EXECUTING
    await dsr_run_service.record_execution_report(_DB(), request, [item], actor_user_id=USER, now=NOW)
    assert len(world.executions) == executions_after_first, "a retried report must not double-record"


@pytest.mark.asyncio
async def test_execution_report_refuses_a_blocked_action(world):
    request, action = await _approved_push_case(world)
    action.status = "blocked"
    action.blocked_reason = "retention rule"
    request.status = case.EXECUTING
    with pytest.raises(CaseNotReadyError):
        await dsr_run_service.record_execution_report(
            _DB(), request,
            [SimpleNamespace(action_id=action.id, status="done", rows_affected=1,
                             occurred_at=NOW, error_text=None)],
            actor_user_id=USER, now=NOW,
        )


@pytest.mark.asyncio
async def test_execution_report_refuses_an_unknown_action(world):
    request, _action = await _approved_push_case(world)
    request.status = case.EXECUTING
    with pytest.raises(DsrNotFoundError):
        await dsr_run_service.record_execution_report(
            _DB(), request,
            [SimpleNamespace(action_id=uuid.uuid4(), status="done", rows_affected=1,
                             occurred_at=NOW, error_text=None)],
            actor_user_id=USER, now=NOW,
        )


@pytest.mark.asyncio
async def test_execution_report_requires_the_case_to_be_executing(world):
    request, action = await _approved_push_case(world)  # status is APPROVED, not EXECUTING
    with pytest.raises(CaseNotReadyError):
        await dsr_run_service.record_execution_report(
            _DB(), request,
            [SimpleNamespace(action_id=action.id, status="done", rows_affected=1,
                             occurred_at=NOW, error_text=None)],
            actor_user_id=USER, now=NOW,
        )


@pytest.mark.asyncio
async def test_a_failed_report_closes_the_case_failed_not_execution_verified(world):
    request, action = await _approved_push_case(world)
    await dsr_run_service.issue_execution_manifest(_DB(), request, actor_user_id=USER)

    result = await dsr_run_service.record_execution_report(
        _DB(), request,
        [SimpleNamespace(action_id=action.id, status="failed", rows_affected=0,
                         occurred_at=NOW, error_text="connection refused")],
        actor_user_id=USER, now=NOW,
    )
    assert result["closed"] is True
    assert action.status == "failed"
    assert request.status == case.FAILED  # recoverable, not terminal (§39)


# ── Privacy: never a row value in a log line ─────────────────────────────────────

@pytest.mark.asyncio
async def test_pushed_row_values_never_reach_a_log_line(world, caplog):
    request = make_request()
    secret_value = "ZZZ_SECRET_ROW_VALUE_should_never_be_logged"
    push = make_push(tables=[{
        "table_name": "customers", "match_count": 1,
        "columns": ["id", "email", "notes"],
        "identifier_columns": {"email": "email"}, "record_key_columns": ["id"],
        "rows": [{"id": 7, "email": "a@b.com", "notes": secret_value}],
        "truncated": False,
    }])
    with caplog.at_level(logging.INFO):
        await dsr_run_service.ingest_pushed_evidence(
            _DB(), request, push,
            actor_user_id=USER, now=NOW,
        )
    for record in caplog.records:
        assert secret_value not in record.getMessage()
        assert "a@b.com" not in record.getMessage()
    # Table/column names and counts are expected to appear -- that is the point.
    assert any("customers" in r.getMessage() for r in caplog.records)
