"""ROPA run naming and run-status honesty.

Covers: a source name is never an empty-bracket label like "Consiva ()"; a run that has
not finished says why from the queue; a run whose job permanently failed is closed with
that job's real error; runs and their status detail never cross organisations; and a
run pushed by an integration key surfaces that key's own operator-chosen name
(Integration Keys page) rather than only whatever generic label the external adapter
happened to send.
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
from app.db.repositories.ropa_repository import integration_key_names_for_runs
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


# --- custom integration-key names (root cause: the key's own name was never
# threaded through to the run or its API response) -----------------------------


def test_custom_integration_name_is_surfaced_alongside_a_real_source_name():
    """A meaningful, adapter-supplied source_name (e.g. 'prepmyevent.com') stays the
    displayed name -- it is usually more specific than the key that pushed it -- but
    the key's own custom name (Integration Keys page) is still returned, so the
    operator can always see which integration this run came from."""
    run = _run(source_name="prepmyevent.com")
    out = _run_response(run, integration_key_name="ROPA integration (write+read)")
    assert out.source_name == "prepmyevent.com"
    assert out.integration_key_name == "ROPA integration (write+read)"
    assert run.source_name == "prepmyevent.com"  # persisted value untouched


def test_integration_key_name_is_the_fallback_when_source_name_is_genuinely_unavailable():
    """The exact failure the bug report describes: an unnamed/placeholder row would
    otherwise show the generic 'Unnamed source' even though the organisation already
    gave this push's integration key a specific, meaningful name. That persisted name
    is a sensible fallback precisely because nothing else is genuinely available."""
    run = _run(source_name="()")
    out = _run_response(run, integration_key_name="ROPA integration (write+read)")
    assert out.source_name == "ROPA integration (write+read)"
    assert out.integration_key_name == "ROPA integration (write+read)"


def test_missing_metadata_still_falls_back_to_the_generic_label():
    """No integration key at all (a connector run, or a push predating migration
    0034) and no usable source_name either: there is genuinely nothing to show."""
    out = _run_response(_run(source_name="()"), integration_key_name=None)
    assert out.source_name == "Unnamed source"
    assert out.integration_key_name is None


def test_multiple_integrations_sharing_a_generic_source_name_stay_distinguishable():
    """Two different integrations whose adapters both left source_name at the same
    generic/boilerplate value (the reported symptom) must still be tellable apart --
    by the name their own key was given, not by rewriting source_name itself."""
    run_a = _run(source_name="Consiva")
    run_b = _run(source_name="Consiva")
    out_a = _run_response(run_a, integration_key_name="ROPA integration (write+read)")
    out_b = _run_response(run_b, integration_key_name="ROPA integration (read-only)")

    assert out_a.source_name == out_b.source_name == "Consiva"  # neither is rewritten
    assert out_a.integration_key_name != out_b.integration_key_name
    assert {out_a.integration_key_name, out_b.integration_key_name} == {
        "ROPA integration (write+read)", "ROPA integration (read-only)",
    }


def test_repeated_runs_from_the_same_key_all_carry_its_name():
    """A key that pushes several times (retried/periodic pushes) must label every
    one of its runs identically -- the name does not drift run to run."""
    runs = [_run(source_name="Consiva") for _ in range(3)]
    outs = [_run_response(r, integration_key_name="ROPA integration (write+read)") for r in runs]
    assert {o.integration_key_name for o in outs} == {"ROPA integration (write+read)"}
    assert len({o.id for o in outs}) == 3  # still genuinely distinct runs


class _FakeKeyRow(SimpleNamespace):
    """Mimics the (id, name) Row SQLAlchemy's select() returns."""


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeSession:
    """Stands in for AsyncSession in integration_key_names_for_runs: records the
    statement it was asked to run and hands back canned rows, so the function's own
    mapping/short-circuit logic is covered without a real database."""

    def __init__(self, rows):
        self._rows = rows
        self.executed = False

    async def execute(self, _stmt):
        self.executed = True
        return _FakeResult(self._rows)


async def test_integration_key_names_for_runs_maps_ids_to_names():
    key_a, key_b = uuid.uuid4(), uuid.uuid4()
    session = _FakeSession([
        _FakeKeyRow(id=key_a, name="ROPA integration (write+read)"),
        _FakeKeyRow(id=key_b, name="ROPA integration (read-only)"),
    ])
    result = await integration_key_names_for_runs(session, uuid.uuid4(), [key_a, key_b])
    assert result == {
        key_a: "ROPA integration (write+read)",
        key_b: "ROPA integration (read-only)",
    }
    assert session.executed


async def test_integration_key_names_for_runs_short_circuits_on_no_ids():
    """No DB round trip at all for a page of runs that are all connector-driven --
    the common case, since only evidence-push runs ever have an integration key."""
    session = _FakeSession([])
    result = await integration_key_names_for_runs(session, uuid.uuid4(), [])
    assert result == {}
    assert session.executed is False


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
        # repeated pushes from the same key all carry its own name
        assert r["integration_key_name"] == "prepmyevent-adapter-test"

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


@_live_db_only
async def test_two_integrations_with_the_same_source_name_are_distinguishable_end_to_end(
    client, auth_headers,
):
    """The bug report end to end: two DIFFERENT integration keys, each minted with its
    own custom name on the Integration Keys page, whose adapters both push the exact
    same (generic) source_name. The run list must still tell them apart -- by the
    key's name, since source_name alone cannot -- and must never invent or merge a
    data source for either of them."""
    key_one = await client.post(
        "/api/v1/ropa/integration-keys", headers=auth_headers,
        json={"name": "ROPA integration (write+read)"},
    )
    key_two = await client.post(
        "/api/v1/ropa/integration-keys", headers=auth_headers,
        json={"name": "ROPA integration (read-only, legacy)"},
    )
    assert key_one.status_code == key_two.status_code == 201, (key_one.text, key_two.text)
    headers_one = {"Authorization": f"Bearer {key_one.json()['api_key']}"}
    headers_two = {"Authorization": f"Bearer {key_two.json()['api_key']}"}

    def _generic_payload() -> dict:
        body = _payload()
        body["source_name"] = "Consiva"  # same generic label from both adapters
        body["correlation_id"] = str(uuid.uuid4())
        return body

    r1 = await client.post("/api/v1/ropa/evidence", headers=headers_one, json=_generic_payload())
    r2 = await client.post("/api/v1/ropa/evidence", headers=headers_two, json=_generic_payload())
    assert r1.status_code == r2.status_code == 201, (r1.text, r2.text)
    id_one, id_two = r1.json()["id"], r2.json()["id"]

    runs = (await client.get("/api/v1/ropa/runs", headers=auth_headers)).json()
    by_id = {r["id"]: r for r in runs}
    run_one, run_two = by_id[id_one], by_id[id_two]

    assert run_one["source_name"] == run_two["source_name"] == "Consiva"  # neither rewritten
    assert run_one["integration_key_name"] == "ROPA integration (write+read)"
    assert run_two["integration_key_name"] == "ROPA integration (read-only, legacy)"
    assert run_one["integration_key_name"] != run_two["integration_key_name"]

    # still no data source was invented for either pushed integration
    sources = (await client.get("/api/v1/ropa/sources", headers=auth_headers)).json()
    assert all(s["name"] not in ("ROPA integration (write+read)", "ROPA integration (read-only, legacy)")
               for s in sources)
