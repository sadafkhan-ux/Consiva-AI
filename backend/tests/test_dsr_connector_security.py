"""DSR connector security (prompt §16, §24, §37).

These are the tests that matter most in Agent 3. The connector is the only component
that touches a customer's production database, and it is the only one that can
delete from it. Everything here is about what it refuses to do.

No live Postgres: a fake asyncpg connection records the SQL and the bound arguments,
so the tests can assert on the statement that WOULD have been sent.
"""

import uuid
from types import SimpleNamespace

import pytest

from app.agents.dsr.connectors import authorization, base
from app.agents.dsr.connectors.postgres import PostgresDsrConfig, PostgresDsrConnector
from app.agents.dsr.errors import (
    ActionFailedError,
    SearchFailedError,
    SourceNotAuthorizedError,
    VerificationFailedError,
)
from app.agents.dsr.schemas import case


def make_auth(**kw):
    """A dsr_source_authorizations row, as the repository would return it."""
    return SimpleNamespace(
        id=uuid.uuid4(),
        enabled=kw.get("enabled", True),
        searchable_tables=kw.get("searchable_tables", ["customers", "orders"]),
        identity_tables=kw.get("identity_tables", ["customers"]),
        identifier_columns=kw.get("identifier_columns", {
            "customers": {"email": "email", "phone": "phone"},
            "orders": {"email": "customer_email"},
        }),
        returnable_columns=kw.get("returnable_columns", {
            "customers": ["name", "email", "phone"],
            "orders": ["order_id", "amount"],
        }),
        erasable_columns=kw.get("erasable_columns", {"customers": ["name", "phone", "email"]}),
        allow_execution=kw.get("allow_execution", False),
        write_credential_ref=kw.get("write_credential_ref", None),
    )


def make_grant(**kw):
    return authorization.resolve(make_auth(**kw), source_name="testdb")


def make_connector(**kw):
    config = PostgresDsrConfig(
        host="db.internal", port=5432, dbname="app",
        read_user="dsr_read", read_password="r-secret",
        write_user=kw.pop("write_user", None), write_password=kw.pop("write_password", None),
    )
    return PostgresDsrConnector(config=config, grant=make_grant(**kw))


# ── Identifier validation: the SQL-injection boundary ────────────────────────────

@pytest.mark.parametrize(
    "malicious",
    [
        'customers"; DROP TABLE customers; --',
        "customers; DELETE FROM customers",
        'customers" OR "1"="1',
        "customers--",
        "cus tomers",
        "customers'",
        "customers\\",
        "customers\x00",
        "1customers",
        "",
        "*",
        "a" * 64,
    ],
)
def test_malicious_identifiers_are_refused_not_sanitized(malicious):
    """Silently stripping the dangerous part would mean querying something other
    than what the configuration named, which is its own bug."""
    with pytest.raises(SourceNotAuthorizedError):
        base.validate_identifier(malicious, kind="table")
    with pytest.raises(SourceNotAuthorizedError):
        base.quote_identifier(malicious, kind="table")


@pytest.mark.parametrize("ok", ["customers", "customer_email", "_private", "t1", "orders$ext"])
def test_legitimate_identifiers_pass_and_are_quoted(ok):
    assert base.validate_identifier(ok) == ok
    assert base.quote_identifier(ok) == f'"{ok}"'


def test_a_malicious_name_in_the_allowlist_is_rejected_at_resolve_time():
    """Defence in depth: even if an administrator (or a compromised admin API) put a
    quoted injection into the allowlist, it never reaches a query builder."""
    with pytest.raises(SourceNotAuthorizedError):
        make_grant(searchable_tables=['customers"; DROP TABLE x; --'])


# ── Allowlists fail closed ───────────────────────────────────────────────────────

def test_unconfigured_source_permits_nothing():
    with pytest.raises(SourceNotAuthorizedError):
        authorization.resolve(None, source_name="unknown")


def test_disabled_authorization_permits_nothing():
    with pytest.raises(SourceNotAuthorizedError):
        make_grant(enabled=False)


def test_empty_allowlist_permits_no_table():
    grant = make_grant(searchable_tables=[], identity_tables=[], identifier_columns={},
                       returnable_columns={}, erasable_columns={})
    with pytest.raises(SourceNotAuthorizedError):
        grant.assert_searchable("customers")


def test_table_outside_the_allowlist_is_refused():
    grant = make_grant()
    grant.assert_searchable("customers")  # configured
    with pytest.raises(SourceNotAuthorizedError):
        grant.assert_searchable("admin_users")


def test_identity_tables_must_be_searchable():
    with pytest.raises(SourceNotAuthorizedError):
        make_grant(identity_tables=["secret_table"])


def test_identifier_columns_must_name_a_searchable_table():
    with pytest.raises(SourceNotAuthorizedError):
        make_grant(identifier_columns={"admin_users": {"email": "email"}})


def test_unknown_identifier_kind_is_refused():
    with pytest.raises(SourceNotAuthorizedError):
        make_grant(identifier_columns={"customers": {"ssn": "ssn"}})


# ── Execution authorization ──────────────────────────────────────────────────────

def test_search_authorization_does_not_imply_execution():
    """The whole point of a separate DSR authorization: a source you may read is not
    thereby a source you may delete from."""
    grant = make_grant(allow_execution=False)
    grant.assert_executable("customers", case.OP_DISCLOSE)  # reads are fine
    with pytest.raises(SourceNotAuthorizedError):
        grant.assert_executable("customers", case.OP_DELETE_RECORD)
    with pytest.raises(SourceNotAuthorizedError):
        grant.assert_executable("customers", case.OP_UPDATE_FIELD)


def test_execution_without_a_write_credential_is_refused():
    """allow_execution alone is not enough -- writing on the read connection is
    exactly what the separate credential exists to prevent."""
    grant = make_grant(allow_execution=True, write_credential_ref=None)
    with pytest.raises(SourceNotAuthorizedError):
        grant.assert_executable("customers", case.OP_DELETE_RECORD)


def test_execution_allowed_when_fully_configured():
    grant = make_grant(allow_execution=True, write_credential_ref="TESTDB_WRITE_PASSWORD")
    grant.assert_executable("customers", case.OP_DELETE_RECORD)


def test_non_mutating_operations_never_need_execution_rights():
    grant = make_grant(allow_execution=False)
    for op in (case.OP_DISCLOSE, case.OP_RETAIN, case.OP_NO_OP):
        grant.assert_executable("customers", op)


def test_unknown_operation_is_refused():
    with pytest.raises(SourceNotAuthorizedError):
        make_grant().assert_executable("customers", "drop_everything")


# ── Erasable-column allowlist ────────────────────────────────────────────────────

def test_column_outside_the_erasable_list_is_blocked():
    grant = make_grant(allow_execution=True, write_credential_ref="W")
    grant.assert_erasable("customers", "phone")
    with pytest.raises(SourceNotAuthorizedError):
        grant.assert_erasable("customers", "account_balance")


def test_a_write_touching_one_unauthorized_column_is_refused_whole():
    """Not partially applied: a correction that would update an authorized column and
    an unauthorized one is refused entirely."""
    grant = make_grant(allow_execution=True, write_credential_ref="W")
    with pytest.raises(SourceNotAuthorizedError):
        grant.assert_writable_columns("customers", {"phone": "+91...", "is_admin": True})


def test_an_empty_write_payload_is_refused():
    grant = make_grant(allow_execution=True, write_credential_ref="W")
    with pytest.raises(SourceNotAuthorizedError):
        grant.assert_writable_columns("customers", {})


# ── Values are parameterized, never interpolated ─────────────────────────────────

@pytest.mark.asyncio
async def test_requester_value_is_bound_not_interpolated(monkeypatch):
    """The injection attempt arrives as a VALUE. It must appear in the argument list
    and never in the statement text."""
    conn = FakeConnection(rows=[])
    connector = make_connector()
    monkeypatch.setattr(connector, "_connect", _stub_connect(conn))

    payload = "victim@example.com' OR 1=1 --"
    await connector.search_subject(identifiers={"email": payload})

    assert conn.queries, "no query was issued"
    for sql, args in conn.queries:
        assert payload not in sql, "the requester's value reached the statement text"
        assert "OR 1=1" not in sql
    assert any(payload in args for _, args in conn.queries), "value was not bound"


@pytest.mark.asyncio
async def test_search_never_selects_star(monkeypatch):
    """§18: data minimization is enforced by the projection, not by filtering after
    the fact."""
    conn = FakeConnection(rows=[])
    connector = make_connector()
    monkeypatch.setattr(connector, "_connect", _stub_connect(conn))
    await connector.search_subject(identifiers={"email": "a@b.com"})
    for sql, _ in conn.queries:
        assert "SELECT *" not in sql.upper()


@pytest.mark.asyncio
async def test_search_only_touches_allowlisted_tables(monkeypatch):
    conn = FakeConnection(rows=[])
    connector = make_connector()
    monkeypatch.setattr(connector, "_connect", _stub_connect(conn))
    await connector.search_subject(identifiers={"email": "a@b.com"})
    for sql, _ in conn.queries:
        assert '"customers"' in sql or '"orders"' in sql
        assert "admin" not in sql.lower()


@pytest.mark.asyncio
async def test_search_with_no_usable_identifier_fails_loudly(monkeypatch):
    connector = make_connector()
    monkeypatch.setattr(connector, "_connect", _stub_connect(FakeConnection(rows=[])))
    with pytest.raises(SearchFailedError):
        await connector.search_subject(identifiers={"email": "   "})


@pytest.mark.asyncio
async def test_row_cap_cannot_be_raised_above_the_hard_limit(monkeypatch):
    conn = FakeConnection(rows=[])
    connector = make_connector()
    monkeypatch.setattr(connector, "_connect", _stub_connect(conn))
    await connector.search_subject(identifiers={"email": "a@b.com"}, limit=10_000)
    for sql, _ in conn.queries:
        limit = int(sql.rsplit("LIMIT", 1)[1].strip())
        assert limit <= base.MAX_ROWS_PER_TABLE + 1


# ── Multiple subjects ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_two_rows_in_an_identity_table_are_two_subjects(monkeypatch):
    conn = FakeConnection(rows=[
        {"id": 1, "email": "a@b.com", "name": "A", "phone": "1"},
        {"id": 2, "email": "a@b.com", "name": "B", "phone": "2"},
    ])
    connector = make_connector(searchable_tables=["customers"], identity_tables=["customers"],
                               identifier_columns={"customers": {"email": "email"}},
                               returnable_columns={"customers": ["name", "email"]})
    monkeypatch.setattr(connector, "_connect", _stub_connect(conn))
    outcome = await connector.search_subject(identifiers={"email": "a@b.com"})
    assert outcome.distinct_subjects == 2


@pytest.mark.asyncio
async def test_many_rows_in_a_related_table_are_one_subject(monkeypatch):
    """An orders table with five rows for one email is one person with five orders --
    flagging that as ambiguous would send every ordinary access request to review."""
    conn = FakeConnection(rows=[
        {"id": i, "customer_email": "a@b.com", "order_id": f"O{i}", "amount": 10} for i in range(5)
    ])
    connector = make_connector(searchable_tables=["orders"], identity_tables=[],
                               identifier_columns={"orders": {"email": "customer_email"}},
                               returnable_columns={"orders": ["order_id", "amount"]},
                               erasable_columns={})
    monkeypatch.setattr(connector, "_connect", _stub_connect(conn))
    outcome = await connector.search_subject(identifiers={"email": "a@b.com"})
    assert outcome.distinct_subjects == 1
    assert not connector.has_identity_table


# ── Execution: verification is a separate claim from the write ───────────────────

@pytest.mark.asyncio
async def test_unverified_update_raises_instead_of_reporting_success(monkeypatch):
    """§24: never mark an action completed merely because the command returned
    successfully. Here the UPDATE reports a row but the read-back disagrees."""
    conn = FakeConnection(
        rows=[], execute_status="UPDATE 1",
        fetchrow_result={"phone": "OLD-VALUE"},  # not what we asked for
    )
    connector = make_connector(allow_execution=True, write_credential_ref="W",
                               write_user="dsr_write", write_password="w-secret")
    monkeypatch.setattr(connector, "_connect", _stub_connect(conn))
    with pytest.raises(VerificationFailedError):
        await connector.execute_action(
            table_name="customers", record_reference={"id": 1},
            operation=case.OP_UPDATE_FIELD, payload={"phone": "NEW-VALUE"},
        )


@pytest.mark.asyncio
async def test_verified_update_reports_both_facts_separately(monkeypatch):
    conn = FakeConnection(rows=[], execute_status="UPDATE 1",
                          fetchrow_result={"phone": "NEW-VALUE"})
    connector = make_connector(allow_execution=True, write_credential_ref="W",
                               write_user="dsr_write", write_password="w-secret")
    monkeypatch.setattr(connector, "_connect", _stub_connect(conn))
    outcome = await connector.execute_action(
        table_name="customers", record_reference={"id": 1},
        operation=case.OP_UPDATE_FIELD, payload={"phone": "NEW-VALUE"},
    )
    assert outcome.rows_affected == 1
    assert outcome.verified is True


@pytest.mark.asyncio
async def test_delete_is_verified_by_absence(monkeypatch):
    conn = FakeConnection(rows=[], execute_status="DELETE 1", fetchval_result=0)
    connector = make_connector(allow_execution=True, write_credential_ref="W",
                               write_user="dsr_write", write_password="w-secret")
    monkeypatch.setattr(connector, "_connect", _stub_connect(conn))
    outcome = await connector.execute_action(
        table_name="customers", record_reference={"id": 1},
        operation=case.OP_DELETE_RECORD, payload={},
    )
    assert outcome.verified is True
    assert outcome.verification_detail["rows_remaining"] == 0


@pytest.mark.asyncio
async def test_delete_that_left_the_row_behind_raises(monkeypatch):
    conn = FakeConnection(rows=[], execute_status="DELETE 1", fetchval_result=1)
    connector = make_connector(allow_execution=True, write_credential_ref="W",
                               write_user="dsr_write", write_password="w-secret")
    monkeypatch.setattr(connector, "_connect", _stub_connect(conn))
    with pytest.raises(VerificationFailedError):
        await connector.execute_action(
            table_name="customers", record_reference={"id": 1},
            operation=case.OP_DELETE_RECORD, payload={},
        )


@pytest.mark.asyncio
async def test_execution_refuses_an_empty_record_reference(monkeypatch):
    """A WHERE clause with no conditions would apply the operation to every row."""
    connector = make_connector(allow_execution=True, write_credential_ref="W",
                               write_user="dsr_write", write_password="w-secret")
    monkeypatch.setattr(connector, "_connect", _stub_connect(FakeConnection(rows=[])))
    with pytest.raises(ActionFailedError):
        await connector.execute_action(
            table_name="customers", record_reference={},
            operation=case.OP_DELETE_RECORD, payload={},
        )


@pytest.mark.asyncio
async def test_write_without_resolved_credential_never_opens_a_connection():
    """allow_execution is set and the grant names a credential, but none was
    resolved -- the connector must not fall back to the read connection."""
    connector = make_connector(allow_execution=True, write_credential_ref="W")
    with pytest.raises(SourceNotAuthorizedError):
        await connector.execute_action(
            table_name="customers", record_reference={"id": 1},
            operation=case.OP_DELETE_RECORD, payload={},
        )


# ── Credentials never leak ───────────────────────────────────────────────────────

def test_config_repr_redacts_both_passwords():
    config = PostgresDsrConfig(
        host="h", port=5432, dbname="d", read_user="ru", read_password="READ-SECRET",
        write_user="wu", write_password="WRITE-SECRET",
    )
    text = repr(config)
    assert "READ-SECRET" not in text
    assert "WRITE-SECRET" not in text
    assert "redacted" in text


# ── fakes ────────────────────────────────────────────────────────────────────────

class FakeConnection:
    """Records every statement and its bound arguments."""

    def __init__(self, *, rows, execute_status="UPDATE 0", fetchrow_result=None, fetchval_result=None):
        self._rows = rows
        self._execute_status = execute_status
        self._fetchrow_result = fetchrow_result
        self._fetchval_result = fetchval_result
        self.queries: list[tuple[str, tuple]] = []

    async def fetch(self, sql, *args):
        self.queries.append((sql, args))
        return list(self._rows)

    async def fetchrow(self, sql, *args):
        self.queries.append((sql, args))
        return self._fetchrow_result

    async def fetchval(self, sql, *args):
        self.queries.append((sql, args))
        return self._fetchval_result

    async def execute(self, sql, *args):
        self.queries.append((sql, args))
        return self._execute_status

    async def close(self):
        return None

    def transaction(self):
        return _FakeTransaction()


class _FakeTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _stub_connect(conn):
    async def _connect(*, write: bool):
        if write and not conn.__dict__.get("_allow_write", True):
            raise AssertionError("unexpected write connection")
        return conn
    return _connect
