"""Crawl controls for application scans: clamping, the rate limiter, the resource guard, and how a scan profile carries them. Offline, no browser."""

import asyncio
import dataclasses
import itertools

import pytest

from app.config import get_settings
from app.scanner.crawl_controls import (
    CrawlControls,
    DomainRateLimiter,
    ResourceGuard,
    build_controls,
    controls_from_options,
)
from app.scanner.scan_profiles import (
    APPLICATION,
    WEB,
    application_crawl_options,
    profile_for_scan_options,
    resolve_profile,
)

S = get_settings()


# ── clamping and defaults ────────────────────────────────────────────────────────────────────────────

def test_a_request_with_nothing_in_it_gets_the_documented_defaults():
    c = build_controls(None, S)
    assert c.max_depth is None
    assert c.max_urls == S.consent_app_default_max_urls
    assert c.concurrency == S.scanner_max_concurrent_pages and c.per_domain_concurrency == c.concurrency
    assert c.requests_per_second == S.consent_app_default_rps and c.memory_budget_mb == 0


def test_every_value_is_lowered_to_its_ceiling_never_refused():
    c = build_controls({"max_depth": 10_000, "max_urls": 99_999_999, "concurrency": 1_000, "per_domain_concurrency": 1_000, "requests_per_second": 5_000.0, "memory_budget_mb": 10**9}, S)
    assert c.max_depth == S.consent_app_crawl_max_depth_ceiling
    assert c.max_urls == S.consent_app_max_urls_ceiling
    assert c.concurrency == S.consent_app_concurrency_ceiling and c.per_domain_concurrency == c.concurrency
    assert c.requests_per_second == S.consent_app_rps_ceiling
    assert c.memory_budget_mb == S.consent_app_memory_budget_ceiling_mb


def test_values_inside_the_ceilings_are_honoured():
    c = build_controls({"max_depth": 3, "max_urls": 250, "concurrency": 2, "per_domain_concurrency": 1, "requests_per_second": 1.5, "memory_budget_mb": 2048}, S)
    assert (c.max_depth, c.max_urls, c.concurrency, c.per_domain_concurrency, c.requests_per_second, c.memory_budget_mb) == (3, 250, 2, 1, 1.5, 2048)
    assert c.effective_concurrency == 1          # a scan crawls one domain, so the smaller of the two applies


def test_per_domain_concurrency_can_never_exceed_overall_concurrency():
    assert build_controls({"concurrency": 2, "per_domain_concurrency": 9}, S).per_domain_concurrency == 2


def test_depth_zero_means_only_the_entry_address():
    assert build_controls({"max_depth": 0}, S).max_depth == 0


# ── what is stored on the scan and read back by the worker ────────────────────────────────────────────

def test_the_stored_block_round_trips():
    stored = {"crawl": application_crawl_options({"max_depth": 4, "max_urls": 100})}
    c = controls_from_options(stored, S)
    assert c is not None and (c.max_depth, c.max_urls) == (4, 100)


def test_a_scan_with_no_crawl_block_has_no_controls_and_runs_as_it_always_did():
    assert controls_from_options(None, S) is None and controls_from_options({"profile": APPLICATION}, S) is None
    assert profile_for_scan_options({"profile": APPLICATION}, S).crawl is None


def test_a_tampered_block_cannot_widen_the_controls():
    c = controls_from_options({"crawl": {"max_depth": 10**6, "max_urls": 10**12, "concurrency": 10**6, "requests_per_second": 10**6}}, S)
    assert c.max_depth == S.consent_app_crawl_max_depth_ceiling and c.max_urls == S.consent_app_max_urls_ceiling
    assert c.concurrency == S.consent_app_concurrency_ceiling and c.requests_per_second == S.consent_app_rps_ceiling


@pytest.mark.parametrize("bad", [-1, "10", True, [3], {"x": 1}])
def test_a_malformed_stored_value_falls_back_to_the_default_not_to_no_limit(bad):
    c = controls_from_options({"crawl": {"max_urls": bad, "max_depth": bad}}, S)
    assert c.max_urls == S.consent_app_default_max_urls and c.max_depth is None


# ── profiles ─────────────────────────────────────────────────────────────────────────────────────────

def test_an_application_profile_carries_its_controls():
    p = profile_for_scan_options({"profile": APPLICATION, "crawl": application_crawl_options({"max_depth": 2})}, S)
    assert p.name == APPLICATION and p.crawl is not None and p.crawl.max_depth == 2


def test_the_public_web_profile_has_no_controls_whatever_the_stored_options_say():
    p = profile_for_scan_options({"profile": WEB, "crawl": application_crawl_options({"max_depth": 9, "max_urls": 9999})}, S)
    assert p.name == WEB and p.crawl is None and p.max_pages == 5


def test_a_missing_or_unknown_profile_is_still_the_small_one():
    for opts in (None, {}, {"profile": "ADMIN"}, {"profile": "Application"}):
        p = profile_for_scan_options({**(opts or {}), "crawl": application_crawl_options(None)}, S)
        assert p.name == WEB and p.crawl is None and p.max_pages == 5


def test_the_web_profile_is_unchanged_by_this_feature():
    p = resolve_profile(WEB, S)
    assert (p.max_pages, p.crawl_deadline_seconds, p.requires_verified_domain) == (5, None, False)
    assert dataclasses.is_dataclass(p) and p.crawl is None


# ── the rate limiter ─────────────────────────────────────────────────────────────────────────────────

class _FakeTime:
    """A clock and a sleep that agree with each other, so the limiter is tested without waiting."""

    def __init__(self):
        self.now = 100.0
        self.slept: list[float] = []

    def clock(self):
        return self.now

    async def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


async def test_navigation_starts_are_spaced_at_least_one_interval_apart():
    ft = _FakeTime()
    lim = DomainRateLimiter(2.0, clock=ft.clock, sleep=ft.sleep)       # 2 per second: 0.5 s apart
    starts = []
    for _ in range(4):
        await lim.acquire()
        starts.append(ft.now)
    gaps = [round(b - a, 6) for a, b in itertools.pairwise(starts)]
    assert all(g >= 0.5 - 1e-9 for g in gaps) and lim.waits == 3


async def test_a_burst_of_simultaneous_requests_is_serialised_not_released_together():
    import time

    lim = DomainRateLimiter(100.0)                       # one start per 10 ms, in real time
    stamps: list[float] = []

    async def go():
        await lim.acquire()
        stamps.append(time.monotonic())

    began = time.monotonic()
    await asyncio.gather(*(go() for _ in range(6)))
    stamps.sort()
    assert stamps[-1] - began >= 5 * 0.01 - 0.003        # six starts need five intervals, not one
    assert len({round(t, 3) for t in stamps}) >= 4                      # they are released one after another, not all at one instant (timer slack allowed)


async def test_an_idle_limiter_lets_the_next_request_straight_through():
    ft = _FakeTime()
    lim = DomainRateLimiter(1.0, clock=ft.clock, sleep=ft.sleep)
    await lim.acquire()
    ft.now += 10                                                          # a long quiet spell earns no backlog of free requests
    await lim.acquire()
    await lim.acquire()
    assert lim.waits == 1


@pytest.mark.parametrize("rps", [0, -1])
def test_a_non_positive_rate_is_refused(rps):
    with pytest.raises(ValueError):
        DomainRateLimiter(rps)


# ── the resource guard ───────────────────────────────────────────────────────────────────────────────

def test_with_no_budget_nothing_is_ever_blocked_and_memory_is_not_read():
    calls = []
    g = ResourceGuard(0, reader=lambda: calls.append(1) or 10**6)
    assert g.over_budget() is False and calls == [] and g.events == []


def test_over_budget_blocks_and_each_change_of_state_is_recorded_once():
    readings = iter([500.0, 900.0, 950.0, 400.0, 300.0])
    g = ResourceGuard(800, reader=lambda: next(readings))
    assert [g.over_budget() for _ in range(5)] == [False, True, True, False, False]
    assert [e["event"] for e in g.events] == ["throttled", "resumed"]
    assert g.is_throttled is False and g.peak_mb == 950.0


def test_memory_that_cannot_be_measured_is_reported_as_not_enforced_once_and_does_not_block():
    g = ResourceGuard(800, reader=lambda: None)
    assert g.over_budget() is False and g.over_budget() is False
    assert [e["event"] for e in g.events] == ["memory_budget_not_enforced"]


def test_the_real_reader_measures_this_process():
    from app.scanner.crawl_controls import _process_memory_mb
    mb = _process_memory_mb()
    assert mb is None or mb > 1


def test_controls_are_immutable():
    c = build_controls(None, S)
    with pytest.raises(dataclasses.FrozenInstanceError):
        c.max_urls = 1            # type: ignore[misc]
    assert isinstance(c, CrawlControls)
