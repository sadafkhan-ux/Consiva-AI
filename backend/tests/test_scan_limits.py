"""0 means unlimited for both per-organisation scan limits; a positive number enforces it; a burst is never refused by default."""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import get_settings
from app.core.exceptions import RateLimitExceededError
from app.services import scan_service
from tests import test_scan_profiles as sp


def test_both_limits_default_to_unlimited():
    fields = type(get_settings()).model_fields          # the field defaults: the test environment overrides the live values
    assert fields["scanner_max_scans_per_org_per_day"].default == 0 and fields["consent_api_scans_per_hour"].default == 0


async def test_the_daily_limit_at_zero_is_not_even_counted(monkeypatch):
    count = AsyncMock(return_value=10**6)
    monkeypatch.setattr(scan_service.scan_repository, "count_scans_since", count)
    monkeypatch.setattr(scan_service, "get_settings", lambda: get_settings().model_copy(update={"scanner_max_scans_per_org_per_day": 0}))

    class Passed(Exception):
        pass

    async def boom(*a, **k):
        raise Passed()
    monkeypatch.setattr(scan_service.scan_repository, "get_or_create_website", boom)
    with pytest.raises(Passed):
        await scan_service.request_scan(AsyncMock(), org_id=uuid.uuid4(), user_id=uuid.uuid4(), url="https://example.com", authorized=True)
    count.assert_not_awaited()


def _hourly(monkeypatch, limit, recent):
    rec = sp._wire_submit(monkeypatch)
    count = AsyncMock(return_value=recent)
    monkeypatch.setattr(sp.route.api_service, "count_scans_since", count)
    monkeypatch.setattr(sp.route, "get_settings", lambda: sp._settings(
        consent_web_max_pages=5, consent_app_max_pages=0, consent_app_max_seconds=1800, consent_api_scans_per_hour=limit))
    return rec, count


async def _submit():
    return await sp.route.create_scan(body=sp._body(), response=MagicMock(), idempotency_key=None, user=sp.ADMIN, db=sp._Db())


async def test_the_hourly_limit_at_zero_accepts_any_burst_without_counting(monkeypatch):
    rec, count = _hourly(monkeypatch, limit=0, recent=10**6)
    await _submit()
    assert rec.enqueued is not None
    count.assert_not_awaited()


async def test_a_positive_hourly_limit_is_enforced(monkeypatch):
    rec, _count = _hourly(monkeypatch, limit=5, recent=5)
    with pytest.raises(RateLimitExceededError, match="5/hour"):
        await _submit()
    assert rec.enqueued is None


async def test_a_positive_hourly_limit_allows_scans_below_it(monkeypatch):
    rec, _count = _hourly(monkeypatch, limit=5, recent=4)
    await _submit()
    assert rec.enqueued is not None


async def _passes_the_gates(monkeypatch, queued, limit):
    s = get_settings().model_copy(update={"scanner_max_queued_scans_per_org": limit, "scanner_max_scans_per_org_per_day": 0})
    monkeypatch.setattr(scan_service, "get_settings", lambda: s)
    count = AsyncMock(return_value=queued)
    monkeypatch.setattr(scan_service.scan_repository, "count_queued_scans", count)

    class Passed(Exception):
        pass

    async def boom(*a, **k):
        raise Passed()
    monkeypatch.setattr(scan_service.scan_repository, "get_or_create_website", boom)
    try:
        await scan_service.request_scan(AsyncMock(), org_id=uuid.uuid4(), user_id=uuid.uuid4(), url="https://example.com", authorized=True)
    except Passed:
        return True, count
    return False, count


async def test_the_queue_ceiling_defaults_to_none():
    assert type(get_settings()).model_fields["scanner_max_queued_scans_per_org"].default == 0


async def test_a_queue_ceiling_of_zero_is_not_even_counted(monkeypatch):
    passed, count = await _passes_the_gates(monkeypatch, queued=10**6, limit=0)
    assert passed
    count.assert_not_awaited()


async def test_a_full_queue_refuses_a_new_scan_with_429(monkeypatch):
    with pytest.raises(RateLimitExceededError, match="waiting"):
        await _passes_the_gates(monkeypatch, queued=20, limit=20)


async def test_a_queue_below_the_ceiling_accepts_a_scan(monkeypatch):
    passed, _ = await _passes_the_gates(monkeypatch, queued=19, limit=20)
    assert passed
