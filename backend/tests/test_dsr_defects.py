"""Regression tests for defects found auditing Agent 3 after the build.

Each one failed when written. They are kept together because they share a theme:
every one is a case where the unit tests passed because each stubbed the component
next to it, and the fault only appears where two real components meet.
"""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import IntegrityError

from app.agents.dsr.connectors import authorization
from app.agents.dsr.connectors.postgres import PostgresDsrConfig, PostgresDsrConnector
from app.agents.dsr.schemas import case
from app.db.models import DsrExecution
from app.db.repositories import dsr_repository

NOW = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)
ORG = uuid.uuid4()


# ── Defect 1: claim_execution rolled back the whole session ──────────────────────

class _SessionSpy:
    """Records whether a SAVEPOINT was used, or the whole transaction discarded."""

    def __init__(self, *, raise_on_flush: bool):
        self._raise = raise_on_flush
        self.rollback_calls = 0
        self.nested_calls = 0
        self.added = []

    def add(self, row):
        self.added.append(row)

    async def flush(self):
        if self._raise:
            self._raise = False
            raise IntegrityError("dup", {}, Exception("unique violation"))

    async def rollback(self):
        self.rollback_calls += 1

    def begin_nested(self):
        self.nested_calls += 1
        return _Savepoint()


class _Savepoint:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        # Swallow the IntegrityError the way a real SAVEPOINT context does, so the
        # outer transaction survives.
        return False


@pytest.mark.asyncio
async def test_a_duplicate_claim_does_not_discard_the_outer_transaction(monkeypatch):
    """execute_queued_actions runs several actions in ONE session. A duplicate key on
    action 3 must not roll back the execution rows, case transitions and audit
    entries already written for actions 1 and 2.
    """
    existing = DsrExecution(
        id=uuid.uuid4(), org_id=ORG, request_id=uuid.uuid4(), action_id=uuid.uuid4(),
        idempotency_key="dup", status="verified",
    )

    async def _find(db, org_id, key):
        return existing

    monkeypatch.setattr(dsr_repository, "find_execution_by_key", _find)

    db = _SessionSpy(raise_on_flush=True)
    row, is_new = await dsr_repository.claim_execution(
        db, org_id=ORG, request_id=uuid.uuid4(), action_id=uuid.uuid4(),
        idempotency_key="dup",
    )
    assert (row, is_new) == (existing, False)
    assert db.rollback_calls == 0, (
        "claim_execution rolled back the whole session; a duplicate on one action "
        "would discard every earlier action's work in the same transaction"
    )
    assert db.nested_calls == 1, "the insert was not attempted inside a SAVEPOINT"


# ── Defect 2: the record locator was hard-coded to an 'id' column ────────────────

def _grant(**kw):
    auth = SimpleNamespace(
        enabled=True,
        searchable_tables=kw.get("searchable_tables", ["subscriptions"]),
        identity_tables=[],
        identifier_columns={"subscriptions": {"email": "subscriber_email"}},
        returnable_columns={"subscriptions": ["plan"]},
        erasable_columns={},
        allow_execution=False,
        write_credential_ref=None,
        record_key_columns=kw.get("record_key_columns", {}),
    )
    return authorization.resolve(auth, source_name="billing")


def _connector(**kw):
    return PostgresDsrConnector(
        config=PostgresDsrConfig(
            host="h", port=5432, dbname="d", read_user="r", read_password="p",
        ),
        grant=_grant(**kw),
    )


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows
        self.queries = []

    async def fetch(self, sql, *args):
        self.queries.append((sql, args))
        return list(self._rows)

    async def execute(self, sql, *args):
        self.queries.append((sql, args))
        return "SELECT 0"

    async def close(self):
        return None


def _stub_connect(conn):
    async def _c(*, write: bool):
        return conn
    return _c


@pytest.mark.asyncio
async def test_a_table_whose_primary_key_is_not_called_id_can_be_searched(monkeypatch):
    """Plenty of real schemas key on subscription_id, uuid, or a composite. Selecting
    a hard-coded "id" made every such table fail with UndefinedColumn."""
    conn = _FakeConn([{"subscription_id": "S-1", "subscriber_email": "a@b.com", "plan": "pro"}])
    connector = _connector(record_key_columns={"subscriptions": ["subscription_id"]})
    monkeypatch.setattr(connector, "_connect", _stub_connect(conn))

    outcome = await connector.search_subject(identifiers={"email": "a@b.com"})

    sql = conn.queries[0][0]
    assert '"id"' not in sql, "the query still selects a hard-coded id column"
    assert '"subscription_id"' in sql
    assert outcome.matches[0].record_reference == {"subscription_id": "S-1"}


@pytest.mark.asyncio
async def test_a_composite_key_is_carried_whole(monkeypatch):
    """A record addressed by half its key is a record that could match another row."""
    conn = _FakeConn([{"org": "O-1", "member": "M-9", "subscriber_email": "a@b.com", "plan": "pro"}])
    connector = _connector(record_key_columns={"subscriptions": ["org", "member"]})
    monkeypatch.setattr(connector, "_connect", _stub_connect(conn))

    outcome = await connector.search_subject(identifiers={"email": "a@b.com"})
    assert outcome.matches[0].record_reference == {"org": "O-1", "member": "M-9"}


@pytest.mark.asyncio
async def test_an_unconfigured_table_still_defaults_to_id(monkeypatch):
    """The common case stays zero-configuration."""
    conn = _FakeConn([{"id": 5, "subscriber_email": "a@b.com", "plan": "pro"}])
    connector = _connector(record_key_columns={})
    monkeypatch.setattr(connector, "_connect", _stub_connect(conn))

    outcome = await connector.search_subject(identifiers={"email": "a@b.com"})
    assert outcome.matches[0].record_reference == {"id": 5}


def test_key_columns_must_be_valid_identifiers():
    from app.agents.dsr.errors import SourceNotAuthorizedError

    with pytest.raises(SourceNotAuthorizedError):
        _grant(record_key_columns={"subscriptions": ['id"; DROP TABLE x; --']})


def test_key_columns_must_name_a_searchable_table():
    from app.agents.dsr.errors import SourceNotAuthorizedError

    with pytest.raises(SourceNotAuthorizedError):
        _grant(record_key_columns={"other_table": ["id"]})


# ── Defect 3: a plan needing no approval stalled in APPROVAL_REQUIRED ────────────

@pytest.mark.asyncio
async def test_a_plan_with_nothing_to_approve_does_not_wait_for_an_approval(monkeypatch):
    """An ACCESS case produces disclosure actions that need no approval. Routing it
    to APPROVAL_REQUIRED left it waiting for a decision nobody would ever make:
    the only thing that moves a case out of that status is a reviewer acting on an
    action, and there was no action to act on.
    """
    from app.services import dsr_run_service

    plan = SimpleNamespace(id=uuid.uuid4(), requires_approval=False)
    actions = [SimpleNamespace(id=uuid.uuid4(), requires_approval=False, status="proposed")]

    assert dsr_run_service.next_status_after_planning(plan, actions) != case.APPROVAL_REQUIRED
    assert dsr_run_service.next_status_after_planning(plan, actions) == case.APPROVED


def test_a_plan_that_needs_approval_still_waits_for_it():
    from app.services import dsr_run_service

    plan = SimpleNamespace(requires_approval=True)
    actions = [SimpleNamespace(requires_approval=True, status="proposed")]
    assert dsr_run_service.next_status_after_planning(plan, actions) == case.APPROVAL_REQUIRED


def test_an_empty_plan_goes_straight_to_a_response():
    from app.services import dsr_run_service

    plan = SimpleNamespace(requires_approval=False)
    assert dsr_run_service.next_status_after_planning(plan, []) == case.RESPONSE_PENDING


def test_a_plan_of_only_blocked_actions_goes_to_a_response_not_approval():
    """Everything was blocked by a constraint. There is nothing to approve and
    nothing to execute, but the requester is still owed an explanation."""
    from app.services import dsr_run_service

    plan = SimpleNamespace(requires_approval=True)
    actions = [
        SimpleNamespace(requires_approval=True, status="blocked"),
        SimpleNamespace(requires_approval=True, status="blocked"),
    ]
    assert dsr_run_service.next_status_after_planning(plan, actions) == case.RESPONSE_PENDING


# ── Defect 4: a recovered case kept a stale error code ───────────────────────────

@pytest.mark.asyncio
async def test_a_case_that_recovers_does_not_keep_its_old_error_code(monkeypatch):
    from app.agents.dsr.services import case_service
    from app.db.models import DsrRequest

    request = DsrRequest(
        id=uuid.uuid4(), org_id=ORG, reference="DSR-REC", raw_request="x",
        due_at=NOW + timedelta(days=30), status=case.FAILED,
        error_code=case.ERR_CONNECTOR_UNAVAILABLE, error_detail="source was down",
    )

    async def _audit(db, **kw):
        return None

    monkeypatch.setattr(case_service.audit_service, "record", _audit)

    class _DB:
        async def flush(self):
            return None

    await case_service.transition(_DB(), request, case.SEARCHING, now=NOW)
    assert request.error_code is None, "a recovered case still carries its old failure"
    assert request.error_detail is None


# ── Defect 5: case references collided at realistic volumes ──────────────────────

def test_case_references_have_enough_entropy_to_be_unique_at_scale():
    """`reference` is unique per org. At 6 hex characters, the birthday bound gives a
    ~95% chance of a collision by the ten-thousandth case -- at which point case
    creation starts failing with an integrity error on a live system."""
    import math

    from app.agents.dsr.services import case_service

    sample = case_service.new_reference()
    entropy_chars = len(sample.removeprefix("DSR-"))
    space = 16 ** entropy_chars

    # One in a million at a million cases per organization.
    n = 1_000_000
    collision_p = 1 - math.exp(-(n * n) / (2 * space))
    assert collision_p < 0.01, (
        f"{entropy_chars} hex chars gives a {collision_p:.1%} collision chance at "
        f"{n:,} cases"
    )


def test_generated_references_are_distinct_and_well_formed():
    import re

    from app.agents.dsr.services import case_service

    refs = {case_service.new_reference() for _ in range(5000)}
    assert len(refs) == 5000, "new_reference produced a duplicate in 5000 draws"
    assert all(re.fullmatch(r"DSR-[0-9A-F]+", r) for r in refs)


@pytest.mark.asyncio
async def test_intake_retries_when_a_reference_is_already_taken(monkeypatch):
    """Entropy makes a collision unlikely, not impossible. If one happens, opening
    the case must not fail -- the reference is cosmetic, the case is not."""
    from app.agents.dsr.services import case_service
    from app.db.models import DsrRequest

    taken = {"DSR-COLLIDE"}
    drawn: list[str] = []

    def _new_reference():
        value = "DSR-COLLIDE" if len(drawn) < 2 else "DSR-FREE01"
        drawn.append(value)
        return value

    async def _reference_taken(db, org_id, reference):
        return reference in taken

    async def _create(db, **kw):
        return DsrRequest(id=uuid.uuid4(), status=case.RECEIVED, received_at=NOW, **kw)

    async def _audit(db, **kw):
        return None

    monkeypatch.setattr(case_service, "new_reference", _new_reference)
    monkeypatch.setattr(case_service.dsr_repository, "reference_exists", _reference_taken)
    monkeypatch.setattr(case_service.dsr_repository, "create_request", _create)
    monkeypatch.setattr(case_service.audit_service, "record", _audit)

    class _DB:
        async def flush(self):
            return None

    request, is_new = await case_service.create_case(
        _DB(), org_id=ORG, raw_request="delete my data", requester_email="a@b.com", now=NOW,
    )
    assert is_new
    assert request.reference == "DSR-FREE01"
    assert len(drawn) == 3, "the collision was not retried"


# ── Standing invariant: the ORM and the migration describe the same tables ───────

def test_every_dsr_orm_column_exists_in_the_migration():
    """Three columns were added to both files during the build (data_source_id on
    evidence, identity_tables and record_key_columns on source authorizations). A
    column added to only one side is a runtime UndefinedColumn on the GB10 and
    nowhere else, because the test suite never touches a real database.
    """
    import pathlib
    import re

    from app.db import models

    migrations = pathlib.Path(__file__).parent.parent / "migrations"
    # Every migration, not just 0011: a DSR column may be added by a later one
    # (0013 adds dsr_actions.requester_explanation), and reading only the original
    # would report drift that does not exist -- or miss drift that does.
    text = "\n".join(f.read_text(encoding="utf-8") for f in sorted(migrations.glob("*.sql")))
    # Table-level constraints share the column indentation; they are not columns.
    keywords = {"unique", "primary", "foreign", "check", "constraint"}

    problems = []
    dsr_tables = [t for t in models.Base.metadata.tables if t.startswith("dsr_")]
    # Not pinned to a count: a new DSR table is a normal thing to add, and a test that
    # fails merely for existing teaches people to edit the number rather than read it.
    assert dsr_tables, "no DSR tables found in the ORM at all"

    for table_name in sorted(dsr_tables):
        body = re.search(rf"create table if not exists {table_name} \((.*?)\n\);", text, re.DOTALL)
        if body is None:
            problems.append(f"{table_name} has no CREATE TABLE in 0011")
            continue
        sql_cols = {
            name for name in re.findall(r"^\s{4}(\w+)\s+\S", body.group(1), re.MULTILINE)
            if name.lower() not in keywords
        }
        # Columns added by a later ALTER count too.
        sql_cols |= set(re.findall(
            rf"alter table {table_name} add column if not exists (\w+)", text
        ))
        orm_cols = {c.name for c in models.Base.metadata.tables[table_name].columns}
        if missing := orm_cols - sql_cols:
            problems.append(f"{table_name}: in the ORM, missing from 0011 -> {sorted(missing)}")
        if extra := sql_cols - orm_cols:
            problems.append(f"{table_name}: in 0011, missing from the ORM -> {sorted(extra)}")

    assert not problems, "ORM/migration drift:\n  " + "\n  ".join(problems)


def test_every_dsr_table_has_rls_and_a_tenant_policy():
    """Across EVERY migration, not just 0011. A DSR table added later (0014 adds
    dsr_retention_rules) must carry the same isolation as the originals, and a policy
    written as its own guarded statement counts as much as one written in a loop."""
    import pathlib
    import re

    from app.db import models

    migrations = pathlib.Path(__file__).parent.parent / "migrations"
    text = "\n".join(f.read_text(encoding="utf-8") for f in sorted(migrations.glob("*.sql")))
    dsr_tables = {t for t in models.Base.metadata.tables if t.startswith("dsr_")}

    rls_enabled = set(re.findall(r"alter table (\w+)\s+enable row level security", text))
    assert dsr_tables <= rls_enabled, f"no RLS on {sorted(dsr_tables - rls_enabled)}"

    # Two equally valid shapes: named in the array the policy loop iterates over, or a
    # `create policy tenant_isolation on <table>` of its own.
    policied = set()
    for loop in re.findall(r"foreach t in array array\[(.*?)\]", text, re.DOTALL):
        policied |= set(re.findall(r"'(\w+)'", loop))
    policied |= set(re.findall(r"create policy tenant_isolation on (\w+)", text))
    assert dsr_tables <= policied, f"no tenant_isolation policy for {sorted(dsr_tables - policied)}"


def test_every_dsr_table_carries_an_org_id():
    from app.db import models

    for name, table in models.Base.metadata.tables.items():
        if name.startswith("dsr_"):
            assert "org_id" in table.columns, f"{name} has no org_id; it cannot be tenant-scoped"


# ── Defect 6: a wholly failed execution still claimed EXECUTION_VERIFIED ─────────

def test_an_execution_where_nothing_succeeded_is_not_reported_as_verified():
    """EXECUTION_VERIFIED asserts the system confirmed the outcome (§23). Reaching it
    with zero successes and N failures states something that did not happen -- and it
    is terminal-adjacent, so the case then reads as processed."""
    from app.services import dsr_run_service

    status, code = dsr_run_service.status_after_execution(succeeded=0, failed=3, blocked=0)
    assert status == case.FAILED
    assert code == case.ERR_ACTION_FAILED


def test_a_fully_successful_execution_is_verified():
    from app.services import dsr_run_service

    status, code = dsr_run_service.status_after_execution(succeeded=3, failed=0, blocked=0)
    assert status == case.EXECUTION_VERIFIED
    assert code is None


def test_a_mixed_execution_is_verified_but_carries_a_partial_code():
    """Some records changed and some did not. The case proceeds to a response -- the
    requester is owed both halves of that story -- but it is not a clean success."""
    from app.services import dsr_run_service

    status, code = dsr_run_service.status_after_execution(succeeded=2, failed=1, blocked=0)
    assert status == case.EXECUTION_VERIFIED
    assert code == case.ERR_ACTION_PARTIAL


def test_blocked_actions_alone_make_an_execution_partial_not_failed():
    from app.services import dsr_run_service

    status, code = dsr_run_service.status_after_execution(succeeded=2, failed=0, blocked=1)
    assert status == case.EXECUTION_VERIFIED
    assert code == case.ERR_ACTION_PARTIAL


def test_a_failed_execution_status_is_recoverable():
    """A connector outage during execution must not permanently kill the case."""
    from app.agents.dsr.services import lifecycle

    status, _ = __import__(
        "app.services.dsr_run_service", fromlist=["x"]
    ).status_after_execution(succeeded=0, failed=1, blocked=0)
    assert not lifecycle.is_terminal(status)


# ── Defect 7: closing a partial case called it COMPLETED ─────────────────────────

def test_closing_a_case_with_unfinished_work_is_partially_completed():
    """§47: a requester told their case is "completed" when one record was retained
    under a retention rule has been told something untrue about their own data."""
    from app.services import dsr_run_service

    assert dsr_run_service.closing_status(blocked=0, failed=0) == case.COMPLETED
    assert dsr_run_service.closing_status(blocked=1, failed=0) == case.PARTIALLY_COMPLETED
    assert dsr_run_service.closing_status(blocked=0, failed=2) == case.PARTIALLY_COMPLETED


def test_both_closing_statuses_are_terminal():
    from app.agents.dsr.services import lifecycle

    assert lifecycle.is_terminal(case.COMPLETED)
    assert lifecycle.is_terminal(case.PARTIALLY_COMPLETED)


# ── Defect 8: table names were unqualified, so only `public` was reachable ───────

@pytest.mark.asyncio
async def test_a_source_whose_tables_live_in_a_named_schema_can_be_searched(monkeypatch):
    """Table names were quoted bare, so they resolved through search_path -- in
    practice `public` and nothing else. A customer whose DSR tables sit in their own
    schema could not be searched at all, and the failure would be an UndefinedTable
    from the driver rather than anything the allowlist could explain."""
    conn = _FakeConn([{"id": 1, "subscriber_email": "a@b.com", "plan": "pro"}])
    connector = PostgresDsrConnector(
        config=PostgresDsrConfig(
            host="h", port=5432, dbname="d", read_user="r", read_password="p",
            schema="billing_app",
        ),
        grant=_grant(),
    )
    monkeypatch.setattr(connector, "_connect", _stub_connect(conn))

    outcome = await connector.search_subject(identifiers={"email": "a@b.com"})

    sql = conn.queries[0][0]
    assert '"billing_app"."subscriptions"' in sql, f"table not schema-qualified: {sql}"
    assert outcome.matches[0].schema_name == "billing_app"


@pytest.mark.asyncio
async def test_no_schema_configured_still_produces_a_bare_table_name(monkeypatch):
    """The default stays exactly as it was, so an existing source keeps working."""
    conn = _FakeConn([{"id": 1, "subscriber_email": "a@b.com", "plan": "pro"}])
    connector = _connector()
    monkeypatch.setattr(connector, "_connect", _stub_connect(conn))

    await connector.search_subject(identifiers={"email": "a@b.com"})
    sql = conn.queries[0][0]
    assert 'FROM "subscriptions"' in sql


def test_a_malicious_schema_name_is_refused():
    from app.agents.dsr.errors import SourceNotAuthorizedError

    connector = PostgresDsrConnector(
        config=PostgresDsrConfig(
            host="h", port=5432, dbname="d", read_user="r", read_password="p",
            schema='public"; DROP SCHEMA x; --',
        ),
        grant=_grant(),
    )
    with pytest.raises(SourceNotAuthorizedError):
        connector._qualified("subscriptions")


# ── Defect 9: internal reasons were shown verbatim to the data subject ───────────

def test_a_configuration_failure_is_not_explained_to_the_requester():
    """The operator needs to read "enable execution and configure a write
    credential". The data subject must not: it is our internal state, it tells them
    nothing about their own data, and it reads as an excuse rather than an answer."""
    from app.agents.dsr.rules import constraints as rules

    found = rules.evaluate(
        operation=case.OP_DELETE_RECORD, table_name="subscriptions",
        grant=_grant(), now=NOW,
    )
    blocker = next(c for c in found if c.effect == rules.EFFECT_BLOCK)

    # Operator-facing text is unchanged.
    assert "write credential" in blocker.reason

    # Requester-facing text says what happened to THEIR data, and nothing about ours.
    subject_text = blocker.requester_explanation
    assert subject_text
    for leak in ("credential", "administrator", "allowlist", "authorized for DSR",
                 "configure", "source '"):
        assert leak not in subject_text.lower(), f"internal detail leaked: {leak!r}"


def test_a_retention_rule_IS_explained_to_the_requester():
    """The opposite case. Why an erasure was refused on policy grounds is exactly
    what a data principal is entitled to know, so this one discloses the substance --
    whose rule it was and until when."""
    from datetime import timedelta

    from app.agents.dsr.rules import constraints as rules

    rule = rules.RetentionRule(
        table_name="subscriptions", minimum_retention=timedelta(days=365 * 7),
        date_column="created_at", authority="Finance policy FIN-3",
    )
    found = rules.evaluate(
        operation=case.OP_DELETE_RECORD, table_name="subscriptions", grant=_grant(),
        record_snapshot={"created_at": "2025-01-01T00:00:00Z"},
        retention_rules=(rule,), now=NOW,
    )
    blocker = next(c for c in found if c.code == case.ERR_ACTION_BLOCKED)
    text = blocker.requester_explanation
    assert "Finance policy FIN-3" in text
    # The date the rule actually implies, computed the same way the rule is -- not
    # hard-coded, so leap years cannot make the test wrong about the code.
    expected = (datetime(2025, 1, 1, tzinfo=UTC) + rule.minimum_retention).date().isoformat()
    assert expected in text, f"{expected} not in {text}"


@pytest.mark.asyncio
async def test_a_blocked_action_carries_both_texts(monkeypatch):
    """The plan keeps the operator's reason; the action also carries the sentence the
    response will use."""
    from app.agents.dsr.services import planning_service
    from app.db.models import DsrEvidence

    evidence = DsrEvidence(
        id=uuid.uuid4(), org_id=ORG, request_id=uuid.uuid4(), search_run_id=uuid.uuid4(),
        data_source_id=uuid.uuid4(), source_name="billing", table_name="subscriptions",
        matched_column="subscriber_email", identifier_kind="email", match_type="exact",
        confidence=1.0, record_reference={"id": 1}, record_snapshot={"plan": "pro"},
    )
    request = SimpleNamespace(
        id=uuid.uuid4(), org_id=ORG, reference="DSR-X", request_type=case.DELETION,
    )
    action, _ = planning_service._plan_one(
        request=request, evidence=evidence, grant=_grant(),
        corrections={}, retention_rules=(),
    )
    assert action.status == "blocked"
    assert "write credential" in action.blocked_reason           # operator
    assert "credential" not in action.requester_explanation.lower()  # data subject
    assert action.requester_explanation.strip()


def test_the_response_uses_the_requester_text_not_the_internal_reason():
    from app.agents.dsr.services import response_service
    from app.db.models import DsrRequest

    request = DsrRequest(
        id=uuid.uuid4(), org_id=ORG, reference="DSR-Y", raw_request="delete my data",
        request_type=case.DELETION, due_at=NOW + timedelta(days=30),
        received_at=NOW, status=case.RESPONSE_PENDING,
    )
    blocked = SimpleNamespace(
        status="blocked", source_name="billing", table_name="subscriptions",
        operation=case.OP_RETAIN, id=uuid.uuid4(),
        blocked_reason="source 'billing' allows execution but names no write credential",
        requester_explanation="We were not able to change this record automatically. "
                              "It has been referred to our team, who will complete it "
                              "and confirm the outcome to you.",
    )
    evidence = SimpleNamespace(
        source_name="billing", table_name="subscriptions", record_snapshot={"plan": "pro"},
    )
    run = SimpleNamespace(
        source_name="billing", status="completed", match_count=1,
        tables_searched=["subscriptions"], error_code=None, id=uuid.uuid4(),
    )
    body = response_service._compose(request, [evidence], [run], [], [blocked])

    assert "write credential" not in body, "an internal reason reached the requester"
    assert "referred to our team" in body
