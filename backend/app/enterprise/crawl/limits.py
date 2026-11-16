"""Resource limits of an Enterprise crawl: a memory budget and a runtime deadline.

Both exist so that one huge or hostile site cannot take the worker down. Neither hides anything: when a limit ends the crawl, the scan records WHY (`stop_reason` = `memory_limit` or
`time_limit`), the URLs it did not reach stay `pending` in the inventory, and the coverage declaration reports the scan as partial. A scan stopped this way can be resumed.

Memory is the resident memory of this process and of the browsers it started (Chromium runs as child processes, so the process alone would understate it). The clock, the memory reading
and the sleep are parameters so the behaviour is tested without waiting or allocating.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import psutil

POLL_SECONDS = 2.0
#: How long the crawl waits for memory to come back under the budget before it stops.
PATIENCE_SECONDS = 300.0

OK, PAUSE, MEMORY_LIMIT, TIME_LIMIT = None, "pause", "memory_limit", "time_limit"


def rss_mb() -> float:
    """Resident memory, in megabytes, of this process and every process it started."""
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
        return 0.0


class LimitGuard:
    def __init__(self, *, memory_budget_mb: int, max_runtime_seconds: int, clock: Callable[[], float] = time.monotonic,
                 rss: Callable[[], float] = rss_mb, patience: float = PATIENCE_SECONDS) -> None:
        self._budget, self._deadline, self._clock, self._rss, self._patience = memory_budget_mb, max_runtime_seconds, clock, rss, patience
        self._started = clock()
        self._over_since: float | None = None

    def check(self) -> str | None:
        """None: carry on. "pause": memory is over the budget, wait a moment and ask again. "memory_limit" / "time_limit": stop the crawl now and say why."""
        now = self._clock()
        if self._deadline and now - self._started >= self._deadline:
            return TIME_LIMIT
        if self._budget and self._rss() > self._budget:
            self._over_since = self._over_since if self._over_since is not None else now
            return MEMORY_LIMIT if now - self._over_since >= self._patience else PAUSE
        self._over_since = None
        return OK
