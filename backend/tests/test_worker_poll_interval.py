"""The worker's poll interval is the floor on the hand-off between a scan's steps (measured ~2.1s at 2s polling)."""

from app.jobs import worker


def test_an_idle_worker_looks_for_work_at_least_every_second():
    assert 0 < worker.POLL_INTERVAL_SECONDS <= 1.0


def test_the_maintenance_sweeps_are_not_sped_up_with_it():
    assert worker.MAINTENANCE_INTERVAL_SECONDS >= 30
