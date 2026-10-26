"""A schedule does not repeat a request that is known to be refused for a reason only the site's owner can change, and does try again once the pause is over."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.config import get_settings
from app.services import monitoring_service as svc

NOW = datetime.now(UTC)


def last_scan(status="failed", error="", age_hours=1.0):
    return SimpleNamespace(status=status, error=error, url="https://x.test/", completed_at=NOW - timedelta(hours=age_hours), created_at=NOW - timedelta(hours=age_hours))


@pytest.fixture
def world(monkeypatch):
    schedule = SimpleNamespace(id=uuid4(), org_id=uuid4(), website_id=uuid4())
    state = SimpleNamespace(last=None, postponed=0, created=0, schedule=schedule)

    async def latest(db, *, org_id, website_id):
        return state.last

    async def postpone(db, sch):
        state.postponed += 1

    async def create(db, **kw):
        state.created += 1
        return SimpleNamespace(id=uuid4(), url=kw["url"])

    monkeypatch.setattr(svc.scan_repository, "latest_scan_for_website", latest)
    monkeypatch.setattr(svc.monitoring_repository, "due_schedules", AsyncMock(return_value=[schedule]))
    monkeypatch.setattr(svc.monitoring_repository, "postpone", postpone)
    monkeypatch.setattr(svc.monitoring_repository, "mark_dispatched", AsyncMock())
    monkeypatch.setattr(svc.scan_repository, "get_website_by_id", AsyncMock(return_value=SimpleNamespace(id=schedule.website_id, domain="x.test")))
    monkeypatch.setattr(svc.scan_repository, "create_scan", create)
    monkeypatch.setattr(svc.audit_service, "record", AsyncMock())
    monkeypatch.setattr(svc.queue, "enqueue", AsyncMock())
    return state


class DB:
    async def commit(self):
        return None


PERMANENT = [
    "The robots.txt of x.test does not allow the scanner to visit https://x.test/ (or any page found on it)",
    "Could not reach the site at all: https://x.test/ does not present a valid security certificate (certificate has expired).",
    "https://x.test/ answered with HTTP 403 instead of a page. The site is refusing the scanner (access denied)",
    "https://x.test/ answered with HTTP 401 instead of a page",
]
TRANSIENT = [
    "https://x.test/ kept answering with HTTP 503 (temporarily unavailable) after 6 attempts",
    "https://x.test/ was too slow or unavailable over the whole retry window (10 attempts; Timeout 30000ms exceeded)",
    "Could not read the robots.txt of x.test (it answered HTTP 503), so it cannot be determined which pages the site permits",
]


async def test_a_recent_permanent_failure_pauses_the_schedule_and_moves_it_on(world):
    for error in PERMANENT:
        world.last, world.created, world.postponed = last_scan(error=error), 0, 0
        assert await svc.dispatch_due_schedules(DB()) == 0, error
        assert world.created == 0 and world.postponed == 1


async def test_once_the_pause_is_over_it_tries_again(world):
    world.last = last_scan(error=PERMANENT[1], age_hours=get_settings().scheduled_scan_permanent_failure_pause_hours + 1)
    assert await svc.dispatch_due_schedules(DB()) == 1 and world.created == 1 and world.postponed == 0


@pytest.mark.parametrize("error", TRANSIENT)
async def test_a_transient_failure_does_not_pause_it(world, error):
    world.last = last_scan(error=error)
    assert await svc.dispatch_due_schedules(DB()) == 1 and world.created == 1


@pytest.mark.parametrize("status", ["completed", "cancelled", "running"])
async def test_only_a_failed_scan_pauses_it(world, status):
    world.last = last_scan(status=status, error=PERMANENT[0])
    assert await svc.dispatch_due_schedules(DB()) == 1


async def test_a_website_never_scanned_is_scanned(world):
    world.last = None
    assert await svc.dispatch_due_schedules(DB()) == 1


async def test_the_pause_can_be_turned_off(world, monkeypatch):
    monkeypatch.setattr(svc, "get_settings", lambda: get_settings().model_copy(update={"scheduled_scan_permanent_failure_pause_hours": 0}))
    world.last = last_scan(error=PERMANENT[0])
    assert await svc.dispatch_due_schedules(DB()) == 1


def test_the_default_pause_is_a_day():
    assert type(get_settings()).model_fields["scheduled_scan_permanent_failure_pause_hours"].default == 24
