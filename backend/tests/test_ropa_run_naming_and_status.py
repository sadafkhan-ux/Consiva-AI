"""ROPA run naming and run-status honesty.

Covers: a source name is never an empty-bracket label like "Consiva ()"; a run that has
not finished says why from the queue; a run whose job permanently failed is closed with
that job's real error; runs and their status detail never cross organisations.
"""

import os
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import jwt
import pytest
from pydantic import ValidationError

from app.agents.ropa.schemas.payload import SourcePayload, clean_source_name
from app.api.v1.routes.ropa import _run_response, _status_detail
from tests.test_ropa_end_to_end import (  # noqa: F401 -- fixtures
    _live_db_only,
    _payload,
    auth_headers,
    client,
    integration_headers,
    worker_session_factory,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Consiva ()", "Consiva"),
        ("Consiva (  )", "Consiva"),
        ("Consiva () ()", "Consiva"),
        ("Consiva []", "Consiva"),
        ("  prepmyevent.com  ", "prepmyevent.com"),
        ("prepmyevent  .com", "prepmyevent .com"),
        ("Consiva (prod)", "Consiva (prod)"),  # a real qualifier is kept
        ("Consiva (db) ()", "Consiva (db)"),
        ("()", None),
        ("   ", None),
        ("- ( ) -", None),
        (None, None),
    ],
)
def test_clean_source_name(raw, expected):
    assert clean_source_name(raw) == expected


def _body(source_name: str) -> dict:
    body = _payload()
    body["source_name"] = source_name
    return body


def test_wire_contract_normalises_empty_brackets():
    assert SourcePayload.model_validate(_body("Consiva ()")).source_name == "Consiva"


@pytest.mark.parametrize("bad", ["()", "   ", "[ ]"])
def test_wire_contract_rejects_names_with_nothing_in_them(bad):
    with pytest.raises(ValidationError):
        SourcePayload.model_validate(_body(bad))


def _run(status="completed", source_name="Consiva ()"):
    return SimpleNamespace(
        id=uuid.uuid4(), data_source_id=None, source_name=source_name, ingest_mode="evidence_push",
        status=status, tables_scanned=21, columns_scanned=100, personal_data_elements=48,
        overall_confidence=None, summary={}, error=None,
        created_at=datetime.now(UTC), started_at=None, completed_at=None,
    )


def _job(status, *, attempts=1, error=None, minutes_old=3):
    return SimpleNamespace(
        status=status, attempts=attempts, error=error,
        created_at=datetime.now(UTC) - timedelta(minutes=minutes_old),
    )


def test_existing_bad_rows_are_displayed_cleanly_without_being_rewritten():
    run = _run(source_name="Consiva ()")
    assert _run_response(run).source_name == "Consiva"
    assert run.source_name == "Consiva ()"  # persisted value untouched


def test_unnameable_row_says_unnamed_rather_than_a_blank_or_fake_name():
    assert _run_response(_run(source_name="()")).source_name == "Unnamed source"


def test_counts_come_straight_from_the_row():
    out = _run_response(_run())
    assert (out.tables_scanned, out.personal_data_elements) == (21, 48)


def test_finished_runs_have_no_status_detail():
    assert _status_detail(_run("completed"), None) is None
    assert _status_detail(_run("failed"), _job("failed")) is None


def test_pending_with_no_job_says_it_will_not_progress():
    assert "will not progress" in _status_detail(_run("pending"), None)


def test_pending_with_queued_job_says_waiting_for_worker():
    detail = _status_detail(_run("pending"), _job("queued", minutes_old=7))
    assert "waiting for a worker" in detail and "7 min" in detail


def test_queued_retry_surfaces_the_last_error():
    assert "boom" in _status_detail(_run("pending"), _job("queued", error="boom"))


def test_running_job_is_reported_as_processing():
    assert "processing" in _status_detail(_run("analyzing"), _job("running", attempts=2))


def test_dead_job_reports_its_real_error():
    detail = _status_detail(_run("pending"), _job("failed", attempts=3, error="Reaped: worker X"))
    assert "Reaped: worker X" in detail and "3 attempt" in detail


# --- live database -------------------------------------------------------------


def _other_org_headers() -> dict:
    return {"Authorization": "Bearer " + jwt.encode(
        {"sub": str(uuid.uuid4()), "org_id": str(uuid.uuid4()), "aud": "authenticated",
         "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp())},
        os.environ["SUPABASE_JWT_SECRET"], algorithm="HS256")}


@_live_db_only
async def test_pending_run_explains_itself_and_repeated_pushes_are_distinct_runs(
    client, auth_headers, integration_headers
):
    ids = []
    for _ in range(2):
        r = await client.post("/api/v1/ropa/evidence", headers=integration_headers, json=_payload())
        assert r.status_code == 201, r.text
        ids.append(r.json()["id"])
    assert ids[0] != ids[1]  # no idempotency key => genuinely separate pushes

    runs = (await client.get("/api/v1/ropa/runs", headers=auth_headers)).json()
    mine = [r for r in runs if r["id"] in ids]
    assert len(mine) == 2
    for r in mine:
        assert r["status"] == "pending"
        assert r["source_name"] == "prepmyevent.com"
        assert r["data_source_id"] is None  # a pushed run is not a connector data source
        assert "waiting for a worker" in r["status_detail"]

    # an integration key is not a data source
    sources = (await client.get("/api/v1/ropa/sources", headers=auth_headers)).json()
    assert all(s["name"] != "prepmyevent-adapter-test" for s in sources)


@_live_db_only
async def test_idempotency_key_reuses_the_run(client, integration_headers):
    key = f"idem-{uuid.uuid4()}"
    a = await client.post("/api/v1/ropa/evidence", headers=integration_headers, json=_payload(key))
    b = await client.post("/api/v1/ropa/evidence", headers=integration_headers, json=_payload(key))
    assert a.json()["id"] == b.json()["id"]


@_live_db_only
async def test_dead_job_closes_the_run_with_the_real_error_and_stays_in_its_org(
    client, auth_headers, integration_headers, worker_session_factory
):
    from sqlalchemy import select

    from app.db.models import AgentJob
    from app.db.repositories import ropa_repository
    from app.db.session import async_session_factory

    r = await client.post("/api/v1/ropa/evidence", headers=integration_headers, json=_payload())
    run_id = uuid.UUID(r.json()["id"])

    async with async_session_factory() as db:
        job = (await db.execute(
            select(AgentJob).where(AgentJob.payload["run_id"].astext == str(run_id))
        )).scalar_one()
        job.status, job.attempts, job.error = "failed", 3, "Reaped: worker died"
        await db.commit()
        await ropa_repository.fail_runs_with_dead_jobs(db)
        await db.commit()

    run = (await client.get(f"/api/v1/ropa/runs/{run_id}", headers=auth_headers)).json()
    assert run["status"] == "failed"
    assert "Reaped: worker died" in run["error"]

    other = _other_org_headers()
    assert (await client.get(f"/api/v1/ropa/runs/{run_id}", headers=other)).status_code == 404
    assert str(run_id) not in str((await client.get("/api/v1/ropa/runs", headers=other)).json())


@_live_db_only
async def test_live_job_is_left_alone_by_reconciliation(
    client, auth_headers, integration_headers, worker_session_factory
):
    from app.db.repositories import ropa_repository
    from app.db.session import async_session_factory

    r = await client.post("/api/v1/ropa/evidence", headers=integration_headers, json=_payload())
    run_id = r.json()["id"]
    async with async_session_factory() as db:
        await ropa_repository.fail_runs_with_dead_jobs(db)
        await db.commit()
    run = (await client.get(f"/api/v1/ropa/runs/{run_id}", headers=auth_headers)).json()
    assert run["status"] == "pending"
