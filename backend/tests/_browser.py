"""Run a coroutine that drives Playwright on an event loop of its own, in a worker thread.

On Windows Playwright needs a loop that can start subprocesses (the Proactor loop). app/jobs/worker.py sets the Selector policy when it is imported, and once any earlier test has imported
it, a plain `async def` test can no longer launch Chromium. A loop created directly, in its own thread, is not affected by the policy.
"""

import asyncio
import sys
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from typing import TypeVar

T = TypeVar("T")


def run_with_browser(make_coro: Callable[[], Awaitable[T]]) -> T:
    def runner() -> T:
        loop = asyncio.ProactorEventLoop() if sys.platform == "win32" else asyncio.new_event_loop()
        try:
            return loop.run_until_complete(make_coro())
        finally:
            loop.close()
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(runner).result()
