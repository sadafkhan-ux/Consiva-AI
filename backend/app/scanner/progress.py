"""Live progress of a crawl: how many pages are found, tried, scanned and failed so far.

A whole-site crawl takes minutes to hours. Until now the page counts existed only after it ended, so a long scan looked frozen. The crawler reports a small snapshot to a callback at most once
every `interval` seconds; the caller decides where it goes (the scan service writes it into the running `website_scan` stage). Reporting is strictly best-effort: a failing callback is logged
at debug level and never disturbs the crawl.

The clock is a parameter so the throttle is tested without waiting.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[dict], Awaitable[None]]
#: The counters a snapshot carries, named as the finished scan's metadata names them, so the same reader serves a running and a finished scan.
SNAPSHOT_KEYS = ("pages_discovered", "pages_attempted", "pages_scanned", "pages_failed", "pages_timeout")
DEFAULT_INTERVAL_SECONDS = 3.0


class ProgressReporter:
    def __init__(self, callback: ProgressCallback | None, *, interval: float = DEFAULT_INTERVAL_SECONDS, clock: Callable[[], float] = time.monotonic) -> None:
        self._callback, self._interval, self._clock = callback, interval, clock
        self._last: float | None = None

    @property
    def enabled(self) -> bool:
        return self._callback is not None

    async def tick(self, counters: dict, *, force: bool = False) -> bool:
        """Report `counters` unless one was reported less than `interval` seconds ago (or `force`). Returns whether a report was sent."""
        if self._callback is None:
            return False
        now = self._clock()
        if not force and self._last is not None and now - self._last < self._interval:
            return False
        self._last = now
        snapshot = {k: int(counters.get(k) or 0) for k in SNAPSHOT_KEYS}
        snapshot["live"] = True
        try:
            await self._callback(snapshot)
        except Exception:                                  # progress is a convenience: it must never break the crawl
            logger.debug("progress report failed", exc_info=True)
            return False
        return True
