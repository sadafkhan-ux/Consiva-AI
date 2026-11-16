"""Live crawl progress: throttled, best-effort, and wired into the crawler and the scan service."""
import asyncio
from pathlib import Path

from app.scanner.progress import ProgressReporter

APP = Path(__file__).resolve().parents[1] / "app"


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def run(coro):
    return asyncio.run(coro)


def counters(**kw):
    return {"pages_discovered": 40, "pages_attempted": 12, "pages_scanned": 10, "pages_failed": 1, "pages_timeout": 1, "noise": "ignored", **kw}


def test_the_first_report_goes_out_at_once_and_carries_only_the_counters_marked_live():
    sent = []

    async def cb(snapshot):
        sent.append(snapshot)

    r = ProgressReporter(cb, interval=3, clock=Clock())
    assert run(r.tick(counters())) is True
    assert sent == [{"pages_discovered": 40, "pages_attempted": 12, "pages_scanned": 10, "pages_failed": 1, "pages_timeout": 1, "live": True}]


def test_reports_are_throttled_to_one_per_interval_and_resume_after_it():
    clock, sent = Clock(), []

    async def cb(snapshot):
        sent.append(snapshot["pages_scanned"])

    r = ProgressReporter(cb, interval=3, clock=clock)
    run(r.tick(counters(pages_scanned=1)))
    clock.t += 1
    assert run(r.tick(counters(pages_scanned=2))) is False          # 1 s later: too soon
    clock.t += 2.5
    assert run(r.tick(counters(pages_scanned=3))) is True           # 3.5 s after the first
    assert sent == [1, 3]


def test_force_ignores_the_throttle():
    sent = []

    async def cb(snapshot):
        sent.append(1)

    r = ProgressReporter(cb, interval=60, clock=Clock())
    run(r.tick(counters()))
    assert run(r.tick(counters(), force=True)) is True and len(sent) == 2


def test_a_failing_callback_never_breaks_the_crawl():
    async def boom(snapshot):
        raise RuntimeError("database is down")

    r = ProgressReporter(boom, interval=3, clock=Clock())
    assert run(r.tick(counters())) is False                         # reported as not sent, and nothing was raised


def test_no_callback_means_no_work():
    r = ProgressReporter(None)
    assert r.enabled is False and run(r.tick(counters())) is False


def test_a_missing_counter_is_zero_not_an_error():
    sent = []

    async def cb(s):
        sent.append(s)

    run(ProgressReporter(cb, clock=Clock()).tick({"pages_scanned": 3}))
    assert sent[0]["pages_scanned"] == 3 and sent[0]["pages_failed"] == 0


def test_the_crawler_reports_between_batches_and_the_scan_service_writes_it_into_the_running_stage():
    crawler = (APP / "scanner" / "crawler.py").read_text(encoding="utf-8")
    assert "await progress.tick(diagnostics)" in crawler and "on_progress=on_progress" in crawler
    service = (APP / "services" / "scan_service.py").read_text(encoding="utf-8")
    assert "merge_running_stage_metadata(progress_db, scan_id, \"website_scan\", snapshot)" in service and "on_progress=_report_progress" in service
    repo = (APP / "db" / "repositories" / "stage_repository.py").read_text(encoding="utf-8")
    assert "status = 'running'" in repo.split("async def merge_running_stage_metadata")[1].split("async def complete_stage")[0]      # a finished stage is never overwritten
