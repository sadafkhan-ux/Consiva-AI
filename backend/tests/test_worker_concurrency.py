"""The worker's concurrent slots, and the separation of job work from the periodic duties.

Both of these are behaviours the old worker did not have. It claimed one job per poll
and awaited it inline, which meant (a) a second scan waited out the first -- measured
at 73s of queue time for two scans started 48s apart, which is what made the UI report
a backend timeout over a scan that finished fine -- and (b) reap_stale_jobs and the two
SLA sweeps were blocked for the whole duration of whatever job was running, including
the reaping that is supposed to recover a job that has hung.
"""

import asyncio
import uuid
from types import SimpleNamespace

from app.jobs import worker


class _FakeSessionCM:
    """Stands in for async_session_factory() -- _run_job opens one only to record the
    job's outcome, which these tests stub out anyway."""

    def __init__(self):
        self.commit = _noop

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


async def _noop(*args, **kwargs):
    return None


def _job():
    return SimpleNamespace(id=uuid.uuid4(), job_type="scan", payload={})


def _install_stubs(monkeypatch, pending, on_process):
    """Hand `pending` out honouring the pump's requested limit, and run on_process."""

    async def fake_dequeue_batch(db, *, worker_id, limit):
        claimed = pending[:limit]
        del pending[:limit]
        return claimed

    monkeypatch.setattr(worker.queue, "dequeue_batch", fake_dequeue_batch)
    monkeypatch.setattr(worker.queue, "mark_done", _noop)
    monkeypatch.setattr(worker.queue, "mark_failed", _noop)
    monkeypatch.setattr(worker, "async_session_factory", _FakeSessionCM)
    monkeypatch.setattr(worker, "_process_one", on_process)


async def _drain(concurrency, *, until, timeout=5.0):
    """Run the pump until `until()` is true, then stop it and wait for the drain."""
    stopping = asyncio.Event()
    pump = asyncio.create_task(worker._job_pump(stopping, concurrency))
    deadline = asyncio.get_running_loop().time() + timeout
    while not until():
        if pump.done() or asyncio.get_running_loop().time() > deadline:
            break
        await asyncio.sleep(0.01)
    stopping.set()
    await asyncio.wait_for(pump, timeout=timeout)


async def test_the_pump_runs_several_jobs_at_once(monkeypatch):
    """Three jobs, three slots: all three overlap. On the old serial loop the peak
    would be 1 no matter how many were queued, which is the whole bug."""
    pending = [_job() for _ in range(3)]
    live = peak = 0
    done: list[uuid.UUID] = []

    async def on_process(job):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0.05)
        live -= 1
        done.append(job.id)

    _install_stubs(monkeypatch, pending, on_process)
    await _drain(3, until=lambda: len(done) == 3)

    assert peak == 3, f"jobs did not overlap (peak concurrency {peak})"
    assert len(done) == 3


async def test_the_pump_never_exceeds_its_slot_count(monkeypatch):
    """Five queued jobs, two slots: all five run, never more than two at a time. This
    is the bound that keeps concurrency from outrunning the connection pool and the
    memory each concurrent Chromium needs."""
    pending = [_job() for _ in range(5)]
    live = peak = 0
    done: list[uuid.UUID] = []

    async def on_process(job):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0.05)
        live -= 1
        done.append(job.id)

    _install_stubs(monkeypatch, pending, on_process)
    await _drain(2, until=lambda: len(done) == 5)

    assert peak == 2, f"expected at most 2 concurrent jobs, saw {peak}"
    assert len(done) == 5, "not every queued job ran"


async def test_a_failing_job_does_not_stop_the_pump(monkeypatch):
    """One bad job must not take the worker down, or strand the jobs behind it. _run_job
    handles its own exceptions because it runs detached -- anything escaping would
    surface only as an unretrieved-exception warning at GC time."""
    pending = [_job() for _ in range(3)]
    seen: list[uuid.UUID] = []

    async def on_process(job):
        seen.append(job.id)
        if len(seen) == 1:
            raise RuntimeError("first job explodes")
        await asyncio.sleep(0.01)

    _install_stubs(monkeypatch, pending, on_process)
    await _drain(2, until=lambda: len(seen) == 3)

    assert len(seen) == 3, "a failing job stopped the jobs behind it"


async def test_a_long_job_does_not_block_the_maintenance_loop(monkeypatch):
    """The regression that made a hung job unrecoverable: reaping used to sit at the top
    of the same loop that ran the job, so a wedged job blocked the one mechanism that
    recovers it. The two loops are independent now, so maintenance ticks while a job is
    still running."""
    pending = [_job()]
    started = asyncio.Event()
    finish = asyncio.Event()

    async def on_process(job):
        started.set()
        await finish.wait()

    _install_stubs(monkeypatch, pending, on_process)

    reaps = 0

    async def _noop_sweep():
        return 0

    async def fake_reap(db):
        nonlocal reaps
        reaps += 1
        return 0

    monkeypatch.setattr(worker.queue, "reap_stale_jobs", fake_reap)
    monkeypatch.setattr(worker.monitoring_service, "dispatch_due_schedules", _noop)
    monkeypatch.setattr(worker.dsr_sla_service, "sweep_overdue", _noop)
    monkeypatch.setattr(worker.incident_sla_service, "sweep_overdue", _noop)
    # Agent 5's due-source sweep rides the same loop and opens its own session. Stubbed
    # like the other three: this test is about the two loops being independent, and a
    # real database round trip per tick at a 10ms interval made it fail under load
    # while passing in isolation.
    monkeypatch.setattr(worker.regwatch_run_service, "sweep_due_sources", _noop_sweep)
    monkeypatch.setattr(worker, "MAINTENANCE_INTERVAL_SECONDS", 0.01)

    stopping = asyncio.Event()
    pump = asyncio.create_task(worker._job_pump(stopping, 1))
    maintenance = asyncio.create_task(worker._maintenance_loop(stopping))

    await asyncio.wait_for(started.wait(), timeout=5)
    at_job_start = reaps
    # The job is deliberately still running for all of this.
    await asyncio.sleep(0.1)
    assert reaps > at_job_start, "maintenance did not tick while a job was in flight"

    finish.set()
    stopping.set()
    await asyncio.wait_for(asyncio.gather(pump, maintenance), timeout=5)
