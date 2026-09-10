"""End-to-end Agent 2 test through the real API, against the real database.

Covers the full requested flow: external evidence -> integration boundary ->
ROPA agent -> discovery -> classification -> subject -> purpose -> activity ->
data flow -> risk -> ROPA record -> persistence -> human review -> audit.
"""

import os
import pathlib
import uuid

import jwt
import pytest
from dotenv import dotenv_values
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.agents.ropa.schemas.ropa import PersonalDataElement
from ropa_integration.ropa_adapter_sdk import AdapterConfig, build_payload
from app.agents.ropa.services import enrichment_service
from app.agents.ropa.services.enrichment_service import ColumnSuggestion, EnrichmentResponse

_ENV_FILE = pathlib.Path(__file__).resolve().parents[1] / ".env"
_REAL_DATABASE_URL = dotenv_values(_ENV_FILE).get("DATABASE_URL")

# conftest sets a dummy DATABASE_URL so imports validate; these tests need the
# REAL one from backend/.env, same approach as test_rag_retrieval_quality.py.
_live_db_only = pytest.mark.skipif(
    not _REAL_DATABASE_URL,
    reason="DATABASE_URL not configured in backend/.env; this test needs a real database",
)


def _attendee_evidence() -> dict:
    """PrepMyEvent-shaped metadata: what an external adapter would post."""
    return {
        "org_id": "00000000-0000-0000-0000-000000000000",
        "discovery_run_id": str(uuid.uuid4()),
        "sources": [{
            "local_id": "source-1", "name": "prepmyevent.com", "source_type": "database",
            "connector": "postgres", "location": "db.internal",
        }],
        "tables": [
            {"local_id": "table-1", "source_local_id": "source-1", "table_name": "attendees"},
            {"local_id": "table-2", "source_local_id": "source-1", "table_name": "payments"},
        ],
        "columns": [
            {"local_id": "column-1", "table_local_id": "table-1", "column_name": "id", "data_type": "uuid"},
            {"local_id": "column-2", "table_local_id": "table-1", "column_name": "full_name", "data_type": "text"},
            {"local_id": "column-3", "table_local_id": "table-1", "column_name": "email", "data_type": "text"},
            {"local_id": "column-4", "table_local_id": "table-1", "column_name": "phone", "data_type": "text"},
            {"local_id": "column-5", "table_local_id": "table-1", "column_name": "linkedin_url", "data_type": "text"},
            {"local_id": "column-6", "table_local_id": "table-2", "column_name": "card_number", "data_type": "text"},
        ],
        "vendors": [{
            "local_id": "vendor-1", "name": "Stripe", "role": "processor",
            "integration_local_id": "source-1", "location": "US", "dpa_status": "Confirmed",
        }],
        "business_metadata": [{
            "local_id": "meta-1", "subject_local_id": "table-1",
            "business_owner": "Events Team", "retention_policy": "24 months",
        }],
    }


def _payload(idempotency_key: str | None = None) -> dict:
    """Build the wire payload with the SDK an external adapter would use, so
    this test fails if the adapter and the server contract ever drift."""
    config = AdapterConfig(
        source_name="prepmyevent.com", consiva_base_url="https://api.example.com",
        integration_key="csv_unused_here", allow_list=(),
    )
    return build_payload(_attendee_evidence(), config, idempotency_key=idempotency_key)


@pytest.fixture
async def client():
    """Real app, with get_db pointed at the REAL database from backend/.env.
    Lifespan is not run (it would build a Postgres LangGraph checkpointer that
    these routes don't need) -- same reasoning as test_api_validation.py."""
    from app.db.session import get_db
    from app.main import app

    engine = create_async_engine(_REAL_DATABASE_URL, pool_size=2, max_overflow=0)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _override_get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = _override_get_db
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_db, None)
        await engine.dispose()


@pytest.fixture
async def integration_headers(client, auth_headers):
    """Mint a service key the way a real operator would, then use it the way an
    external adapter would. Proves the full credential lifecycle."""
    response = await client.post(
        "/api/v1/ropa/integration-keys",
        headers=auth_headers,
        json={"name": "prepmyevent-adapter-test"},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["api_key"].startswith("csv_")
    return {"Authorization": f"Bearer {body['api_key']}"}


@pytest.fixture
def auth_headers():
    """Same real-JWT approach Agent 1's own API tests use (test_api_validation.py),
    so these routes are exercised through the production auth dependency."""
    token = jwt.encode(
        {"sub": str(uuid.uuid4()), "org_id": str(uuid.uuid4()), "aud": "authenticated"},
        os.environ["SUPABASE_JWT_SECRET"],
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


@_live_db_only
async def test_full_flow_evidence_push_to_approved_ropa(client, auth_headers, integration_headers):
    # 1. External integration posts evidence -- no production credentials shared.
    response = await client.post(
        "/api/v1/ropa/evidence",
        headers=integration_headers,
        json=_payload(),
    )
    assert response.status_code == 201, response.text
    run = response.json()
    assert run["ingest_mode"] == "evidence_push"
    assert run["status"] == "completed"
    assert run["tables_scanned"] == 2
    assert run["personal_data_elements"] >= 4  # name, email, phone, card_number

    run_id = run["id"]

    # 2. ROPA records were persisted with versioning.
    records = (await client.get(f"/api/v1/ropa/runs/{run_id}/records", headers=auth_headers)).json()
    assert records
    attendee = next(r for r in records if r["processing_activity"] == "Event Attendee Management")
    payload = attendee["payload"]

    # 3. The ROPA contains every field the spec requires.
    assert payload["processing_activity"] == "Event Attendee Management"
    assert payload["data_subjects"] == ["Attendee"]
    assert "Contact Data" in payload["personal_data_categories"]
    assert payload["purpose"] == "Event Attendee Management"
    assert payload["source_systems"] == ["prepmyevent.com"]
    assert payload["retention"] == "24 months"
    assert payload["business_owner"] == "Events Team"
    assert payload["evidence"]
    assert payload["confidence"] > 0
    assert attendee["version"] >= 1
    assert attendee["status"] in ("draft", "in_review")

    # 4. Risk findings were persisted.
    findings = (await client.get(f"/api/v1/ropa/runs/{run_id}/findings", headers=auth_headers)).json()
    assert findings
    assert all(f["severity_factors"] for f in findings), "risk scoring must be explainable"

    # 5. Human review: approve the record.
    decision = await client.post(
        f"/api/v1/ropa/records/{attendee['id']}/decision",
        headers=auth_headers,
        json={"decision": "approved", "reason": "verified against source of truth"},
    )
    assert decision.status_code == 200, decision.text
    assert decision.json()["status"] == "approved"

    # 6. Reject a finding.
    finding_decision = await client.post(
        f"/api/v1/ropa/findings/{findings[0]['id']}/decision",
        headers=auth_headers,
        json={"decision": "rejected", "reason": "accepted risk"},
    )
    assert finding_decision.status_code == 200
    assert finding_decision.json()["review_status"] == "rejected"


@_live_db_only
async def test_rerun_supersedes_previous_version(client, auth_headers, integration_headers):
    """A second run must create version N+1, never overwrite the approved record."""
    payload = _payload()
    first = (await client.post("/api/v1/ropa/evidence", headers=integration_headers, json=payload)).json()
    second = (await client.post("/api/v1/ropa/evidence", headers=integration_headers, json=payload)).json()

    first_records = (await client.get(f"/api/v1/ropa/runs/{first['id']}/records", headers=auth_headers)).json()
    second_records = (await client.get(f"/api/v1/ropa/runs/{second['id']}/records", headers=auth_headers)).json()

    a1 = next(r for r in first_records if r["processing_activity"] == "Event Attendee Management")
    a2 = next(r for r in second_records if r["processing_activity"] == "Event Attendee Management")

    assert a2["version"] > a1["version"]
    assert a2["supersedes_id"] == a1["id"]


@_live_db_only
async def test_idempotency_key_returns_same_run(client, auth_headers, integration_headers):
    key = f"test-{uuid.uuid4()}"
    body = _payload(idempotency_key=key)
    first = (await client.post("/api/v1/ropa/evidence", headers=integration_headers, json=body)).json()
    second = (await client.post("/api/v1/ropa/evidence", headers=integration_headers, json=body)).json()
    assert first["id"] == second["id"], "same idempotency key must not start a second run"


@_live_db_only
async def test_source_config_rejects_inline_secrets(client, auth_headers):
    """A password must go in the environment, never into the stored config."""
    response = await client.post(
        "/api/v1/ropa/sources",
        headers=auth_headers,
        json={
            "name": "bad-source", "connector": "postgres", "source_type": "database",
            "config": {"host": "db.example.com", "dbname": "prod", "user": "ro", "password": "hunter2"},
        },
    )
    assert response.status_code == 422
    assert "credential_ref" in response.text


@_live_db_only
async def test_routes_require_authentication(client):
    assert (await client.get("/api/v1/ropa/sources")).status_code in (401, 403)
    assert (await client.get("/api/v1/ropa/connectors")).status_code in (401, 403)
    assert (await client.post("/api/v1/ropa/evidence", json={})).status_code in (401, 403)


# ── Enrichment safety (no live LLM needed) ──────────────────────────────────────


def _unknown(table: str, column: str) -> PersonalDataElement:
    return PersonalDataElement(
        source="s", table=table, column=column, classification="Unknown",
        confidence=0.0, evidence=[f"{table}.{column}"], review_required=True,
    )


def test_llm_cannot_invent_columns():
    elements = [_unknown("attendees", "job_role")]
    response = EnrichmentResponse(suggestions=[
        ColumnSuggestion(column="job_role", category="Employment Data", reasoning="a role"),
        ColumnSuggestion(column="totally_made_up", category="Contact Data", reasoning="invented"),
    ])
    result = enrichment_service.apply_suggestions(elements, response)
    assert len(result) == 1
    assert result[0].classification == "Employment Data"
    assert all(e.column != "totally_made_up" for e in result)


def test_llm_suggestions_always_require_review():
    elements = [_unknown("attendees", "job_role")]
    response = EnrichmentResponse(suggestions=[
        ColumnSuggestion(column="job_role", category="Employment Data", reasoning="a role"),
    ])
    result = enrichment_service.apply_suggestions(elements, response)
    assert result[0].review_required is True
    assert result[0].confidence == enrichment_service.LLM_SUGGESTION_CONFIDENCE


def test_llm_cannot_override_rule_classification():
    """A column the rules already settled is never sent to the LLM."""
    settled = PersonalDataElement(
        source="s", table="attendees", column="email", classification="Contact Data",
        confidence=0.98, evidence=["column-1"],
    )
    assert enrichment_service.ambiguous_elements([settled]) == []


def test_llm_invalid_category_is_dropped():
    elements = [_unknown("attendees", "job_role")]
    response = EnrichmentResponse(suggestions=[
        ColumnSuggestion(column="job_role", category="Totally Invalid", reasoning="x"),
    ])
    result = enrichment_service.apply_suggestions(elements, response)
    assert result[0].classification == "Unknown"
