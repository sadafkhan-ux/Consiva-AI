"""Phase 4/5/6: field declarations, the versioned wire contract, and correlation IDs."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.agents.ropa.schemas.payload import CURRENT_SCHEMA_VERSION, SourcePayload
from ropa_integration.ropa_adapter_sdk import (
    AdapterConfig,
    FieldDeclaration,
    TableAllowList,
    build_payload,
    new_correlation_id,
)


def _evidence() -> dict:
    return {
        "org_id": "external",
        "discovery_run_id": "run-1",
        "sources": [{"local_id": "source-1", "name": "prepmyevent.com", "source_type": "database"}],
        "tables": [{"local_id": "table-1", "source_local_id": "source-1", "table_name": "attendees"}],
        "columns": [{
            "local_id": "column-1", "table_local_id": "table-1",
            "column_name": "email", "data_type": "text",
        }],
    }


def _config() -> AdapterConfig:
    return AdapterConfig(
        source_name="prepmyevent.com", consiva_base_url="https://api.example.com",
        integration_key="csv_a_b", allow_list=(),
    )


# ── Phase 5: versioned contract ─────────────────────────────────────────────────


def test_payload_carries_version_and_correlation():
    payload = build_payload(_evidence(), _config())
    assert payload["schema_version"] == CURRENT_SCHEMA_VERSION
    assert payload["correlation_id"].startswith("pme-")
    assert payload["adapter_version"]
    assert payload["generated_at"]


def test_adapter_payload_validates_against_server_schema():
    """The SDK builds a plain dict; it must satisfy the server's Pydantic model.
    This is the test that catches the two sides drifting apart."""
    parsed = SourcePayload.model_validate(build_payload(_evidence(), _config()))
    assert parsed.source_name == "prepmyevent.com"
    assert parsed.evidence.tables[0].table_name == "attendees"


def test_correlation_id_is_unique_per_call():
    assert len({new_correlation_id() for _ in range(100)}) == 100


def test_explicit_correlation_id_is_preserved():
    payload = build_payload(_evidence(), _config(), correlation_id="my-trace-42")
    assert payload["correlation_id"] == "my-trace-42"


def test_unsupported_major_version_is_rejected():
    body = build_payload(_evidence(), _config())
    body["schema_version"] = "99.0"
    with pytest.raises(ValidationError, match="unsupported schema_version"):
        SourcePayload.model_validate(body)


def test_minor_version_difference_is_accepted():
    """Additive changes must not break an older sender."""
    body = build_payload(_evidence(), _config())
    body["schema_version"] = "1.7"
    assert SourcePayload.model_validate(body).schema_version == "1.7"


def test_absurd_future_timestamp_is_rejected():
    """A broken sender clock would corrupt change-detection ordering."""
    body = build_payload(_evidence(), _config())
    body["generated_at"] = (datetime.now(UTC) + timedelta(days=5)).isoformat()
    with pytest.raises(ValidationError, match="clock"):
        SourcePayload.model_validate(body)


def test_correlation_id_is_required():
    body = build_payload(_evidence(), _config())
    del body["correlation_id"]
    with pytest.raises(ValidationError):
        SourcePayload.model_validate(body)


# ── Phase 4: field declarations ─────────────────────────────────────────────────


def test_fields_act_as_column_allow_list():
    rule = TableAllowList(table="attendees", fields=(
        FieldDeclaration("email"), FieldDeclaration("full_name"),
    ))
    assert rule.approved_columns() == ("email", "full_name")


def test_columns_only_allow_list_still_works():
    rule = TableAllowList(table="attendees", columns=("id", "email"))
    assert rule.approved_columns() == ("id", "email")
    assert rule.declaration_for("email") is None


def test_no_restriction_means_all_columns():
    assert TableAllowList(table="attendees").approved_columns() is None


def test_declaration_lookup():
    decl = FieldDeclaration("email", is_personal_data=True, personal_data_category="Contact Data")
    rule = TableAllowList(table="attendees", fields=(decl,))
    assert rule.declaration_for("email") is decl
    assert rule.declaration_for("nope") is None


def test_undeclared_metadata_stays_none():
    """Anything not declared must remain unknown, never a default guess."""
    decl = FieldDeclaration("mystery_column")
    assert decl.personal_data_category is None
    assert decl.data_subject is None
    assert decl.purpose is None
    assert decl.retention is None
    assert decl.is_personal_data is None


def test_prepmyevent_ships_no_invented_declarations():
    """The handoff must not contain ROPA metadata nobody confirmed."""
    from ropa_integration.prepmyevent.adapter import DECLARED_TABLES

    assert DECLARED_TABLES == (), "declarations must come from PrepMyEvent, not be guessed here"


def test_declared_tables_override_plain_allow_list():
    from ropa_integration.prepmyevent import adapter

    declared = TableAllowList(table="attendees", fields=(FieldDeclaration("email"),))
    original = adapter.DECLARED_TABLES
    try:
        adapter.DECLARED_TABLES = (declared,)
        effective = adapter.effective_allow_list()
        attendees = [t for t in effective if t.table == "attendees"]
        assert len(attendees) == 1, "no duplicate entry for the same table"
        assert attendees[0].fields, "the declared version must win"
    finally:
        adapter.DECLARED_TABLES = original
