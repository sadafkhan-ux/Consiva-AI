"""Phase 12: configuration rather than hand-written SQL (blueprint §22, §24).

Three things that previously only existed if someone typed them into the database:
an organisation's retention rules, a source's DSR authorization, and the data
category shown against a discovered record.
"""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.agents.dsr.rules import constraints as rules
from app.agents.dsr.schemas import case
from app.main import app

client = TestClient(app)
NOW = datetime(2026, 9, 14, tzinfo=UTC)


def stored_rule(**kw):
    """A `dsr_retention_rules` row as the repository returns it."""
    return SimpleNamespace(
        table_name=kw.get("table_name", "orders"),
        date_column=kw.get("date_column", "created_at"),
        retention_days=kw.get("retention_days", 365 * 7),
        authority=kw.get("authority", "Finance policy FIN-3"),
        applies_to_operations=kw.get("applies_to_operations", ["delete_record"]),
    )


# ── Stored configuration becomes applied policy ──────────────────────────────────

def test_a_stored_rule_becomes_an_engine_rule():
    built = rules.rules_from_config([stored_rule()])
    assert len(built) == 1
    assert built[0].table_name == "orders"
    assert built[0].minimum_retention == timedelta(days=365 * 7)
    assert built[0].authority == "Finance policy FIN-3"


def test_a_configured_rule_actually_blocks_an_erasure():
    """The whole point: before this, the engine could apply a rule but nothing could
    supply one, so no real deployment could express a retention policy at all."""
    grant = SimpleNamespace(
        source_name="crm", allow_execution=True, write_credential_ref="W",
        erasable_columns={"orders": ["customer_email"]},
    )
    found = rules.evaluate(
        operation=case.OP_DELETE_RECORD, table_name="orders", grant=grant,
        record_snapshot={"created_at": "2025-01-01T00:00:00Z"},
        retention_rules=rules.rules_from_config([stored_rule()]),
        now=NOW,
    )
    blocker = next(c for c in found if c.effect == rules.EFFECT_BLOCK)
    assert blocker.kind == rules.KIND_POLICY
    assert "Finance policy FIN-3" in blocker.reason
    assert "Finance policy FIN-3" in blocker.requester_explanation


def test_an_unrecognised_operation_does_not_silently_void_the_rule():
    """A rule naming one valid and one invalid operation must still apply to the valid
    one -- dropping the whole rule would quietly weaken a retention guarantee."""
    built = rules.rules_from_config(
        [stored_rule(applies_to_operations=["delete_record", "teleport_record"])]
    )
    assert case.OP_DELETE_RECORD in built[0].applies_to_operations
    assert "teleport_record" not in built[0].applies_to_operations


def test_a_rule_with_no_usable_operation_still_guards_deletion():
    built = rules.rules_from_config([stored_rule(applies_to_operations=[])])
    assert built[0].applies_to_operations == frozenset({case.OP_DELETE_RECORD})


def test_rules_are_scoped_to_what_they_name():
    """A rule about orders must not block a deletion in customers."""
    grant = SimpleNamespace(
        source_name="crm", allow_execution=True, write_credential_ref="W",
        erasable_columns={"customers": ["name"]},
    )
    found = rules.evaluate(
        operation=case.OP_DELETE_RECORD, table_name="customers", grant=grant,
        record_snapshot={"created_at": "2025-01-01T00:00:00Z"},
        retention_rules=rules.rules_from_config([stored_rule(table_name="orders")]),
        now=NOW,
    )
    assert not [c for c in found if c.kind == rules.KIND_POLICY]


# ── The configuration API ────────────────────────────────────────────────────────

CONFIG_PATHS = ["/api/v1/dsr/config/sources", "/api/v1/dsr/config/retention"]


@pytest.mark.parametrize("path", CONFIG_PATHS)
def test_configuration_requires_a_token(path):
    for method in ("GET", "PUT"):
        assert client.request(method, path, json={}).status_code == 401


def test_a_retention_rule_must_name_its_authority():
    """A block with no attributable source is exactly the unattributable legal claim
    the engine refuses to make."""
    from app.api.v1.routes.dsr import RetentionRuleIn

    with pytest.raises(ValueError):
        RetentionRuleIn(table_name="orders", date_column="created_at",
                        retention_days=30, authority="")


def test_a_retention_rule_cannot_restrict_a_non_writing_operation():
    from app.api.v1.routes.dsr import RetentionRuleIn

    with pytest.raises(ValueError):
        RetentionRuleIn(table_name="orders", date_column="created_at", retention_days=30,
                        authority="Finance policy FIN-3", applies_to_operations=["disclose"])


def test_retention_days_must_be_positive_and_sane():
    from app.api.v1.routes.dsr import RetentionRuleIn

    for bad in (0, -1, 100 * 365 + 1):
        with pytest.raises(ValueError):
            RetentionRuleIn(table_name="orders", date_column="created_at",
                            retention_days=bad, authority="Finance policy FIN-3")


def test_a_write_credential_field_refuses_an_actual_secret():
    """It holds the NAME of a secret. Something that looks like a password is almost
    certainly someone pasting the password itself."""
    from app.api.v1.routes.dsr import SourceAuthorizationIn

    with pytest.raises(ValueError):
        SourceAuthorizationIn(data_source_id=uuid.uuid4(),
                              write_credential_ref="hunter2!@#$%^&*")
    ok = SourceAuthorizationIn(data_source_id=uuid.uuid4(),
                               write_credential_ref="DEMO_DB_WRITE_PASSWORD")
    assert ok.write_credential_ref == "DEMO_DB_WRITE_PASSWORD"


def test_an_authorization_defaults_to_permitting_nothing():
    from app.api.v1.routes.dsr import SourceAuthorizationIn

    a = SourceAuthorizationIn(data_source_id=uuid.uuid4())
    assert a.searchable_tables == []
    assert a.identity_tables == []
    assert a.allow_execution is False
    assert a.write_credential_ref is None


def test_no_configuration_response_can_return_a_credential():
    spec = app.openapi()
    for name in ("SourceAuthorizationIn", "RetentionRuleIn"):
        schema = spec["components"]["schemas"][name]
        for field in schema["properties"]:
            # `write_credential_ref` is the NAME of a secret and is allowed; anything
            # that reads like the value itself is not.
            assert field != "write_credential"
            assert not field.endswith("_password")
            assert field != "password"


# ── Evidence carries Agent 2's data category ─────────────────────────────────────

@pytest.mark.parametrize(
    "table,column,expected_personal",
    [
        ("customers", "email", True),
        ("customers", "phone", True),
        ("customers", "address", True),
        ("orders", "order_id", False),
        ("orders", "amount", False),
        ("support_tickets", "created_at", False),
    ],
)
def test_a_discovered_column_is_labelled_by_agent_2s_classifier(table, column, expected_personal):
    """§22: the reviewer sees "Contact Data", not just "email". Labelled from the
    column ACTUALLY found rather than from a stored ROPA record, which could describe
    a schema that has since changed."""
    from app.agents.dsr.services.search_service import _ropa_category

    label = _ropa_category(table, column)
    assert (label is not None) == expected_personal, f"{table}.{column} -> {label}"


def test_labelling_never_breaks_a_search():
    """A label is context for a reviewer, not a fact the case depends on. An
    unclassifiable column must produce no label and no exception."""
    from app.agents.dsr.services.search_service import _ropa_category

    for column in ("", "x" * 200, "weird$$column", "123"):
        assert _ropa_category("some_table", column) is None


def test_the_label_uses_the_same_rules_agent_2_uses():
    """Not a second copy of the taxonomy -- literally Agent 2's classifier, so the two
    agents can never disagree about what a column is."""
    from app.agents.dsr.services import search_service
    from app.agents.ropa.rules import personal_data_rules

    assert search_service.personal_data_rules is personal_data_rules
    assert search_service._ropa_category("customers", "email") == (
        personal_data_rules.classify_column("email", table_name="customers").category
    )
