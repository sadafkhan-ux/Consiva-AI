"""Phase 12: configuration rather than hand-written SQL (blueprint §22, §24).

Three things that previously only existed if someone typed them into the database:
an organisation's retention rules, a source's DSR authorization, and the data
category shown against a discovered record.
"""

import os
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import jwt
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


def _auth_headers(org_id: uuid.UUID, *, role: str | None = None) -> dict:
    """Same real-JWT approach as test_ropa_end_to_end.py's auth_headers, so this route
    is exercised through the production auth dependency rather than a stub.

    `role` is omitted by default (a legacy-shaped token, as before); pass
    role="admin" for a route gated by `_require_admin` (PUT /config/sources)."""
    claims = {
        "sub": str(uuid.uuid4()),
        "org_id": str(org_id),
        "aud": "authenticated",
        "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
    }
    if role is not None:
        claims["role"] = role
    token = jwt.encode(claims, os.environ["SUPABASE_JWT_SECRET"], algorithm="HS256")
    return {"Authorization": f"Bearer {token}"}


def test_sources_are_looked_up_once_not_once_per_authorization(monkeypatch):
    """GET /config/sources must resolve every authorization's source from ONE
    list_data_sources call, not one get_data_source call per row -- the N+1 this
    route used to have (blueprint item 17)."""
    from app.db.repositories import dsr_repository, ropa_repository

    org_id = uuid.uuid4()
    source_a = SimpleNamespace(id=uuid.uuid4(), name="crm")
    source_b = SimpleNamespace(id=uuid.uuid4(), name="billing")

    def auth_for(source, **kw):
        return SimpleNamespace(
            id=uuid.uuid4(), data_source_id=source.id, enabled=True,
            searchable_tables=["t"], identity_tables=["t"],
            identifier_columns={"t": {"email": "email"}},
            returnable_columns={"t": ["name"]}, record_key_columns={"t": ["id"]},
            erasable_columns={"t": []}, allow_execution=False,
            write_credential_ref=None, **kw,
        )

    auth_a, auth_b = auth_for(source_a), auth_for(source_b)

    async def list_source_authorizations(db, org):
        assert org == org_id
        return [auth_a, auth_b]

    list_data_sources_calls = []

    async def list_data_sources(db, org):
        list_data_sources_calls.append(org)
        return [source_a, source_b]

    async def get_data_source(db, source_id, org):
        raise AssertionError(
            "must not be called per-authorization; this route batches via list_data_sources"
        )

    monkeypatch.setattr(dsr_repository, "list_source_authorizations", list_source_authorizations)
    monkeypatch.setattr(ropa_repository, "list_data_sources", list_data_sources)
    monkeypatch.setattr(ropa_repository, "get_data_source", get_data_source)

    response = client.get("/api/v1/dsr/config/sources", headers=_auth_headers(org_id))

    assert response.status_code == 200, response.text
    body = response.json()
    assert {row["source_name"] for row in body} == {"crm", "billing"}
    assert len(list_data_sources_calls) == 1, (
        "one batched lookup for the whole route, not one per authorization"
    )


def test_get_source_authorizations_is_scoped_to_the_callers_org(monkeypatch):
    """Two organisations each have their own authorized source. A caller from org A
    must never see org B's row, even though both are served by the same route and
    the same in-memory fakes."""
    from app.db.repositories import dsr_repository, ropa_repository

    org_a, org_b = uuid.uuid4(), uuid.uuid4()
    source_a = SimpleNamespace(id=uuid.uuid4(), name="crm-a")
    source_b = SimpleNamespace(id=uuid.uuid4(), name="crm-b")

    def auth_for(source):
        return SimpleNamespace(
            id=uuid.uuid4(), data_source_id=source.id, enabled=True,
            searchable_tables=["t"], identity_tables=["t"],
            identifier_columns={"t": {"email": "email"}},
            returnable_columns={"t": ["name"]}, record_key_columns={"t": ["id"]},
            erasable_columns={"t": []}, allow_execution=False, write_credential_ref=None,
        )

    auth_a, auth_b = auth_for(source_a), auth_for(source_b)

    async def list_source_authorizations(db, org):
        return {org_a: [auth_a], org_b: [auth_b]}.get(org, [])

    async def list_data_sources(db, org):
        return {org_a: [source_a], org_b: [source_b]}.get(org, [])

    monkeypatch.setattr(dsr_repository, "list_source_authorizations", list_source_authorizations)
    monkeypatch.setattr(ropa_repository, "list_data_sources", list_data_sources)

    response_a = client.get("/api/v1/dsr/config/sources", headers=_auth_headers(org_a))
    response_b = client.get("/api/v1/dsr/config/sources", headers=_auth_headers(org_b))

    assert response_a.status_code == 200 and response_b.status_code == 200
    names_a = {row["source_name"] for row in response_a.json()}
    names_b = {row["source_name"] for row in response_b.json()}
    assert names_a == {"crm-a"}
    assert names_b == {"crm-b"}
    assert "crm-b" not in names_a
    assert "crm-a" not in names_b


def test_put_source_authorization_rejects_a_cross_org_data_source(monkeypatch):
    """A data_source_id that is real, but belongs to a DIFFERENT organisation, must be
    rejected exactly the way a nonexistent id is (404, non-disclosure) --
    ropa_repository.get_data_source's own WHERE org_id == org_id filter is what
    enforces this: a foreign id is simply invisible to this org's lookup."""
    from app.db.repositories import dsr_repository, ropa_repository

    org_id = uuid.uuid4()
    foreign_source_id = uuid.uuid4()

    calls = []

    async def get_data_source(db, source_id, org):
        calls.append((source_id, org))
        return None  # the real query would find nothing: this id is some other org's

    async def upsert_source_authorization(db, **kw):
        raise AssertionError(
            "must never persist an authorization for a source this org cannot see"
        )

    monkeypatch.setattr(ropa_repository, "get_data_source", get_data_source)
    monkeypatch.setattr(dsr_repository, "upsert_source_authorization", upsert_source_authorization)

    response = client.put(
        "/api/v1/dsr/config/sources",
        json={"data_source_id": str(foreign_source_id), "allow_execution": False},
        headers=_auth_headers(org_id, role="admin"),
    )

    assert response.status_code == 404, response.text
    assert calls == [(foreign_source_id, org_id)]


@pytest.mark.parametrize("unsupported_connector", ["mysql", "mongodb", "salesforce"])
def test_put_source_authorization_rejects_an_unsupported_connector(monkeypatch, unsupported_connector):
    """A ROPA source whose connector DSR has no implementation for must be refused
    AT CONFIGURATION TIME, with no row persisted -- not accepted here and only
    discovered to be useless the first time a search actually runs against it."""
    from app.db.repositories import dsr_repository, ropa_repository

    org_id = uuid.uuid4()
    source_id = uuid.uuid4()
    unsupported_source = SimpleNamespace(
        id=source_id, name="legacy-source", connector=unsupported_connector,
    )

    async def get_data_source(db, sid, org):
        return unsupported_source

    async def upsert_source_authorization(db, **kw):
        raise AssertionError(
            "an unsupported connector must be rejected before anything is persisted"
        )

    monkeypatch.setattr(ropa_repository, "get_data_source", get_data_source)
    monkeypatch.setattr(dsr_repository, "upsert_source_authorization", upsert_source_authorization)

    response = client.put(
        "/api/v1/dsr/config/sources",
        json={"data_source_id": str(source_id), "allow_execution": False},
        headers=_auth_headers(org_id, role="admin"),
    )

    assert response.status_code == 403, response.text
    assert unsupported_connector in response.json()["detail"]


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
