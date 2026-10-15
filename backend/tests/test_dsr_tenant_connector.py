"""Tenant-level DSR connector: configure a source ONCE, reuse it, test it, never leak it.

An administrator stores a source's write credential encrypted (migration 0037) instead
of naming an environment variable someone has to set and redeploy for every
write-enabled source, and can test the configuration against the real source without
reading a row of personal data. Synthetic data and in-memory fakes only.
"""

import uuid
from types import SimpleNamespace

import asyncpg
import pytest
from pydantic import SecretStr

from app.agents.dsr.connectors import authorization, factory
from app.agents.dsr.errors import SourceNotAuthorizedError
from app.api.v1.routes import dsr as dsr_routes
from app.core.security import CurrentUser
from app.db.repositories import dsr_repository, ropa_repository
from app.services import audit_service

from tests.test_dsr_connector_security import (
    FakeConnection, _stub_connect, make_auth, make_connector,
)

ORG = uuid.uuid4()
ADMIN = CurrentUser(user_id=str(uuid.uuid4()), org_id=str(ORG), role="admin")
PLAINTEXT = "w-s3cret-DO-NOT-LEAK"


class _Db:
    async def flush(self):
        return None

    async def commit(self):
        return None


# ── The stored credential is a real credential, equivalent to naming one ─────────

def test_a_stored_write_credential_satisfies_the_execution_gate():
    row = make_auth(allow_execution=True, write_credential_ref=None)
    row.write_credential_ciphertext = "ciphertext-not-plaintext"
    grant = authorization.resolve(row, source_name="crm")

    assert grant.has_write_credential
    grant.assert_executable("customers", "delete_record")  # must not raise


def test_no_stored_credential_and_no_ref_still_refuses_to_write():
    grant = authorization.resolve(make_auth(allow_execution=True), source_name="crm")

    assert not grant.has_write_credential
    with pytest.raises(SourceNotAuthorizedError, match="no write credential"):
        grant.assert_executable("customers", "delete_record")


def test_the_grant_carries_only_the_fact_of_a_stored_secret_never_the_value():
    row = make_auth(allow_execution=True)
    row.write_credential_ciphertext = "ciphertext"
    grant = authorization.resolve(row, source_name="crm")

    assert "ciphertext" not in repr(grant)
    assert grant.write_credential_stored is True


# ── The factory resolves it, and prefers it to an environment entry ──────────────

def _source():
    return SimpleNamespace(
        name="crm", connector="postgres", config={"host": "db.internal", "dbname": "app", "user": "u"},
        credential_ref="READ_REF", credential_ciphertext=None,
    )


def test_factory_uses_the_stored_write_credential_over_the_environment(monkeypatch):
    monkeypatch.setenv("READ_REF", "read-pw")
    monkeypatch.setenv("WRITE_REF", "env-write-pw")
    monkeypatch.setattr(factory, "decrypt_credential", lambda c: PLAINTEXT if c == "ct" else "wrong")
    row = make_auth(allow_execution=True, write_credential_ref="WRITE_REF")
    row.write_credential_ciphertext = "ct"

    connector = factory.build_connector(data_source=_source(), authorization=row, for_execution=True)

    assert connector._config.write_password == PLAINTEXT


def test_factory_still_falls_back_to_the_environment_entry(monkeypatch):
    """A source configured the old way, before the stored credential existed, must keep
    working -- the change is additive."""
    monkeypatch.setenv("READ_REF", "read-pw")
    monkeypatch.setenv("WRITE_REF", "env-write-pw")
    row = make_auth(allow_execution=True, write_credential_ref="WRITE_REF")

    connector = factory.build_connector(data_source=_source(), authorization=row, for_execution=True)

    assert connector._config.write_password == "env-write-pw"


def test_a_search_connector_never_resolves_the_write_credential(monkeypatch):
    monkeypatch.setenv("READ_REF", "read-pw")
    monkeypatch.setattr(
        factory, "decrypt_credential",
        lambda c: (_ for _ in ()).throw(AssertionError("write secret decrypted for a search")),
    )
    row = make_auth(allow_execution=True, write_credential_ref="WRITE_REF")
    row.write_credential_ciphertext = "ct"

    connector = factory.build_connector(data_source=_source(), authorization=row, for_execution=False)

    assert connector._config.write_password is None


# ── PUT /config/sources: encrypted once, never echoed ────────────────────────────

def _put_harness(monkeypatch):
    seen = {"audit": [], "upsert": None}
    source = SimpleNamespace(id=uuid.uuid4(), name="crm", connector="postgres")

    async def get_data_source(db, source_id, org_id):
        return source

    async def upsert(db, **kw):
        seen["upsert"] = kw
        return SimpleNamespace(id=uuid.uuid4(), allow_execution=kw["allow_execution"])

    async def audit(db, **kw):
        seen["audit"].append(kw)

    monkeypatch.setattr(ropa_repository, "get_data_source", get_data_source)
    monkeypatch.setattr(dsr_repository, "upsert_source_authorization", upsert)
    monkeypatch.setattr(audit_service, "record", audit)
    monkeypatch.setattr(dsr_routes, "encrypt_credential", lambda s: f"enc::{s[::-1]}")
    return source, seen


def _payload(source, **kw):
    return dsr_routes.SourceAuthorizationIn(
        data_source_id=source.id, searchable_tables=["customers"],
        identifier_columns={"customers": {"email": "email"}},
        returnable_columns={"customers": ["name"]}, erasable_columns={"customers": ["name"]},
        allow_execution=True, **kw,
    )


@pytest.mark.asyncio
async def test_put_stores_only_ciphertext_and_never_echoes_the_secret(monkeypatch):
    source, seen = _put_harness(monkeypatch)

    result = await dsr_routes.put_source_authorization(
        payload=_payload(source, write_secret=SecretStr(PLAINTEXT)), user=ADMIN, db=_Db(),
    )

    stored = seen["upsert"]["write_credential_ciphertext"]
    assert stored and PLAINTEXT not in stored, "the plaintext reached the repository"
    assert PLAINTEXT not in repr(result)
    assert PLAINTEXT not in repr(seen["audit"]), "the secret entered the audit trail"
    assert seen["audit"][0]["after"]["write_secret_changed"] is True


@pytest.mark.asyncio
async def test_a_later_put_without_a_secret_keeps_the_stored_one(monkeypatch):
    source, seen = _put_harness(monkeypatch)

    await dsr_routes.put_source_authorization(payload=_payload(source), user=ADMIN, db=_Db())

    assert seen["upsert"]["write_credential_ciphertext"] is None  # None == "leave it alone"
    assert seen["upsert"]["clear_write_credential"] is False
    assert seen["audit"][0]["after"]["write_secret_changed"] is False


@pytest.mark.asyncio
async def test_a_secret_can_be_cleared_explicitly(monkeypatch):
    source, seen = _put_harness(monkeypatch)

    await dsr_routes.put_source_authorization(
        payload=_payload(source, clear_write_secret=True), user=ADMIN, db=_Db(),
    )

    assert seen["upsert"]["clear_write_credential"] is True


def test_the_secret_is_not_visible_in_the_models_own_repr():
    payload = dsr_routes.SourceAuthorizationIn(
        data_source_id=uuid.uuid4(), write_secret=SecretStr(PLAINTEXT),
    )
    assert PLAINTEXT not in repr(payload) and PLAINTEXT not in str(payload)


@pytest.mark.asyncio
async def test_a_non_admin_cannot_store_a_credential(monkeypatch):
    source, seen = _put_harness(monkeypatch)
    member = CurrentUser(user_id=str(uuid.uuid4()), org_id=str(ORG), role="member")

    with pytest.raises(Exception, match="administrator"):
        await dsr_routes.put_source_authorization(
            payload=_payload(source, write_secret=SecretStr(PLAINTEXT)), user=member, db=_Db(),
        )
    assert seen["upsert"] is None


# ── The health check: read-only, no row, no secret, honest about its limits ──────

@pytest.mark.asyncio
async def test_health_check_selects_zero_rows_and_reports_each_table(monkeypatch):
    conn = FakeConnection(rows=[])
    connector = make_connector()
    monkeypatch.setattr(connector, "_connect", _stub_connect(conn))

    report = await connector.check_health()

    assert report["reachable"] is True and report["all_tables_ok"] is True
    assert {t["table"] for t in report["tables"]} == {"customers", "orders"}
    assert conn.queries and all(sql.rstrip().endswith("LIMIT 0") for sql, _ in conn.queries), (
        "a health check must never read a row"
    )
    assert all(not args for _, args in conn.queries)


@pytest.mark.asyncio
async def test_health_check_reports_a_missing_column_without_the_driver_text(monkeypatch):
    class _Boom(asyncpg.UndefinedColumnError):
        pass

    class _Conn(FakeConnection):
        async def fetch(self, sql, *args):
            if '"orders"' in sql:
                raise _Boom('column "customer_email" of relation "orders" does not exist')
            return await super().fetch(sql, *args)

    connector = make_connector()
    monkeypatch.setattr(connector, "_connect", _stub_connect(_Conn(rows=[])))

    report = await connector.check_health()

    bad = next(t for t in report["tables"] if t["table"] == "orders")
    assert bad["ok"] is False and report["all_tables_ok"] is False
    assert "customer_email" not in repr(bad), "driver text must not be echoed"


@pytest.mark.asyncio
async def test_health_check_never_claims_the_write_path_works(monkeypatch):
    conn = FakeConnection(rows=[])
    connector = make_connector(
        allow_execution=True, write_credential_ref="W", write_user="dsr_write", write_password="x",
    )
    monkeypatch.setattr(connector, "_connect", _stub_connect(conn))

    report = await connector.check_health()

    assert report["write_credential_resolved"] is True
    assert report["write_path_verified"] is False, "no write is attempted, so none is verified"


@pytest.mark.asyncio
async def test_health_route_reports_unreachable_without_leaking_anything(monkeypatch):
    source = SimpleNamespace(id=uuid.uuid4(), name="crm", connector="postgres")
    row = SimpleNamespace(id=uuid.uuid4(), allow_execution=False)
    audits = []

    async def get_data_source(db, sid, org_id):
        return source

    async def get_auth(db, sid, org_id):
        return row

    async def audit(db, **kw):
        audits.append(kw)

    class _Down:
        async def check_health(self):
            from app.agents.dsr.errors import ConnectorUnavailableError
            raise ConnectorUnavailableError("source 'crm' is unreachable or rejected the connection")

    monkeypatch.setattr(ropa_repository, "get_data_source", get_data_source)
    monkeypatch.setattr(dsr_repository, "get_source_authorization", get_auth)
    monkeypatch.setattr(audit_service, "record", audit)
    monkeypatch.setattr(dsr_routes, "build_connector", lambda **kw: _Down())

    result = await dsr_routes.test_source_connection(
        data_source_id=source.id, user=ADMIN, db=_Db(),
    )

    assert result["healthy"] is False and result["error_code"] == "CONNECTOR_UNAVAILABLE"
    assert result["reachable"] is False
    assert "does_not_prove" in result, "a passing/failing test must state what it does not prove"
    assert audits and audits[0]["after"]["healthy"] is False


@pytest.mark.asyncio
async def test_health_route_refuses_a_source_with_no_authorization(monkeypatch):
    source = SimpleNamespace(id=uuid.uuid4(), name="crm", connector="postgres")

    async def get_data_source(db, sid, org_id):
        return source

    async def get_auth(db, sid, org_id):
        return None

    monkeypatch.setattr(ropa_repository, "get_data_source", get_data_source)
    monkeypatch.setattr(dsr_repository, "get_source_authorization", get_auth)

    with pytest.raises(SourceNotAuthorizedError, match="no DSR authorization"):
        await dsr_routes.test_source_connection(data_source_id=source.id, user=ADMIN, db=_Db())


# ── A missing encryption key is a clear 503, not an opaque 500 ───────────────────

def _no_key(monkeypatch):
    from app.agents.ropa.connectors.factory import CredentialEncryptionNotConfigured

    def raise_unset(secret):
        raise CredentialEncryptionNotConfigured("ROPA_CREDENTIAL_ENCRYPTION_KEY is not set")
    monkeypatch.setattr(factory, "_ropa_encrypt_credential", raise_unset)


def test_the_encrypt_wrapper_turns_a_missing_key_into_a_503_that_names_the_setting(monkeypatch):
    from app.agents.dsr.errors import EncryptionNotConfiguredError

    _no_key(monkeypatch)
    with pytest.raises(EncryptionNotConfiguredError) as caught:
        factory.encrypt_credential(PLAINTEXT)

    assert caught.value.status_code == 503 and caught.value.code == "ENCRYPTION_NOT_CONFIGURED"
    assert "ROPA_CREDENTIAL_ENCRYPTION_KEY" in str(caught.value)
    assert PLAINTEXT not in str(caught.value)


def test_pushed_evidence_without_a_key_fails_clearly_instead_of_crashing(monkeypatch):
    from app.agents.dsr.errors import EncryptionNotConfiguredError
    from app.agents.dsr.schemas.evidence_push import EvidencePush
    from app.agents.dsr.services import evidence_push_service

    _no_key(monkeypatch)
    push = EvidencePush.model_validate({
        "source_name": "crm", "subject": {"email": "a@example.invalid"},
        "tables": [{
            "table_name": "customers", "match_count": 1, "columns": ["id", "email"],
            "identifier_columns": {"email": "email"}, "record_key_columns": ["id"],
            "rows": [{"id": 1, "email": "a@example.invalid"}],
        }],
    })
    with pytest.raises(EncryptionNotConfiguredError):
        evidence_push_service.encrypt_push_payload(push)


@pytest.mark.asyncio
async def test_storing_a_write_secret_without_a_key_stores_nothing_and_says_why(monkeypatch):
    from app.agents.dsr.errors import EncryptionNotConfiguredError

    source, seen = _put_harness(monkeypatch)
    _no_key(monkeypatch)
    monkeypatch.setattr(dsr_routes, "encrypt_credential", factory.encrypt_credential)

    with pytest.raises(EncryptionNotConfiguredError):
        await dsr_routes.put_source_authorization(
            payload=_payload(source, write_secret=SecretStr(PLAINTEXT)), user=ADMIN, db=_Db(),
        )
    assert seen["upsert"] is None, "nothing may be saved when the secret could not be encrypted"
    assert PLAINTEXT not in repr(seen["audit"])


@pytest.mark.asyncio
async def test_a_put_without_a_secret_does_not_need_the_key(monkeypatch):
    source, seen = _put_harness(monkeypatch)
    _no_key(monkeypatch)
    monkeypatch.setattr(dsr_routes, "encrypt_credential", factory.encrypt_credential)

    await dsr_routes.put_source_authorization(payload=_payload(source), user=ADMIN, db=_Db())

    assert seen["upsert"] is not None
