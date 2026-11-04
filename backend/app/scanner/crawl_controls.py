"""Crawl controls for authorized application scans: how deep, how many URLs, how fast, how many at once, how much memory.

Server-side only. A caller's request is a wish: every value is clamped to a ceiling in Settings (config.py, CONSENT_APP_*), the effective values are
stored on the scan (`scan_options.crawl`) and read back by the worker, so what ran is what the response says ran. The public web profile has no
controls: its five-page cap and behaviour are not touched by anything here.

The rate limiter and the resource guard are small and take their clock and memory reader as arguments, so they are tested without a browser or a
real clock.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from typing import Any

from app.config import Settings


@dataclass(frozen=True)
class CrawlControls:
    #: Deepest link level crawled (the entry address is 0). None = no depth limit.
    max_depth: int | None
    #: Most distinct URLs recorded in the inventory. None = no cap.
    max_urls: int | None
    #: Pages in flight in this scan, and in flight against one domain. A scan crawls one domain, so the smaller of the two applies.
    concurrency: int
    per_domain_concurrency: int
    #: Navigation starts per second against the domain.
    requests_per_second: float
    #: Pause launching pages while the scanner's browsers use more than this many MB. 0 = no budget.
    memory_budget_mb: int

    @property
    def effective_concurrency(self) -> int:
        return max(1, min(self.concurrency, self.per_domain_concurrency))

    def as_options(self) -> dict[str, Any]:
        return asdict(self)


def build_controls(requested: dict[str, Any] | None, settings: Settings) -> CrawlControls:
    """The controls for a request: every value inside its ceiling, every missing value its default. Values arrive already range-checked by the API
    schema (>= 1); a value above a ceiling is lowered to it, never refused."""
    r = requested or {}

    def pick(name: str, default, ceiling):
        v = r.get(name)
        v = default if v is None else v
        return min(v, ceiling)

    concurrency = pick("concurrency", settings.scanner_max_concurrent_pages, settings.consent_app_concurrency_ceiling)
    per_domain = pick("per_domain_concurrency", concurrency, concurrency)
    max_urls = pick("max_urls", settings.consent_app_default_max_urls, settings.consent_app_max_urls_ceiling)
    depth = r.get("max_depth")
    return CrawlControls(
        max_depth=None if depth is None else min(depth, settings.consent_app_crawl_max_depth_ceiling),
        max_urls=max_urls,
        concurrency=concurrency,
        per_domain_concurrency=per_domain,
        requests_per_second=float(pick("requests_per_second", settings.consent_app_default_rps, settings.consent_app_rps_ceiling)),
        memory_budget_mb=min(int(r.get("memory_budget_mb") or 0), settings.consent_app_memory_budget_ceiling_mb),
    )


def controls_from_options(options: dict | None, settings: Settings) -> CrawlControls | None:
    """The controls stored on a scan, re-clamped on the way out (a tampered row cannot widen them), or None for a scan that has none (every web scan)."""
    block = options.get("crawl") if isinstance(options, dict) else None
    if not isinstance(block, dict):
        return None
    clean = {k: block.get(k) for k in ("max_depth", "max_urls", "concurrency", "per_domain_concurrency", "requests_per_second", "memory_budget_mb")}
    for k, v in clean.items():
        if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0):
            clean[k] = None
    return build_controls(clean, settings)


class DomainRateLimiter:
    """Spaces navigation starts at least 1/rps apart. Waiters queue in order, so a burst cannot jump the line. `sleep` and `clock` are injectable."""

    def __init__(self, requests_per_second: float, *, clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        if requests_per_second <= 0:
            raise ValueError("requests_per_second must be positive")
        self._interval = 1.0 / requests_per_second
        self._clock, self._sleep = clock, sleep
        self._next = 0.0
        self._lock = asyncio.Lock()
        self.waits = 0

    async def acquire(self) -> None:
        async with self._lock:
            now = self._clock()
            wait = self._next - now
            self._next = max(now, self._next) + self._interval
        if wait > 0:
            self.waits += 1
            await self._sleep(wait)


def _process_memory_mb() -> float | None:
    """Resident memory of this process and its children (the browsers), in MB, or None when psutil is not installed."""
    try:
        import psutil
    except ImportError:
        return None
    try:
        me = psutil.Process()
        total = me.memory_info().rss
        for child in me.children(recursive=True):
            try:
                total += child.memory_info().rss
            except psutil.Error:
                continue
        return total / (1024 * 1024)
    except psutil.Error:
        return None


class ResourceGuard:
    """Says whether another page may be launched. Off (budget 0) it never blocks. With a budget it blocks while memory is over it, and records each
    change of state as an event so the scan can say it was throttled. When memory cannot be measured it says so once and does not block: a control
    that cannot run is reported as not enforced, never silently skipped."""

    def __init__(self, budget_mb: int, *, reader: Callable[[], float | None] = _process_memory_mb, clock: Callable[[], float] = time.monotonic) -> None:
        self._budget = budget_mb
        self._read = reader
        self._clock = clock
        self.events: list[dict[str, Any]] = []
        self._throttled = False
        self._unmeasurable_noted = False
        self.peak_mb = 0.0

    @property
    def is_throttled(self) -> bool:
        return self._throttled

    def over_budget(self) -> bool:
        if self._budget <= 0:
            return False
        mb = self._read()
        if mb is None:
            if not self._unmeasurable_noted:
                self._unmeasurable_noted = True
                self.events.append({"event": "memory_budget_not_enforced", "reason": "memory could not be measured (psutil unavailable)"})
            return False
        self.peak_mb = max(self.peak_mb, mb)
        over = mb > self._budget
        if over and not self._throttled:
            self.events.append({"event": "throttled", "memory_mb": round(mb), "budget_mb": self._budget})
        elif not over and self._throttled:
            self.events.append({"event": "resumed", "memory_mb": round(mb), "budget_mb": self._budget})
        self._throttled = over
        return over
