"""Standalone entrypoint for running exactly one scan in its own process.

Why this exists: on Windows, Playwright's subprocess-based browser launching requires
asyncio's ProactorEventLoop, while psycopg's async mode (used by the LangGraph
checkpointer — see agents/consent_agent/graph.py) requires SelectorEventLoop. Both are
needed in the same worker process (jobs/worker.py handles both scan and analyze jobs),
and a single process can't run two event loop policies at once. The fix: the main
process runs under SelectorEventLoop (fixing the checkpointer), and the scanner runs
here, in a freshly spawned child process that gets Python's *default* event loop
(ProactorEventLoop) since it never touches the parent's policy. See
scanner/crawler.py's `run_scan_isolated()`, which launches this via plain (non-asyncio)
subprocess.run — asyncio subprocess creation is itself unavailable under
SelectorEventLoop on Windows, which is exactly why this can't be spawned with
`asyncio.create_subprocess_exec` from the parent.

Not needed on non-Windows platforms, where this conflict doesn't exist — see
`run_scan_isolated()`'s platform check.

Usage: python -m app.scanner.run_single_scan <url> [web|application]
Prints the ScanResult as JSON to stdout on success. On failure, prints the error to
stderr and exits non-zero.
"""

import asyncio
import os
import sys

from app.scanner.crawler import CANCEL_FILE_ENV, run_scan
from app.scanner.scan_profiles import PROFILE_NAMES, resolve_profile

# Real scanned websites routinely contain non-ASCII Unicode (arrows, em/en dashes,
# curly quotes, non-Latin scripts) in page titles/form labels/policy link text --
# confirmed live: a real scan of prepmyevent.com crashed here with
# `UnicodeEncodeError: 'charmap' codec can't encode character '→'`, because on
# Windows, sys.stdout without this reconfiguration encodes using the console's
# default ANSI codepage (e.g. cp1252), not UTF-8, unless the OS has the "use Unicode
# UTF-8 worldwide language support" beta option enabled (most installs don't).
# reconfigure() is a no-op where stdout is already UTF-8 (Linux/macOS), so this is
# safe everywhere, not just Windows. Must run before anything else touches stdout/stderr.
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")


async def _main(url: str, profile_name: str | None = None) -> None:
    # The profile NAME is the only thing that arrives on the command line; its page limit and
    # deadline come from this process's own validated settings, never from argv.
    profile = resolve_profile(profile_name) if profile_name else None
    cancel_file = os.environ.get(CANCEL_FILE_ENV)

    async def _should_stop() -> bool:
        # This process has no database access; the parent signals a cancel by creating a file.
        return bool(cancel_file) and os.path.exists(cancel_file)

    kwargs: dict = {}
    if profile is not None:
        kwargs["profile"] = profile
    if cancel_file:
        kwargs["should_stop"] = _should_stop
    result = await run_scan(url, **kwargs)
    sys.stdout.write(result.model_dump_json())


if __name__ == "__main__":
    if len(sys.argv) not in (2, 3) or (len(sys.argv) == 3 and sys.argv[2] not in PROFILE_NAMES):
        print("usage: python -m app.scanner.run_single_scan <url> [web|application]", file=sys.stderr)
        sys.exit(2)
    try:
        asyncio.run(_main(sys.argv[1], sys.argv[2] if len(sys.argv) == 3 else None))
    except Exception as exc:  # noqa: BLE001 — surfaced to the parent process via stderr
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
