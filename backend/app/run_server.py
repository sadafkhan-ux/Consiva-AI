"""Local dev server launcher — use this instead of `uvicorn app.main:app` directly on
Windows: `python -m app.run_server`.

Why: uvicorn's own `Server.run()` calls `asyncio.run(..., loop_factory=...)` with an
EXPLICIT loop factory it computes itself (uvicorn/loops/asyncio.py) — on Windows this
hardcodes `ProactorEventLoop` unless uvicorn's internal `use_subprocess` flag is set,
completely bypassing `asyncio.set_event_loop_policy()` (confirmed: setting the policy
before calling `uvicorn.run()` has no effect, since an explicit loop_factory always
wins over the ambient policy). The fix is to hand uvicorn our own loop factory instead
of fighting its default — see `_selector_event_loop_factory` below.

See scanner/run_single_scan.py for why SelectorEventLoop is needed at all (the
LangGraph checkpointer's psycopg vs. Playwright's subprocess launching disagree on
Windows event loop types; Playwright is isolated into its own child process instead,
so the main process is free to run under SelectorEventLoop).

`--reload` is intentionally not used here — combine with a real ASGI server / process
manager for anything beyond local development.
"""

import asyncio
import sys


def _selector_event_loop_factory() -> asyncio.AbstractEventLoop:
    return asyncio.SelectorEventLoop()


if __name__ == "__main__":
    import uvicorn

    loop_arg = "asyncio" if sys.platform != "win32" else "app.run_server:_selector_event_loop_factory"
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, loop=loop_arg)
