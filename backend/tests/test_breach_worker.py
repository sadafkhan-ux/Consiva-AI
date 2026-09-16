"""The analysis job's route through the lifecycle (§19, §41).

These exist because of a defect a live run found and a unit test could not: the job
assessed impact and risk correctly, then tried to move the incident straight from
INVESTIGATING to RESPONSE_PENDING. That edge does not exist -- and should not, since an
incident reaching response planning has had its impact and its risk assessed -- so the
lifecycle refused, the refusal was caught as a domain error, and a completed analysis
was recorded as FAILED.

The property under test is therefore not "the job produces a risk score" (covered
elsewhere) but "wherever the job leaves an incident, it got there by edges that exist".
"""

import itertools
import uuid

import pytest

from app.agents.breach.schemas import incident as vocab
from app.agents.breach.services import lifecycle
from app.db.models import IncidentCase
from app.services import incident_run_service

ORG = uuid.uuid4()


def make_case(status=vocab.INVESTIGATING):
    return IncidentCase(
        id=uuid.uuid4(), org_id=ORG, reference="INC-WORKER01", title="t",
        description="d", source=vocab.SOURCE_SIEM, status=status,
        incident_type=vocab.TYPE_UNAUTHORIZED_ACCESS,
        personal_data_involved=vocab.UNKNOWN, breach_confirmed=vocab.UNKNOWN,
    )


class _DB:
    async def flush(self):
        return None


# ── The route itself ─────────────────────────────────────────────────────────────

def test_the_stages_the_job_walks_are_all_real_edges():
    """INVESTIGATING -> IMPACT_ASSESSMENT -> RISK_ASSESSMENT -> RESPONSE_PENDING, each
    hop legal. If someone tightens the lifecycle later, this fails here rather than in
    a worker log at 3am."""
    route = [
        vocab.INVESTIGATING,
        vocab.IMPACT_ASSESSMENT,
        vocab.RISK_ASSESSMENT,
        vocab.RESPONSE_PENDING,
    ]
    for origin, destination in itertools.pairwise(route):
        assert lifecycle.can_transition(origin, destination), (
            f"the analysis job walks {origin} -> {destination}, which the lifecycle "
            "does not allow"
        )


def test_the_thin_evidence_route_is_also_real():
    """An assessment held with UNKNOWN confidence goes to a person instead."""
    assert lifecycle.can_transition(vocab.RISK_ASSESSMENT, vocab.REVIEW_REQUIRED)


def test_investigating_does_not_jump_straight_to_response_pending():
    """The edge the job used to assume. Asserting its ABSENCE keeps the fix honest: if
    somebody adds it later, the extra hops in the job become dead code and this test
    says so."""
    assert not lifecycle.can_transition(vocab.INVESTIGATING, vocab.RESPONSE_PENDING)


@pytest.mark.asyncio
async def test_a_stage_that_cannot_be_entered_is_skipped_not_fatal(monkeypatch):
    """A person may have moved the incident while the job was running. That is a
    reason to skip a stage, not to abandon an analysis that is otherwise fine."""
    case = make_case(status=vocab.REVIEW_REQUIRED)
    moved: list[str] = []

    async def fake_transition(db, c, to_status, **kw):
        moved.append(to_status)
        c.status = to_status
        return c

    monkeypatch.setattr(incident_run_service.incident_service, "transition", fake_transition)

    # REVIEW_REQUIRED -> IMPACT_ASSESSMENT is legal; -> RISK_ASSESSMENT is too.
    await incident_run_service._step(
        _DB(), case, vocab.IMPACT_ASSESSMENT, vocab.AUDIT_SYSTEMS_IDENTIFIED
    )
    assert moved == [vocab.IMPACT_ASSESSMENT]

    # CLOSED is not reachable from here, and asking for it must be a no-op.
    await incident_run_service._step(_DB(), case, vocab.CLOSED, vocab.AUDIT_CLOSED)
    assert moved == [vocab.IMPACT_ASSESSMENT]
    assert case.status == vocab.IMPACT_ASSESSMENT


@pytest.mark.asyncio
async def test_a_stage_already_reached_is_not_re_entered(monkeypatch):
    case = make_case(status=vocab.RISK_ASSESSMENT)
    moved: list[str] = []

    async def fake_transition(db, c, to_status, **kw):
        moved.append(to_status)
        return c

    monkeypatch.setattr(incident_run_service.incident_service, "transition", fake_transition)
    await incident_run_service._step(
        _DB(), case, vocab.RISK_ASSESSMENT, vocab.AUDIT_RISK_ASSESSED
    )
    assert moved == []


# ── Where the job leaves an incident ─────────────────────────────────────────────

def test_an_unknown_confidence_assessment_routes_to_a_person():
    """A risk level nobody can trust is not a basis for planning containment."""
    from types import SimpleNamespace

    thin = SimpleNamespace(confidence=vocab.UNKNOWN)
    assert incident_run_service.next_status_after_analysis(thin) == vocab.REVIEW_REQUIRED


@pytest.mark.parametrize("level", [vocab.POSSIBLE, vocab.PROBABLE])
def test_a_usable_assessment_routes_to_response_planning(level):
    from types import SimpleNamespace

    assert (
        incident_run_service.next_status_after_analysis(SimpleNamespace(confidence=level))
        == vocab.RESPONSE_PENDING
    )


# ── The queue contract ───────────────────────────────────────────────────────────

def test_the_worker_dispatches_incident_analysis():
    import inspect

    from app.jobs import worker

    source = inspect.getsource(worker._process_one)
    assert '"incident_analysis"' in source
    assert "incident_run_service.run_analysis" in source


def test_the_two_sla_sweeps_do_not_shadow_one_another():
    """Agents 3 and 4 both have an sla_service. Importing both under the same name
    would silently leave one sweep never running."""
    from app.jobs import worker

    assert worker.dsr_sla_service is not worker.incident_sla_service
    assert worker.incident_sla_service.__name__.endswith("breach.services.sla_service")


def test_the_incident_sweep_runs_in_the_worker_loop():
    """Both sweeps have to be on a loop the worker actually starts.

    They used to sit in run_forever itself. They now live in _maintenance_loop, which
    run_forever runs as its own task so that a long job cannot block the periodic
    duties. So this checks both halves: the calls are in the maintenance loop, AND
    run_forever still starts that loop. Asserting only the first would pass happily if
    the loop were never scheduled, which is the silent stop the sweeps' own comments
    warn about.
    """
    import inspect

    from app.jobs import worker

    maintenance = inspect.getsource(worker._maintenance_loop)
    assert "incident_sla_service.sweep_overdue" in maintenance
    assert "dsr_sla_service.sweep_overdue" in maintenance

    started = inspect.getsource(worker.run_forever)
    assert "_maintenance_loop" in started, (
        "the sweeps live in _maintenance_loop but run_forever no longer starts it, so "
        "neither sweep would ever run"
    )


# ── The audit trail has to read in order ─────────────────────────────────────────

def test_audit_rows_are_stamped_at_write_not_at_transaction_start():
    """Postgres `now()` is TRANSACTION start time, so every audit row written in one
    request shared a timestamp and a trail ordered by created_at came back in
    arbitrary order -- a live run produced "classified, created, validated".

    The audit trail is the artifact somebody reconstructs an incident from. It has to
    read in the order things happened.
    """
    import inspect

    from app.db.repositories import audit_repository

    source = inspect.getsource(audit_repository.record)
    assert "created_at=datetime.now(UTC)" in source, (
        "audit rows are relying on the column default again; rows written in one "
        "transaction will share a timestamp and the trail will not read in order"
    )
