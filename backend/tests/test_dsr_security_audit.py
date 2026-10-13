"""Platform-wide security hardening audit, DSR lane (prompt §16, §37, §40).

Companion to tests/test_dsr_connector_security.py (which already covers SQL
injection, allowlist enforcement, execution verification and credential
redaction in the connector's repr). This file covers what that one does not:

  * SSRF/internal-address protection on the Postgres connector's real socket
    connection (test_dsr_connector_security.py never exercises `_connect`
    itself -- every test there monkeypatches it away).
  * Connection/statement timeouts actually reaching the real connection, not
    just being declared on the config dataclass.
  * The DSR factory's READ-credential resolution supporting the same
    ciphertext path Agent 2's (ROPA's) factory already supports, since both
    read the same `ropa_data_sources` row.
  * Credential-safety of error messages raised by the factory's secret
    resolution.

No live Postgres and no real DNS/network call in any test here: `asyncpg.
connect` is always monkeypatched, and the one "blocked host" case uses a
literal loopback IP, which `getaddrinfo` resolves without a network round
trip.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.agents.dsr.connectors import factory
from app.agents.dsr.connectors.authorization import resolve
from app.agents.dsr.connectors.postgres import PostgresDsrConfig, PostgresDsrConnector
from app.agents.dsr.errors import ConnectorUnavailableError, SourceNotAuthorizedError


def _grant(**kw):
    auth = SimpleNamespace(
        id=uuid.uuid4(),
        enabled=True,
        searchable_tables=kw.get("searchable_tables", ["customers"]),
        identity_tables=kw.get("identity_tables", ["customers"]),
        identifier_columns=kw.get("identifier_columns", {"customers": {"email": "email"}}),
        returnable_columns=kw.get("returnable_columns", {"customers": ["email"]}),
        erasable_columns=kw.get("erasable_columns", {}),
        record_key_columns=kw.get("record_key_columns", {}),
        allow_execution=kw.get("allow_execution", False),
        write_credential_ref=kw.get("write_credential_ref", None),
    )
    return resolve(auth, source_name="testdb")


# ── SSRF: the Postgres connector must validate the host before connecting ───────
#
# Confirmed gap (now fixed): PostgresDsrConnector._connect handed `self._config.host`
# straight to asyncpg.connect with no check against private/loopback/link-local/
# cloud-metadata ranges -- the same gap the Purpose Classifier's read_postgres had
# (app/agents/purpose/connectors/structured.py) before it gained an
# `assert_safe_host` guard. A `ropa_data_sources.config["host"]` is administrator-set,
# not supplied per-request by an arbitrary caller, but any org admin can set it, and
# this connector runs on a shared worker alongside every other org's requests -- a
# source pointed at 169.254.169.254 or 127.0.0.1 would turn DSR search/execute into
# an SSRF / internal-network-reconnaissance primitive. This test pins the fix: the
# host is checked, and rejected, BEFORE any real connection attempt.

@pytest.mark.asyncio
async def test_postgres_connector_blocks_loopback_host_before_connecting(monkeypatch):
    from app.agents.dsr.connectors import postgres

    attempted_connect = {"called": False}

    async def _fake_connect(**kwargs):
        attempted_connect["called"] = True
        raise AssertionError("asyncpg.connect was called -- the SSRF guard did not run first")

    monkeypatch.setattr(postgres.asyncpg, "connect", _fake_connect)

    config = PostgresDsrConfig(
        host="127.0.0.1", port=5432, dbname="app",
        read_user="dsr_read", read_password="r-secret",
    )
    connector = PostgresDsrConnector(config=config, grant=_grant())

    with pytest.raises(ConnectorUnavailableError):
        await connector._connect(write=False)

    assert not attempted_connect["called"], (
        "the connector dialled a loopback address instead of refusing it before any "
        "connection attempt"
    )


@pytest.mark.asyncio
async def test_postgres_connector_blocks_cloud_metadata_host_before_connecting(monkeypatch):
    """169.254.169.254 is link-local, which is the same range that covers the AWS/GCP/
    Azure metadata endpoint -- the single highest-value SSRF target there is."""
    from app.agents.dsr.connectors import postgres

    attempted_connect = {"called": False}

    async def _fake_connect(**kwargs):
        attempted_connect["called"] = True
        raise AssertionError("asyncpg.connect was called -- the SSRF guard did not run first")

    monkeypatch.setattr(postgres.asyncpg, "connect", _fake_connect)

    config = PostgresDsrConfig(
        host="169.254.169.254", port=5432, dbname="app",
        read_user="dsr_read", read_password="r-secret",
    )
    connector = PostgresDsrConnector(config=config, grant=_grant())

    with pytest.raises(ConnectorUnavailableError):
        await connector._connect(write=False)

    assert not attempted_connect["called"]


@pytest.mark.asyncio
async def test_postgres_connector_still_connects_to_a_safe_host(monkeypatch):
    """The guard must not become a denial-of-service against legitimate sources: a
    host that resolves to a normal, non-internal address proceeds to the real
    connection exactly as before."""
    from app.agents.dsr.connectors import postgres

    async def _fake_assert_safe_host(hostname):
        assert hostname == "db.customer-prod.example.net"
        return ["203.0.113.10"]  # TEST-NET-3, deliberately not private/loopback

    connect_calls = []

    class _FakeConn:
        async def execute(self, sql, *args):
            connect_calls.append(("execute", sql))
            return "SET"

    async def _fake_connect(**kwargs):
        connect_calls.append(("connect", kwargs))
        return _FakeConn()

    monkeypatch.setattr(postgres, "assert_safe_host", _fake_assert_safe_host)
    monkeypatch.setattr(postgres.asyncpg, "connect", _fake_connect)

    config = PostgresDsrConfig(
        host="db.customer-prod.example.net", port=5432, dbname="app",
        read_user="dsr_read", read_password="r-secret",
        connect_timeout_seconds=7.5, statement_timeout_seconds=20.0,
    )
    connector = PostgresDsrConnector(config=config, grant=_grant())

    conn = await connector._connect(write=False)

    assert conn is not None
    kinds = [c[0] for c in connect_calls]
    assert kinds == ["connect", "execute"], "connection then timeout statement, in order"


# ── Timeouts actually reach the real connection, not just the config dataclass ──

@pytest.mark.asyncio
async def test_connect_timeout_is_passed_to_asyncpg(monkeypatch):
    from app.agents.dsr.connectors import postgres

    monkeypatch.setattr(postgres, "assert_safe_host", _noop_assert_safe_host)
    captured = {}

    class _FakeConn:
        async def execute(self, sql, *args):
            captured["statement_timeout_sql"] = sql
            return "SET"

    async def _fake_connect(**kwargs):
        captured["connect_kwargs"] = kwargs
        return _FakeConn()

    monkeypatch.setattr(postgres.asyncpg, "connect", _fake_connect)

    config = PostgresDsrConfig(
        host="db.example.net", port=5432, dbname="app",
        read_user="dsr_read", read_password="r-secret",
        connect_timeout_seconds=3.0, statement_timeout_seconds=9.0,
    )
    connector = PostgresDsrConnector(config=config, grant=_grant())
    await connector._connect(write=False)

    assert captured["connect_kwargs"]["timeout"] == 3.0
    assert "9000" in captured["statement_timeout_sql"], (
        "statement_timeout_seconds=9.0 should become a 9000ms SET statement_timeout"
    )


async def _noop_assert_safe_host(hostname):
    return ["203.0.113.10"]


# ── Credential resolution: DSR's READ path must support the ciphertext Agent 2 ──
# ── (ROPA) already supports on the same `ropa_data_sources` row ─────────────────
#
# Confirmed gap: `ropa_data_sources.credential_ciphertext` (migration 0031, set via
# POST /sources/{id}/credential) is resolved by Agent 2's own factory
# (app/agents/ropa/connectors/factory.py `_resolve_secret`) but DSR's factory had its
# OWN, separate `_resolve_secret` that only ever looked at `credential_ref` -- never
# `credential_ciphertext`. A source an administrator set up with only a stored
# ciphertext (no credential_ref env var -- the whole point of 0031 was to let a
# deployment avoid env-var-per-source) would discover fine under Agent 2 and then
# fail DSR's `build_connector` with a misleading "no credential_ref" error, for a
# credential that in fact exists and resolves. Fixed by having DSR's `_resolve_secret`
# try `credential_ciphertext` first (via Agent 2's own `decrypt_credential`, so there
# is exactly one Fernet implementation, not two), falling back to `credential_ref`
# exactly as it did before. The WRITE credential is unaffected: DsrSourceAuthorization
# has no `write_credential_ciphertext` column, so it still resolves only by ref.

def _auth(**kw):
    return SimpleNamespace(
        enabled=True, searchable_tables=["customers"], identity_tables=["customers"],
        identifier_columns={"customers": {"email": "email"}},
        returnable_columns={"customers": ["email"]}, erasable_columns={},
        record_key_columns={}, allow_execution=kw.get("allow_execution", False),
        write_credential_ref=kw.get("write_credential_ref", None),
    )


def test_read_credential_resolves_from_stored_ciphertext_when_no_ref_is_set(monkeypatch):
    """Mirrors Agent 2's own precedence: a source configured only with a stored
    credential (no credential_ref env var) must still build a working DSR connector."""
    monkeypatch.setattr(factory, "decrypt_credential", lambda ciphertext: "the-real-password")

    data_source = SimpleNamespace(
        name="crm", connector="postgres",
        config={"host": "db.example.net", "dbname": "d", "user": "dsr_read"},
        credential_ref=None,
        credential_ciphertext="opaque-ciphertext-blob",
    )
    connector = factory.build_connector(data_source=data_source, authorization=_auth())

    assert connector._config.read_password == "the-real-password"


def test_read_credential_prefers_ciphertext_over_a_stale_ref(monkeypatch):
    """Matches Agent 2's own documented precedence exactly (ciphertext tried first):
    a source migrated to the encrypted path must not keep reading a stale env-var
    secret -- see test_ropa_credential_encryption.py's
    test_build_connector_prefers_ciphertext_over_credential_ref, which pins the
    identical contract for Agent 2's own factory using the same Fernet key."""
    from cryptography.fernet import Fernet

    from app.agents.ropa.connectors import factory as ropa_factory
    from app.config import get_settings

    monkeypatch.setenv("ROPA_CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("DSR_AUDIT_STALE_REF", "stale-env-password")
    get_settings.cache_clear()
    try:
        ciphertext = ropa_factory.encrypt_credential("current-password")

        data_source = SimpleNamespace(
            name="crm", connector="postgres",
            config={"host": "db.example.net", "dbname": "d", "user": "dsr_read"},
            credential_ref="DSR_AUDIT_STALE_REF",
            credential_ciphertext=ciphertext,
        )
        connector = factory.build_connector(data_source=data_source, authorization=_auth())
        assert connector._config.read_password == "current-password"
    finally:
        get_settings.cache_clear()


def test_a_source_with_neither_ref_nor_ciphertext_is_refused():
    data_source = SimpleNamespace(
        name="crm", connector="postgres",
        config={"host": "db.example.net", "dbname": "d", "user": "dsr_read"},
        credential_ref=None, credential_ciphertext=None,
    )
    with pytest.raises(SourceNotAuthorizedError, match="credential_ref or stored credential"):
        factory.build_connector(data_source=data_source, authorization=_auth())


def test_a_data_source_with_no_ciphertext_attribute_at_all_still_works():
    """Backward compatibility: a data_source object that predates the ciphertext
    column (or any stand-in without the attribute at all, e.g. in older tests)
    must resolve exactly as it did before this fix."""
    import os

    os.environ["DSR_AUDIT_LEGACY_REF"] = "legacy-env-password"
    try:
        data_source = SimpleNamespace(
            name="crm", connector="postgres",
            config={"host": "db.example.net", "dbname": "d", "user": "dsr_read"},
            credential_ref="DSR_AUDIT_LEGACY_REF",
            # no credential_ciphertext attribute at all
        )
        connector = factory.build_connector(data_source=data_source, authorization=_auth())
        assert connector._config.read_password == "legacy-env-password"
    finally:
        os.environ.pop("DSR_AUDIT_LEGACY_REF", None)


def test_a_ciphertext_that_fails_to_decrypt_is_refused_without_leaking_it(monkeypatch):
    from app.agents.ropa.connectors.base import ConnectorError as RopaConnectorError

    def _boom(ciphertext):
        raise RopaConnectorError("stored credential could not be decrypted")

    monkeypatch.setattr(factory, "decrypt_credential", _boom)

    data_source = SimpleNamespace(
        name="crm", connector="postgres",
        config={"host": "db.example.net", "dbname": "d", "user": "dsr_read"},
        credential_ref=None,
        credential_ciphertext="totally-not-a-real-fernet-token",
    )
    with pytest.raises(SourceNotAuthorizedError) as excinfo:
        factory.build_connector(data_source=data_source, authorization=_auth())

    message = str(excinfo.value)
    assert "totally-not-a-real-fernet-token" not in message


# ── Credential errors never leak the secret itself, only the ref NAME ───────────

def test_an_unresolvable_credential_ref_error_never_contains_a_secret_value():
    import os

    ref_name = "DSR_AUDIT_MISSING_REF_DOES_NOT_EXIST"
    os.environ.pop(ref_name, None)  # guarantee it is unset
    with pytest.raises(SourceNotAuthorizedError) as excinfo:
        factory._resolve_secret(ref_name, purpose="read")
    message = str(excinfo.value)
    assert ref_name in message, "the ref NAME is diagnostic and should survive"


def test_an_invalid_credential_ref_format_is_rejected_without_a_secret():
    with pytest.raises(SourceNotAuthorizedError, match="invalid read credential_ref format"):
        factory._resolve_secret("not a valid env var name!", purpose="read")
