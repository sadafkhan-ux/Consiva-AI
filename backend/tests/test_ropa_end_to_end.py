"""End-to-end Agent 2 test through the real API, against the real database.

Covers the full requested flow: external evidence -> integration boundary ->
ROPA agent -> discovery -> classification -> subject -> purpose -> activity ->
data flow -> risk -> ROPA record -> persistence -> human review -> audit.
"""

import hashlib
import hmac
import os
import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.agents.ropa.schemas.ropa import PersonalDataElement
from app.agents.ropa.services import enrichment_service
from app.agents.ropa.services.enrichment_service import ColumnSuggestion, EnrichmentResponse
from ropa_integration.ropa_adapter_sdk import AdapterConfig, build_payload
from tests.live_db import (
    READ_ONLY_SKIP_REASON,
    WRITABLE_SKIP_REASON,
    read_only_dsn,
    writable_dsn,
)

# writable_dsn, because these tests COMMIT: they create ROPA runs, approve them and
# leave the records behind. That makes DATABASE_URL the wrong thing to read -- it is
# routinely the deployment, and this suite would then be seeding it with test data. So
# it runs only against a database named deliberately in TEST_DATABASE_URL, and skips
# otherwise. See tests/live_db.py.
_REAL_DATABASE_URL = writable_dsn()

_live_db_only = pytest.mark.skipif(not _REAL_DATABASE_URL, reason=WRITABLE_SKIP_REASON)

# Two tests in this file assert that a request is REJECTED, so they create nothing: the
# inline-secret check is refused by a pydantic validator before the route body runs, and
# the auth check is refused before anything is authenticated at all (auth_headers just
# mints a JWT locally -- it writes nothing either). Gating those behind
# TEST_DATABASE_URL would mean two security assertions quietly stop running against the
# deployment, which is the opposite of what tightening this was for.
_read_only_db = pytest.mark.skipif(not read_only_dsn(), reason=READ_ONLY_SKIP_REASON)

# The `client` fixture is shared by both kinds, so it takes whichever DSN is available:
# the writable one when the committing tests are enabled, otherwise the read-only one
# for the two rejection tests. The committing tests skip before the fixture is built
# when writable_dsn() is None, so this can never hand them a database they must not
# write to.
_FIXTURE_DSN = _REAL_DATABASE_URL or read_only_dsn()


def _attendee_evidence(*, retention_policy: str = "24 months") -> dict:
    """PrepMyEvent-shaped metadata: what an external adapter would post.

    `retention_policy` is the one knob the versioning tests vary -- it flows
    straight into RopaRecord.retention, one of the fields the content
    fingerprint hashes, so changing it is what makes two pushes genuinely
    different rather than a no-op re-push.
    """
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
            "business_owner": "Events Team", "retention_policy": retention_policy,
        }],
    }


def _payload(idempotency_key: str | None = None, *, retention_policy: str = "24 months") -> dict:
    """Build the wire payload with the SDK an external adapter would use, so
    this test fails if the adapter and the server contract ever drift."""
    config = AdapterConfig(
        source_name="prepmyevent.com", consiva_base_url="https://api.example.com",
        integration_key="csv_unused_here", allow_list=(),
    )
    return build_payload(
        _attendee_evidence(retention_policy=retention_policy), config, idempotency_key=idempotency_key
    )


async def _run_evidence_analysis(auth_headers: dict, run_id: str, evidence_payload: dict) -> None:
    """Drive a queued evidence-push run to completion, the way the worker would.

    POST /evidence only enqueues now (ingest_pushed_evidence); the tests below
    call this afterward to actually run execute_queued_evidence_analysis --
    same pattern as the connector-discovery tests calling
    execute_queued_discovery directly. Requires the `worker_session_factory`
    fixture to be active in the calling test (not referenced here directly --
    it patches app.db.session.async_session_factory for the duration), since
    this opens its own session exactly as the real worker does.
    """
    from app.services import ropa_run_service

    token = auth_headers["Authorization"].split(" ", 1)[1]
    org_id = jwt.decode(
        token, os.environ["SUPABASE_JWT_SECRET"], algorithms=["HS256"], audience="authenticated",
    )["org_id"]
    await ropa_run_service.execute_queued_evidence_analysis(
        uuid.UUID(run_id), uuid.UUID(org_id), evidence_payload
    )


@pytest.fixture
async def client():
    """Real app, with get_db pointed at a real database (see _FIXTURE_DSN).
    Lifespan is not run (it would build a Postgres LangGraph checkpointer that
    these routes don't need) -- same reasoning as test_api_validation.py."""
    from app.db.session import get_db
    from app.main import app

    engine = create_async_engine(_FIXTURE_DSN, pool_size=2, max_overflow=0)
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
async def integration_headers_with_read(client, auth_headers):
    """A service key with BOTH evidence:write and evidence:read -- what an
    operator mints for an adapter that needs to push evidence AND read its own
    results back without a user session."""
    response = await client.post(
        "/api/v1/ropa/integration-keys",
        headers=auth_headers,
        json={
            "name": "prepmyevent-adapter-read-test",
            "scopes": ["evidence:write", "evidence:read"],
        },
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
        {
            "sub": str(uuid.uuid4()),
            "org_id": str(uuid.uuid4()),
            "aud": "authenticated",
            # Required since the verifiers stopped accepting tokens with no expiry --
            # a token without one was previously valid forever.
            "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
        },
        os.environ["SUPABASE_JWT_SECRET"],
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


@_live_db_only
async def test_full_flow_evidence_push_to_approved_ropa(
    client, auth_headers, integration_headers, worker_session_factory
):
    # 1. External integration posts evidence -- no production credentials shared.
    payload = _payload()
    response = await client.post(
        "/api/v1/ropa/evidence",
        headers=integration_headers,
        json=payload,
    )
    assert response.status_code == 201, response.text
    queued = response.json()
    assert queued["ingest_mode"] == "evidence_push"
    assert queued["status"] == "pending", (
        "evidence-push must queue the analysis and return before it runs; "
        "anything other than 'pending' here means it ran inline again"
    )
    run_id = queued["id"]

    # 1b. The worker's entry point is what actually runs analysis -- same
    # pattern as connector-based discovery, for the same reason (enrichment is
    # a real network call now, so this must not run inside the request).
    await _run_evidence_analysis(auth_headers, run_id, payload["evidence"])

    run = (await client.get(f"/api/v1/ropa/runs/{run_id}", headers=auth_headers)).json()
    assert run["status"] == "completed"
    assert run["tables_scanned"] == 2
    assert run["personal_data_elements"] >= 4  # name, email, phone, card_number

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

    # 3b. The data-flow chain (source -> storage -> Stripe, evidenced by the
    # pushed vendor record) reaches the record, not just the transient output.
    assert payload["data_flows"], "a vendor was supplied; the flow must reach the stored record"
    assert any(step["to_node"] == "Stripe" for step in payload["data_flows"])

    # 4. Risk findings were persisted, each with a machine-readable category,
    # its run id, and when it was created.
    findings = (await client.get(f"/api/v1/ropa/runs/{run_id}/findings", headers=auth_headers)).json()
    assert findings
    assert all(f["severity_factors"] for f in findings), "risk scoring must be explainable"
    assert all(f["category"] for f in findings)
    assert all(f["run_id"] == run_id for f in findings)
    assert all(f["created_at"] for f in findings)

    # 4b. The per-column classification trail is queryable independently of the
    # grouped record above -- "why was this column classified this way".
    classifications = (
        await client.get(f"/api/v1/ropa/runs/{run_id}/classifications", headers=auth_headers)
    ).json()
    assert classifications
    email_col = next(c for c in classifications if c["column"] == "email")
    assert email_col["classification"] == "Contact Data"
    assert email_col["confidence"] > 0
    assert email_col["evidence"]

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
async def test_evidence_push_queues_a_job_instead_of_analyzing_inline(
    client, integration_headers, monkeypatch
):
    """Regression test for the incident: POST /evidence must create the run
    and enqueue a 'ropa_evidence_analysis' job, returning immediately -- it
    must NOT run LLM enrichment (or any of the analysis pipeline) from the
    request path. A self-hosted model stalling or retrying for minutes used to
    hang this endpoint past every reasonable client timeout, because
    run_pipeline_enriched -- and its LLM call -- ran inline here. That analysis
    only happens in execute_queued_evidence_analysis, which the worker calls.
    """
    from app.agents.ropa.services import discovery_service

    def _must_not_be_called(*args, **kwargs):
        raise AssertionError(
            "run_pipeline_enriched was called from the request path -- evidence "
            "analysis is supposed to be queued, not run inline"
        )

    monkeypatch.setattr(discovery_service, "run_pipeline_enriched", _must_not_be_called)

    response = await client.post(
        "/api/v1/ropa/evidence", headers=integration_headers, json=_payload(),
    )
    assert response.status_code == 201, response.text
    run = response.json()
    assert run["status"] == "pending", (
        "evidence-push must return before the worker has touched this run; "
        "anything other than 'pending' here means analysis ran inline again"
    )


@_live_db_only
async def test_rerun_with_changed_evidence_creates_new_version(
    client, auth_headers, integration_headers, worker_session_factory
):
    """A second run whose evidence actually differs (a new retention policy)
    must create version N+1, never overwrite the approved record."""
    first_payload = _payload(retention_policy="24 months")
    second_payload = _payload(retention_policy="36 months")
    first = (
        await client.post("/api/v1/ropa/evidence", headers=integration_headers, json=first_payload)
    ).json()
    second = (
        await client.post("/api/v1/ropa/evidence", headers=integration_headers, json=second_payload)
    ).json()
    await _run_evidence_analysis(auth_headers, first["id"], first_payload["evidence"])
    await _run_evidence_analysis(auth_headers, second["id"], second_payload["evidence"])

    first_records = (await client.get(f"/api/v1/ropa/runs/{first['id']}/records", headers=auth_headers)).json()
    second_records = (await client.get(f"/api/v1/ropa/runs/{second['id']}/records", headers=auth_headers)).json()

    a1 = next(r for r in first_records if r["processing_activity"] == "Event Attendee Management")
    a2 = next(r for r in second_records if r["processing_activity"] == "Event Attendee Management")

    assert a2["version"] > a1["version"]
    assert a2["supersedes_id"] == a1["id"]
    assert a2["payload"]["retention"] == "36 months"


@_live_db_only
async def test_rerun_with_unchanged_evidence_does_not_create_a_new_version(
    client, auth_headers, integration_headers, worker_session_factory
):
    """Regression test: re-pushing evidence that produces the SAME record in
    substance must not mint a no-op version, even though the run itself gets a
    fresh discovery_run_id and fresh evidence local_ids every time.

    /runs/{id}/records filters by discovery_run_id, and that row is
    re-pointed at whichever run most recently re-confirmed it (persist_output,
    the `previous.content_hash == fingerprint` branch) -- so run 2 re-finding
    this activity unchanged DOES list it under run 2, not just run 1. A run
    that completes having re-confirmed N existing records must be able to
    show them; a run reporting 0 records is indistinguishable from a run that
    found nothing at all, which is the bug this used to let through (and
    which real evidence-push integrations hit: a completed run with
    personal_data_elements > 0 and risk findings, but an empty Records tab).

    The regression itself is checked the way the docstring originally meant:
    the record run 1 created is still the CURRENT, non-superseded version
    after run 2 -- same row, same version number -- i.e. run 2 recognizing it
    as unchanged did not fork it into a new version. It only relabels which
    run that single current version is attributed to.
    """
    payload = _payload()
    first = (await client.post("/api/v1/ropa/evidence", headers=integration_headers, json=payload)).json()
    await _run_evidence_analysis(auth_headers, first["id"], payload["evidence"])
    first_records = (await client.get(f"/api/v1/ropa/runs/{first['id']}/records", headers=auth_headers)).json()
    a1 = next(r for r in first_records if r["processing_activity"] == "Event Attendee Management")

    second = (await client.post("/api/v1/ropa/evidence", headers=integration_headers, json=payload)).json()
    assert first["id"] != second["id"], "two distinct runs, not deduped by idempotency_key"
    await _run_evidence_analysis(auth_headers, second["id"], payload["evidence"])

    second_records = (await client.get(f"/api/v1/ropa/runs/{second['id']}/records", headers=auth_headers)).json()
    a2 = next(r for r in second_records if r["processing_activity"] == "Event Attendee Management")
    assert a2["id"] == a1["id"], "run 2 must not have superseded run 1's record with a new one"
    assert a2["version"] == a1["version"]
    assert a2["status"] != "superseded"

    run = (await client.get(f"/api/v1/ropa/runs/{second['id']}", headers=auth_headers)).json()
    assert run["status"] == "completed", "the re-push is still a real, fully audited run"
    assert run["summary"]["unchanged_records"] >= 1

    # The record now belongs to run 2's view, not run 1's -- the whole point
    # of re-pointing discovery_run_id. Run 1's own audit trail and summary
    # counts are untouched; only the live "current records" listing moved.
    first_records_again = (
        await client.get(f"/api/v1/ropa/runs/{first['id']}/records", headers=auth_headers)
    ).json()
    assert not any(r["processing_activity"] == "Event Attendee Management" for r in first_records_again)


@_live_db_only
async def test_same_activity_name_across_sources_does_not_collide(
    client, auth_headers, integration_headers, worker_session_factory
):
    """Regression test for the version-identity bug: two different sources
    producing an activity with the SAME name must never share a version
    chain -- a run against source B must not supersede source A's record."""
    source_a = _payload()
    source_a["source_name"] = "source-a.example.com"
    source_b = _payload()
    source_b["source_name"] = "source-b.example.com"

    run_a = (await client.post("/api/v1/ropa/evidence", headers=integration_headers, json=source_a)).json()
    run_b = (await client.post("/api/v1/ropa/evidence", headers=integration_headers, json=source_b)).json()
    await _run_evidence_analysis(auth_headers, run_a["id"], source_a["evidence"])
    await _run_evidence_analysis(auth_headers, run_b["id"], source_b["evidence"])

    records_a = (await client.get(f"/api/v1/ropa/runs/{run_a['id']}/records", headers=auth_headers)).json()
    records_b = (await client.get(f"/api/v1/ropa/runs/{run_b['id']}/records", headers=auth_headers)).json()

    a = next(r for r in records_a if r["processing_activity"] == "Event Attendee Management")
    b = next(r for r in records_b if r["processing_activity"] == "Event Attendee Management")

    assert a["id"] != b["id"]
    assert a["version"] == 1
    assert b["version"] == 1, "source B's first run must be version 1, not a supersession of source A"
    assert a["source_name"] == "source-a.example.com"
    assert b["source_name"] == "source-b.example.com"

    # source A's record must still be the current (non-superseded) one for its
    # own source after source B's run.
    records_a_again = (
        await client.get(f"/api/v1/ropa/runs/{run_a['id']}/records", headers=auth_headers)
    ).json()
    a_again = next(r for r in records_a_again if r["processing_activity"] == "Event Attendee Management")
    assert a_again["status"] != "superseded"


@_live_db_only
async def test_idempotency_key_returns_same_run(client, auth_headers, integration_headers):
    key = f"test-{uuid.uuid4()}"
    body = _payload(idempotency_key=key)
    first = (await client.post("/api/v1/ropa/evidence", headers=integration_headers, json=body)).json()
    second = (await client.post("/api/v1/ropa/evidence", headers=integration_headers, json=body)).json()
    assert first["id"] == second["id"], "same idempotency key must not start a second run"


@_live_db_only
async def test_evidence_read_key_can_read_its_own_runs_and_promote_baseline(
    client, auth_headers, integration_headers_with_read, worker_session_factory
):
    """Regression test for the bug where an adapter's key got 201 from /evidence
    but 401 on every read: these routes must accept the SAME key (holding
    evidence:read) the push used, with no human session involved anywhere in
    this test (see integration_auth.get_ropa_reader)."""
    payload = _payload()
    push = await client.post(
        "/api/v1/ropa/evidence", headers=integration_headers_with_read, json=payload,
    )
    assert push.status_code == 201, push.text
    run_id = push.json()["id"]
    await _run_evidence_analysis(auth_headers, run_id, payload["evidence"])

    runs = await client.get("/api/v1/ropa/runs", headers=integration_headers_with_read)
    assert runs.status_code == 200, runs.text
    assert any(r["id"] == run_id for r in runs.json())

    assert (
        await client.get(f"/api/v1/ropa/runs/{run_id}", headers=integration_headers_with_read)
    ).status_code == 200

    assert (
        await client.get(
            f"/api/v1/ropa/runs/{run_id}/records", headers=integration_headers_with_read
        )
    ).status_code == 200

    assert (
        await client.get(
            f"/api/v1/ropa/runs/{run_id}/findings", headers=integration_headers_with_read
        )
    ).status_code == 200

    assert (
        await client.get(
            f"/api/v1/ropa/runs/{run_id}/changes", headers=integration_headers_with_read
        )
    ).status_code == 200

    promote = await client.post(
        f"/api/v1/ropa/runs/{run_id}/promote-baseline", headers=integration_headers_with_read,
    )
    assert promote.status_code == 200, promote.text
    assert promote.json()["is_current"] is True


@_live_db_only
async def test_evidence_read_key_cannot_read_another_orgs_run(client, integration_headers_with_read):
    """Acceptance criterion from the integration bug report: a key gets 403 or
    404 for another org's run -- evidence:read scopes a key to READ requests in
    general, never to a specific run; org isolation still has to hold per call.
    A second org's own evidence:read key must not be able to read the first
    org's run, even though both keys hold the identical scope."""
    push = await client.post(
        "/api/v1/ropa/evidence", headers=integration_headers_with_read, json=_payload(),
    )
    assert push.status_code == 201, push.text
    run_id = push.json()["id"]

    other_org_token = jwt.encode(
        {
            "sub": str(uuid.uuid4()), "org_id": str(uuid.uuid4()), "aud": "authenticated",
            "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
        },
        os.environ["SUPABASE_JWT_SECRET"], algorithm="HS256",
    )
    other_org_auth_headers = {"Authorization": f"Bearer {other_org_token}"}
    other_key_response = await client.post(
        "/api/v1/ropa/integration-keys", headers=other_org_auth_headers,
        json={"name": "other-org-adapter-test", "scopes": ["evidence:write", "evidence:read"]},
    )
    assert other_key_response.status_code == 201, other_key_response.text
    other_org_headers = {"Authorization": f"Bearer {other_key_response.json()['api_key']}"}

    response = await client.get(f"/api/v1/ropa/runs/{run_id}", headers=other_org_headers)
    assert response.status_code in (403, 404), response.text

    records = await client.get(f"/api/v1/ropa/runs/{run_id}/records", headers=other_org_headers)
    assert records.status_code in (403, 404), records.text


@_live_db_only
async def test_write_only_key_cannot_read_runs(client, integration_headers):
    """A key minted with only evidence:write (the default) must still be refused
    on the read endpoints: evidence:read is a separate grant, never implied by
    push access."""
    push = await client.post("/api/v1/ropa/evidence", headers=integration_headers, json=_payload())
    assert push.status_code == 201, push.text
    run_id = push.json()["id"]

    response = await client.get(f"/api/v1/ropa/runs/{run_id}", headers=integration_headers)
    assert response.status_code == 403


@_live_db_only
async def test_discover_source_queues_a_job_instead_of_connecting_inline(
    client, auth_headers, monkeypatch
):
    """Regression test: POST /sources/{id}/discover must create the run and
    enqueue a 'ropa_discovery' job, returning immediately -- it must NOT
    connect to the source itself from the request path. That connection only
    happens in execute_queued_discovery, which the worker calls."""
    from app.services import ropa_run_service

    def _must_not_be_called(**kwargs):
        raise AssertionError(
            "build_connector was called from the request path -- discovery is "
            "supposed to be queued, not run inline"
        )

    monkeypatch.setattr(ropa_run_service, "build_connector", _must_not_be_called)

    source = await client.post(
        "/api/v1/ropa/sources", headers=auth_headers,
        json={
            "name": "queued-discovery-test", "connector": "postgres", "source_type": "database",
            "config": {"host": "db.example.com", "port": 5432, "dbname": "x", "user": "ro"},
            "credential_ref": "TEST_UNUSED_CREDENTIAL_REF",
        },
    )
    assert source.status_code == 201, source.text
    source_id = source.json()["id"]

    response = await client.post(f"/api/v1/ropa/sources/{source_id}/discover", headers=auth_headers)
    assert response.status_code == 200, response.text
    run = response.json()
    assert run["status"] == "pending", (
        "discover_source must return before the worker has touched this run; "
        "anything other than 'pending' here means discovery ran inline again"
    )

    # A second request while the first is still queued must still be refused,
    # same protection the old synchronous path had -- "pending" is one of the
    # two non-terminal statuses find_active_run_for_source checks for.
    second = await client.post(f"/api/v1/ropa/sources/{source_id}/discover", headers=auth_headers)
    assert second.status_code == 409, second.text


@_live_db_only
async def test_disabling_a_source_blocks_discovery_until_re_enabled(client, auth_headers):
    """POST /sources/{id}/enabled must actually gate discovery -- a disabled
    source must refuse to queue a new run, and re-enabling it must restore
    that ability, rather than the flag being cosmetic."""
    source = await client.post(
        "/api/v1/ropa/sources", headers=auth_headers,
        json={
            "name": "enable-disable-test", "connector": "postgres", "source_type": "database",
            "config": {"host": "db.example.com", "port": 5432, "dbname": "x", "user": "ro"},
            "credential_ref": "TEST_UNUSED_CREDENTIAL_REF",
        },
    )
    assert source.status_code == 201, source.text
    source_id = source.json()["id"]
    assert source.json()["enabled"] is True

    disabled = await client.post(
        f"/api/v1/ropa/sources/{source_id}/enabled", headers=auth_headers, json={"enabled": False},
    )
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["enabled"] is False

    listed = await client.get("/api/v1/ropa/sources", headers=auth_headers)
    assert next(s for s in listed.json() if s["id"] == source_id)["enabled"] is False

    # run_discovery_for_source raises ValueError for a disabled source, same
    # as its not-found case -- the route maps that to 404, not 409 (409 is
    # reserved for DiscoveryAlreadyInProgressError, a different condition).
    refused = await client.post(f"/api/v1/ropa/sources/{source_id}/discover", headers=auth_headers)
    assert refused.status_code == 404, refused.text

    enabled = await client.post(
        f"/api/v1/ropa/sources/{source_id}/enabled", headers=auth_headers, json={"enabled": True},
    )
    assert enabled.status_code == 200, enabled.text
    assert enabled.json()["enabled"] is True

    allowed = await client.post(f"/api/v1/ropa/sources/{source_id}/discover", headers=auth_headers)
    assert allowed.status_code == 200, allowed.text


class _FakeConnector:
    """Satisfies connectors/base.py's SourceConnector protocol without any
    real network call, so these tests exercise execute_queued_discovery's
    real DB read/write path end to end (run status, persistence, audit) while
    controlling exactly how discovery succeeds or fails."""

    connector_name = "fake"
    source_type = "database"

    def __init__(self, *, evidence=None, error: Exception | None = None):
        self._evidence = evidence
        self._error = error

    async def discover(self, *, org_id: str, source_name: str):
        if self._error is not None:
            raise self._error
        return self._evidence

    async def test_connection(self) -> None:
        return None


@pytest.fixture
async def worker_session_factory(monkeypatch):
    """execute_queued_discovery is the WORKER's entry point: it opens its own
    session via `app.db.session.async_session_factory` (it has no request to
    inherit a `db` from), which is bound to the app's configured DATABASE_URL
    -- not whatever DSN the `client` fixture's dependency override points at.
    Calling it directly from a test (rather than through the HTTP layer, the
    way every other test in this file does) needs this patched to the same
    throwaway database, or it reads/writes a different one entirely."""
    engine = create_async_engine(_FIXTURE_DSN, pool_size=2, max_overflow=0)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("app.db.session.async_session_factory", session_factory)
    yield session_factory
    await engine.dispose()


@_live_db_only
async def test_connector_pull_discovery_fires_the_partner_webhook_on_completion(
    client, auth_headers, monkeypatch, worker_session_factory
):
    """The fixed external-partner channel (agent_webhook_service, distinct
    from the per-customer webhook_url) must see "ropa.run.completed" with this
    run's real id once execute_queued_discovery -- the worker's own entry
    point -- finishes successfully."""
    from unittest.mock import AsyncMock

    from app.agents.ropa.schemas.evidence import DiscoveryEvidence
    from app.services import ropa_run_service

    token = auth_headers["Authorization"].split(" ", 1)[1]
    org_id = jwt.decode(
        token, os.environ["SUPABASE_JWT_SECRET"], algorithms=["HS256"], audience="authenticated",
    )["org_id"]

    evidence = DiscoveryEvidence.model_validate(_attendee_evidence())
    monkeypatch.setattr(
        ropa_run_service, "build_connector", lambda **kwargs: _FakeConnector(evidence=evidence)
    )
    mock_send_event = AsyncMock()
    monkeypatch.setattr(ropa_run_service.agent_webhook_service, "send_event", mock_send_event)

    source = await client.post(
        "/api/v1/ropa/sources", headers=auth_headers,
        json={
            "name": "webhook-completion-test", "connector": "postgres", "source_type": "database",
            "config": {"host": "db.example.com", "port": 5432, "dbname": "x", "user": "ro"},
            "credential_ref": "TEST_UNUSED_CREDENTIAL_REF",
        },
    )
    assert source.status_code == 201, source.text
    source_id = source.json()["id"]

    queued = await client.post(f"/api/v1/ropa/sources/{source_id}/discover", headers=auth_headers)
    assert queued.status_code == 200, queued.text
    run_id = queued.json()["id"]

    await ropa_run_service.execute_queued_discovery(uuid.UUID(run_id), uuid.UUID(org_id))

    mock_send_event.assert_awaited_once_with(
        "ropa.run.completed", run_id=run_id, callback_url=None, callback_secret=None,
    )

    run = (await client.get(f"/api/v1/ropa/runs/{run_id}", headers=auth_headers)).json()
    assert run["status"] == "completed"


@_live_db_only
async def test_connector_pull_discovery_fires_the_partner_webhook_on_failure(
    client, auth_headers, monkeypatch, worker_session_factory
):
    """Same channel, the failure branch: a connector error during discovery
    must still reach the partner, with this run's id and the real error, and
    the run itself must land in status="failed" (the regression this whole
    file's postgres-connector test guards: a failure that never reaches
    fail_run leaves a run stuck at "discovering" forever)."""
    from unittest.mock import AsyncMock

    from app.agents.ropa.connectors.base import ConnectorError
    from app.services import ropa_run_service

    token = auth_headers["Authorization"].split(" ", 1)[1]
    org_id = jwt.decode(
        token, os.environ["SUPABASE_JWT_SECRET"], algorithms=["HS256"], audience="authenticated",
    )["org_id"]

    monkeypatch.setattr(
        ropa_run_service, "build_connector",
        lambda **kwargs: _FakeConnector(error=ConnectorError("synthetic connector failure")),
    )
    mock_send_event = AsyncMock()
    monkeypatch.setattr(ropa_run_service.agent_webhook_service, "send_event", mock_send_event)

    source = await client.post(
        "/api/v1/ropa/sources", headers=auth_headers,
        json={
            "name": "webhook-failure-test", "connector": "postgres", "source_type": "database",
            "config": {"host": "db.example.com", "port": 5432, "dbname": "x", "user": "ro"},
            "credential_ref": "TEST_UNUSED_CREDENTIAL_REF",
        },
    )
    assert source.status_code == 201, source.text
    source_id = source.json()["id"]

    queued = await client.post(f"/api/v1/ropa/sources/{source_id}/discover", headers=auth_headers)
    assert queued.status_code == 200, queued.text
    run_id = queued.json()["id"]

    with pytest.raises(ConnectorError):
        await ropa_run_service.execute_queued_discovery(uuid.UUID(run_id), uuid.UUID(org_id))

    mock_send_event.assert_awaited_once()
    call_args = mock_send_event.await_args
    assert call_args.args[0] == "ropa.run.failed"
    assert call_args.kwargs["run_id"] == run_id
    assert "synthetic connector failure" in call_args.kwargs["error"]

    run = (await client.get(f"/api/v1/ropa/runs/{run_id}", headers=auth_headers)).json()
    assert run["status"] == "failed"


@_live_db_only
async def test_evidence_push_fires_the_partner_webhook_exactly_once_for_an_idempotent_replay(
    client, auth_headers, integration_headers, monkeypatch, worker_session_factory
):
    """A retried POST /evidence with the same idempotency_key must not
    re-announce a run that already announced itself once -- the
    ingest_pushed_evidence `is_new` guard this test is really checking.

    The webhook now fires from execute_queued_evidence_analysis (the worker's
    entry point), not the route: POST /evidence only enqueues. `is_new` still
    governs whether a job is enqueued at all -- the replay below finds the
    already-committed run and enqueues nothing a second time -- so running
    analysis once for that one run is still the only way the webhook can fire
    more than once, and this proves it doesn't.
    """
    from unittest.mock import AsyncMock

    from app.services import ropa_run_service

    mock_send_event = AsyncMock()
    monkeypatch.setattr(ropa_run_service.agent_webhook_service, "send_event", mock_send_event)

    payload = _payload(idempotency_key="replay-webhook-test")
    first = await client.post("/api/v1/ropa/evidence", headers=integration_headers, json=payload)
    second = await client.post("/api/v1/ropa/evidence", headers=integration_headers, json=payload)
    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert first.json()["id"] == second.json()["id"], "same idempotency_key must return the same run"

    await _run_evidence_analysis(auth_headers, first.json()["id"], payload["evidence"])

    mock_send_event.assert_awaited_once_with(
        "ropa.run.completed", run_id=first.json()["id"], callback_url=None, callback_secret=None,
    )


@_live_db_only
async def test_evidence_analysis_failure_fails_the_run_and_notifies_the_partner(
    client, auth_headers, integration_headers, monkeypatch, worker_session_factory
):
    """Same regression execute_queued_discovery's failure branch guards
    against, for the evidence-push path: a failure during analysis (anything
    run_pipeline_enriched itself didn't already swallow -- enrich_elements
    catches its own LLM errors) must still reach the partner, with this run's
    id and the real error, and the run itself must land in status="failed" --
    not get stuck at "analyzing" forever."""
    from unittest.mock import AsyncMock

    from app.agents.ropa.services import discovery_service
    from app.services import ropa_run_service

    def _boom(*args, **kwargs):
        raise ValueError("synthetic analysis failure")

    monkeypatch.setattr(discovery_service, "run_pipeline_enriched", _boom)
    mock_send_event = AsyncMock()
    monkeypatch.setattr(ropa_run_service.agent_webhook_service, "send_event", mock_send_event)

    payload = _payload()
    push = await client.post("/api/v1/ropa/evidence", headers=integration_headers, json=payload)
    assert push.status_code == 201, push.text
    run_id = push.json()["id"]

    with pytest.raises(ValueError):
        await _run_evidence_analysis(auth_headers, run_id, payload["evidence"])

    mock_send_event.assert_awaited_once()
    call_args = mock_send_event.await_args
    assert call_args.args[0] == "ropa.run.failed"
    assert call_args.kwargs["run_id"] == run_id
    assert "synthetic analysis failure" in call_args.kwargs["error"]

    run = (await client.get(f"/api/v1/ropa/runs/{run_id}", headers=auth_headers)).json()
    assert run["status"] == "failed"


@_live_db_only
async def test_discover_source_accepts_a_per_run_callback_override(
    client, auth_headers, monkeypatch, worker_session_factory
):
    """Genuine end-to-end proof of the shared-agent-instance fix: a caller
    that knows which environment started this run can override the fixed
    AGENT_WEBHOOK_URL/AGENT_WEBHOOK_SECRET for this run's own notification --
    encrypted at rest (migration 0032), decrypted and used instead of the
    fixed config at delivery, never the other way round."""
    import os
    from unittest.mock import AsyncMock

    from cryptography.fernet import Fernet

    from app.agents.ropa.schemas.evidence import DiscoveryEvidence
    from app.config import get_settings
    from app.services import ropa_run_service

    old_key = os.environ.get("ROPA_CREDENTIAL_ENCRYPTION_KEY")
    os.environ["ROPA_CREDENTIAL_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
    get_settings.cache_clear()
    try:
        token = auth_headers["Authorization"].split(" ", 1)[1]
        org_id = jwt.decode(
            token, os.environ["SUPABASE_JWT_SECRET"], algorithms=["HS256"], audience="authenticated",
        )["org_id"]

        evidence = DiscoveryEvidence.model_validate(_attendee_evidence())
        monkeypatch.setattr(
            ropa_run_service, "build_connector", lambda **kwargs: _FakeConnector(evidence=evidence)
        )
        # The FIXED config, deliberately different from the override below --
        # if delivery ever used this instead, the assertions on the captured
        # request would fail.
        monkeypatch.setattr(
            ropa_run_service.agent_webhook_service, "get_settings",
            lambda: type("S", (), {
                "agent_webhook_url": "https://fixed.example/webhook",
                "agent_webhook_secret": "fixed-secret",
            })(),
        )
        monkeypatch.setattr(
            ropa_run_service.agent_webhook_service, "assert_safe_url", AsyncMock(return_value=[])
        )
        captured = {}

        # Patching httpx.AsyncClient.post globally also intercepts the test
        # `client` fixture's own calls into the FastAPI app (it's an
        # httpx.AsyncClient too, over ASGITransport) -- so this only captures
        # calls aimed at the fake webhook destinations below and otherwise
        # delegates to the real implementation.
        import httpx as httpx_module

        _real_post = httpx_module.AsyncClient.post
        _webhook_targets = {"https://override.example/webhook", "https://fixed.example/webhook"}

        async def _fake_post(self, url, *args, **kwargs):
            if str(url) in _webhook_targets:
                captured["url"] = str(url)
                captured["headers"] = kwargs.get("headers", {})
                return type("R", (), {"status_code": 200})()
            return await _real_post(self, url, *args, **kwargs)

        monkeypatch.setattr("httpx.AsyncClient.post", _fake_post)

        source = await client.post(
            "/api/v1/ropa/sources", headers=auth_headers,
            json={
                "name": "webhook-override-test", "connector": "postgres", "source_type": "database",
                "config": {"host": "db.example.com", "port": 5432, "dbname": "x", "user": "ro"},
                "credential_ref": "TEST_UNUSED_CREDENTIAL_REF",
            },
        )
        assert source.status_code == 201, source.text
        source_id = source.json()["id"]

        queued = await client.post(
            f"/api/v1/ropa/sources/{source_id}/discover", headers=auth_headers,
            json={
                "callback_url": "https://override.example/webhook",
                "callback_secret": "override-secret",
            },
        )
        assert queued.status_code == 200, queued.text
        run_id = queued.json()["id"]

        await ropa_run_service.execute_queued_discovery(uuid.UUID(run_id), uuid.UUID(org_id))

        assert captured["url"] == "https://override.example/webhook"
        # A signature computed with the FIXED secret must not match what was
        # actually sent -- same assurance test_agent_webhook_service.py's unit
        # test gives, now proven through a real DB round trip (store
        # ciphertext -> decrypt -> sign -> post).
        wrong_mac = hmac.new(
            b"fixed-secret", f"{captured['headers']['X-Agent-Timestamp']}.".encode(), hashlib.sha256
        ).hexdigest()
        assert captured["headers"]["X-Agent-Signature"] != f"sha256={wrong_mac}"
        assert captured["headers"]["X-Agent-Signature"].startswith("sha256=")
    finally:
        if old_key is None:
            os.environ.pop("ROPA_CREDENTIAL_ENCRYPTION_KEY", None)
        else:
            os.environ["ROPA_CREDENTIAL_ENCRYPTION_KEY"] = old_key
        get_settings.cache_clear()


@_live_db_only
async def test_discover_source_rejects_a_callback_url_without_a_secret(client, auth_headers):
    source = await client.post(
        "/api/v1/ropa/sources", headers=auth_headers,
        json={
            "name": "webhook-partial-override-test", "connector": "postgres", "source_type": "database",
            "config": {"host": "db.example.com", "port": 5432, "dbname": "x", "user": "ro"},
            "credential_ref": "TEST_UNUSED_CREDENTIAL_REF",
        },
    )
    assert source.status_code == 201, source.text
    source_id = source.json()["id"]

    response = await client.post(
        f"/api/v1/ropa/sources/{source_id}/discover", headers=auth_headers,
        json={"callback_url": "https://override.example/webhook"},
    )
    assert response.status_code == 422, response.text
    assert "together" in response.text


@_live_db_only
async def test_discover_source_rejects_a_callback_secret_when_encryption_is_not_configured(
    client, auth_headers,
):
    import os

    from app.config import get_settings

    old_key = os.environ.pop("ROPA_CREDENTIAL_ENCRYPTION_KEY", None)
    get_settings.cache_clear()
    try:
        source = await client.post(
            "/api/v1/ropa/sources", headers=auth_headers,
            json={
                "name": "webhook-no-encryption-key-test", "connector": "postgres",
                "source_type": "database",
                "config": {"host": "db.example.com", "port": 5432, "dbname": "x", "user": "ro"},
                "credential_ref": "TEST_UNUSED_CREDENTIAL_REF",
            },
        )
        assert source.status_code == 201, source.text
        source_id = source.json()["id"]

        response = await client.post(
            f"/api/v1/ropa/sources/{source_id}/discover", headers=auth_headers,
            # A real, resolvable domain -- the point of this test is reaching the
            # encryption step, not the earlier SSRF/DNS check.
            json={"callback_url": "https://example.com/webhook", "callback_secret": "s"},
        )
        assert response.status_code == 422, response.text
        assert "ROPA_CREDENTIAL_ENCRYPTION_KEY" in response.text
    finally:
        if old_key is not None:
            os.environ["ROPA_CREDENTIAL_ENCRYPTION_KEY"] = old_key
        get_settings.cache_clear()


@_live_db_only
async def test_source_credential_is_encrypted_and_never_returned(client, auth_headers):
    """POST /sources/{id}/credential (migration 0031): the secret must never
    come back from any read, and the row's stored ciphertext must decrypt
    back to exactly what was set when a connector is built from it."""
    import os
    import uuid as uuid_module

    from cryptography.fernet import Fernet

    from app.agents.ropa.connectors import factory as connector_factory
    from app.config import get_settings
    from app.db.repositories import ropa_repository

    old_key = os.environ.get("ROPA_CREDENTIAL_ENCRYPTION_KEY")
    os.environ["ROPA_CREDENTIAL_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
    get_settings.cache_clear()
    try:
        source = await client.post(
            "/api/v1/ropa/sources", headers=auth_headers,
            json={
                "name": "self-service-credential-test", "connector": "postgres",
                "source_type": "database",
                "config": {"host": "db.example.com", "port": 5432, "dbname": "x", "user": "ro"},
            },
        )
        assert source.status_code == 201, source.text
        source_id = source.json()["id"]
        assert source.json()["has_stored_credential"] is False

        set_response = await client.post(
            f"/api/v1/ropa/sources/{source_id}/credential",
            headers=auth_headers, json={"secret": "a-real-db-password"},
        )
        assert set_response.status_code == 204, set_response.text

        # Every read of this source must show a credential is configured, and
        # must never leak the secret or its ciphertext in any field.
        listed = await client.get("/api/v1/ropa/sources", headers=auth_headers)
        row = next(s for s in listed.json() if s["id"] == source_id)
        assert row["has_stored_credential"] is True
        assert "a-real-db-password" not in listed.text
        assert "credential_ciphertext" not in listed.text

        # The stored ciphertext must round-trip to exactly the secret that was
        # set, and a connector built from the real row must see the plaintext.
        token = auth_headers["Authorization"].split(" ", 1)[1]
        org_id = jwt.decode(
            token, os.environ["SUPABASE_JWT_SECRET"], algorithms=["HS256"], audience="authenticated",
        )["org_id"]

        engine = create_async_engine(_FIXTURE_DSN, pool_size=1, max_overflow=0)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as db:
                data_source = await ropa_repository.get_data_source(
                    db, uuid_module.UUID(source_id), uuid_module.UUID(org_id)
                )
        finally:
            await engine.dispose()

        assert data_source is not None
        assert data_source.credential_ciphertext is not None
        assert "a-real-db-password" not in data_source.credential_ciphertext

        connector = connector_factory.build_connector(
            connector=data_source.connector, config=data_source.config,
            credential_ref=data_source.credential_ref,
            credential_ciphertext=data_source.credential_ciphertext,
        )
        assert connector._config.password == "a-real-db-password"
    finally:
        if old_key is None:
            os.environ.pop("ROPA_CREDENTIAL_ENCRYPTION_KEY", None)
        else:
            os.environ["ROPA_CREDENTIAL_ENCRYPTION_KEY"] = old_key
        get_settings.cache_clear()


@_read_only_db
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


@_read_only_db
async def test_routes_require_authentication(client):
    assert (await client.get("/api/v1/ropa/sources")).status_code in (401, 403)
    assert (await client.get("/api/v1/ropa/connectors")).status_code in (401, 403)
    assert (await client.post("/api/v1/ropa/evidence", json={})).status_code in (401, 403)


@_read_only_db
async def test_connection_test_against_an_unreachable_host_fails_cleanly(client, auth_headers):
    """This route never touches the database (no Depends(get_db) at all), so
    unlike every other test in this file it needs no live DB -- it genuinely
    executes a real connection attempt against a host that cannot resolve,
    and the real PostgresConnectorError it raises must come back as
    ok: false, not an unhandled 500. This is also the regression test for the
    PostgresConnectorError/ConnectorError hierarchy bug: before that fix, this
    exact failure was NOT a ConnectorError, so the route's
    `except ConnectorError` would have missed it and this call would 500."""
    response = await client.post(
        "/api/v1/ropa/sources/test-connection",
        headers=auth_headers,
        json={
            "connector": "postgres",
            "config": {"host": "this-host-does-not-resolve.invalid", "dbname": "x", "user": "ro"},
            "secret": "whatever",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is False
    assert body["message"]


@_read_only_db
async def test_connection_test_rejects_an_unregistered_connector(client, auth_headers):
    response = await client.post(
        "/api/v1/ropa/sources/test-connection",
        headers=auth_headers,
        json={"connector": "mysql", "config": {}},
    )
    assert response.status_code == 422, response.text
    assert "unknown connector" in response.text


@_read_only_db
async def test_connection_test_rejects_an_inline_secret_in_config(client, auth_headers):
    """The `secret` field exists exactly so a raw credential never has to go
    into `config` -- the same inline-secret guard DataSourceCreate uses."""
    response = await client.post(
        "/api/v1/ropa/sources/test-connection",
        headers=auth_headers,
        json={
            "connector": "postgres",
            "config": {"host": "h", "dbname": "d", "user": "u", "password": "hunter2"},
        },
    )
    assert response.status_code == 422, response.text
    assert "credential_ref" in response.text


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
