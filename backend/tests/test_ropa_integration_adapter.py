"""Integration-adapter SDK + integration-key auth tests.

The adapter half is exercised against a REAL SQLAlchemy engine and a real
database catalog (using this project's own tables to stand in for a customer's),
because the whole point of the SDK is that it reads a live Inspector correctly.
"""

import pathlib
import uuid

import pytest
from dotenv import dotenv_values

from app.core import integration_auth
from ropa_integration.ropa_adapter_sdk import (
    AdapterConfig,
    AdapterError,
    TableAllowList,
    collect_evidence,
    push_evidence,
)

_ENV_FILE = pathlib.Path(__file__).resolve().parents[1] / ".env"
_REAL_DATABASE_URL = dotenv_values(_ENV_FILE).get("DATABASE_URL")

_live_db_only = pytest.mark.skipif(
    not _REAL_DATABASE_URL, reason="DATABASE_URL not configured in backend/.env"
)


@pytest.fixture
def engine():
    from sqlalchemy import create_engine

    from ropa_integration.prepmyevent.adapter import _sync_url

    eng = create_engine(_sync_url(_REAL_DATABASE_URL), pool_size=1, max_overflow=0)
    try:
        yield eng
    finally:
        eng.dispose()


def _config(allow_list: tuple[TableAllowList, ...]) -> AdapterConfig:
    return AdapterConfig(
        source_name="prepmyevent.com",
        consiva_base_url="https://api.example.com",
        integration_key="csv_test_key",
        allow_list=allow_list,
    )


# ── Key generation / verification ───────────────────────────────────────────────


def test_generated_key_round_trips():
    full_key, prefix, key_hash = integration_auth.generate_key()
    assert full_key.startswith("csv_")
    assert integration_auth.parse_prefix(full_key) == prefix
    assert integration_auth.hash_key(full_key) == key_hash


def test_key_hash_does_not_contain_the_key():
    """A DB dump must not yield a usable credential."""
    full_key, prefix, key_hash = integration_auth.generate_key()
    secret = full_key.split("_", 2)[2]
    assert secret not in key_hash
    assert secret not in prefix
    assert len(key_hash) == 64  # sha256 hex


def test_keys_are_unique_and_high_entropy():
    keys = {integration_auth.generate_key()[0] for _ in range(50)}
    assert len(keys) == 50
    # 32 url-safe random bytes -> comfortably over 40 chars of secret
    assert all(len(k.split("_", 2)[2]) >= 40 for k in keys)


@pytest.mark.parametrize("bad", ["", "nope", "csv_only_two", "bearer_x_y", "csvx_a_b"])
def test_malformed_keys_are_rejected_by_the_parser(bad):
    assert integration_auth.parse_prefix(bad) is None


# ── Adapter: READ / VALIDATE / TRANSFORM ────────────────────────────────────────


@_live_db_only
def test_collect_evidence_reads_real_catalog(engine):
    """Uses this repo's own tables as a stand-in for a customer's schema."""
    evidence = collect_evidence(engine, _config((
        TableAllowList(table="websites"),
        TableAllowList(table="consent_scans"),
    )))

    assert {t["table_name"] for t in evidence["tables"]} == {"websites", "consent_scans"}
    assert evidence["columns"], "columns should be discovered"
    assert evidence["sources"][0]["name"] == "prepmyevent.com"


@_live_db_only
def test_allow_list_excludes_everything_not_approved(engine):
    """A table that exists but is NOT approved must not appear."""
    evidence = collect_evidence(engine, _config((TableAllowList(table="websites"),)))
    names = {t["table_name"] for t in evidence["tables"]}
    assert names == {"websites"}
    assert "cookies" not in names
    assert "audit_logs" not in names


@_live_db_only
def test_column_level_allow_list_is_enforced(engine):
    """Naming a column subset must exclude every other column."""
    evidence = collect_evidence(engine, _config((
        TableAllowList(table="websites", columns=("id", "domain")),
    )))
    discovered = {c["column_name"] for c in evidence["columns"]}
    assert discovered == {"id", "domain"}
    assert "org_id" not in discovered


@_live_db_only
def test_payload_contains_no_row_values(engine):
    """The single most important guarantee: metadata only, never data."""
    # A source name that cannot collide with any stored row value, so a hit
    # below is a genuine leak rather than the label we chose.
    config = AdapterConfig(
        source_name="unique-source-label-9z7q",
        consiva_base_url="https://api.example.com",
        integration_key="csv_test_key",
        allow_list=(TableAllowList(table="websites"), TableAllowList(table="cookies")),
    )
    evidence = collect_evidence(engine, config)
    assert all(c["sample_pattern"] is None for c in evidence["columns"])

    # No real value from the database may appear anywhere in the payload.
    from sqlalchemy import text

    with engine.connect() as conn:
        values = [r[0] for r in conn.execute(text("select domain from websites limit 5"))]
        values += [r[0] for r in conn.execute(text("select name from cookies limit 5"))]

    serialized = str(evidence)
    for value in values:
        assert value and value not in serialized, f"row value {value!r} leaked into the payload"


@_live_db_only
def test_relationships_only_reference_approved_tables(engine):
    """An FK pointing at a non-approved table must not disclose that table."""
    evidence = collect_evidence(engine, _config((TableAllowList(table="consent_scans"),)))
    approved_ids = {t["local_id"] for t in evidence["tables"]}
    for rel in evidence["relationships"]:
        assert rel["from_table_local_id"] in approved_ids
        assert rel["to_table_local_id"] in approved_ids


@_live_db_only
def test_missing_approved_table_is_skipped_not_fatal(engine):
    """A table in the allow-list that doesn't exist yet must not crash the run."""
    evidence = collect_evidence(engine, _config((
        TableAllowList(table="websites"),
        TableAllowList(table="table_that_does_not_exist"),
    )))
    assert {t["table_name"] for t in evidence["tables"]} == {"websites"}


@_live_db_only
def test_adapter_does_not_leak_internal_host(engine):
    evidence = collect_evidence(engine, _config((TableAllowList(table="websites"),)))
    assert evidence["sources"][0]["location"] is None


# ── Adapter: SEND ───────────────────────────────────────────────────────────────


def test_push_requires_https():
    config = AdapterConfig(
        source_name="x", consiva_base_url="http://insecure.example.com",
        integration_key="csv_a_b", allow_list=(),
    )
    with pytest.raises(AdapterError, match="https"):
        push_evidence({"org_id": "x"}, config)


def test_push_does_not_retry_on_client_error(monkeypatch):
    """A 401/400 means the request is wrong; retrying just repeats it."""
    import urllib.error

    attempts = {"n": 0}

    def _fail(*_args, **_kwargs):
        attempts["n"] += 1
        raise urllib.error.HTTPError("u", 401, "Unauthorized", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", _fail)
    config = AdapterConfig(
        source_name="x", consiva_base_url="https://api.example.com",
        integration_key="csv_a_b", allow_list=(), max_retries=3,
    )
    with pytest.raises(AdapterError, match="401"):
        push_evidence({"org_id": "x"}, config)
    assert attempts["n"] == 1, "a 401 must not be retried"


def test_push_retries_on_server_error(monkeypatch):
    import urllib.error

    attempts = {"n": 0}

    def _fail(*_args, **_kwargs):
        attempts["n"] += 1
        raise urllib.error.HTTPError("u", 503, "Unavailable", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", _fail)
    monkeypatch.setattr("time.sleep", lambda _s: None)
    config = AdapterConfig(
        source_name="x", consiva_base_url="https://api.example.com",
        integration_key="csv_a_b", allow_list=(), max_retries=3,
    )
    with pytest.raises(AdapterError, match="after 3 attempts"):
        push_evidence({"org_id": "x"}, config)
    assert attempts["n"] == 3


@_live_db_only
def test_run_adapter_never_raises_into_host_app(engine, monkeypatch):
    """If Consiva is down, PrepMyEvent must keep working."""
    from ropa_integration.ropa_adapter_sdk import run_adapter

    def _explode(*_args, **_kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", _explode)
    monkeypatch.setattr("time.sleep", lambda _s: None)

    result = run_adapter(engine, _config((TableAllowList(table="websites"),)))
    assert result is None, "failure must be reported as None, never raised"


def test_adapter_module_issues_no_write_sql():
    """Static guarantee: the SDK contains no write path at all."""
    sdk = (pathlib.Path(__file__).resolve().parents[1] / "ropa_integration" / "ropa_adapter_sdk.py").read_text(
        encoding="utf-8"
    )
    code_only = "\n".join(
        line for line in sdk.splitlines()
        if not line.strip().startswith(("#", "*", '"""', "'''"))
    ).lower()
    for forbidden in ("insert into", "update ", "delete from", "drop ", "alter ", "truncate "):
        assert forbidden not in code_only, f"SDK must contain no {forbidden!r} statement"


def test_prepmyevent_allow_list_covers_researched_tables():
    from ropa_integration.prepmyevent.adapter import ALLOW_LIST

    tables = {a.table for a in ALLOW_LIST}
    confirmed = {
        "leads", "events", "attendees", "campaigns", "emails",
        "email_events", "users", "connected_inboxes", "transactions", "audit_logs",
    }
    assert confirmed <= tables, f"missing confirmed tables: {confirmed - tables}"

    # Alternate names from the same research are also listed; a name absent from
    # their real schema is skipped at collection time, never fatal.
    alternates = {
        "event_attendees", "outreach_campaigns", "campaign_leads",
        "generated_emails", "pending_signups", "credit_transactions",
    }
    assert alternates <= tables


def test_allow_list_excludes_credentials_and_content():
    """Nothing in the allow-list may be a credential or message-body store."""
    from ropa_integration.prepmyevent.adapter import ALLOW_LIST

    tables = {a.table for a in ALLOW_LIST}
    for forbidden in ("oauth_tokens", "smtp_credentials", "encryption_keys",
                      "api_keys", "sessions", "message_bodies"):
        assert forbidden not in tables
