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

import argparse
import asyncio
import os
import sys


def _selector_event_loop_factory() -> asyncio.AbstractEventLoop:
    return asyncio.SelectorEventLoop()


if __name__ == "__main__":
    import uvicorn

    # Host/port are configurable because the previously hardcoded 127.0.0.1:8000 meant
    # the only way to move the port on Windows was to bypass this launcher and call
    # `uvicorn` directly -- which immediately fails with "Psycopg cannot use the
    # 'ProactorEventLoop'", since bypassing it also bypasses the loop factory below.
    # Env vars as well as flags so a container or shell can set it without changing
    # the command.
    parser = argparse.ArgumentParser(description="Run the Consiva API (Windows-safe event loop).")
    parser.add_argument("--host", default=os.getenv("API_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("API_PORT", "8000")))
    parser.add_argument("--reload", action="store_true", help="dev autoreload")
    args = parser.parse_args()

    loop_arg = "asyncio" if sys.platform != "win32" else "app.run_server:_selector_event_loop_factory"
    uvicorn.run(
        "app.main:app", host=args.host, port=args.port, loop=loop_arg, reload=args.reload,
    )
