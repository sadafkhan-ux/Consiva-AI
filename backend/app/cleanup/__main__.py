"""Command line for the local scan-data clean-up.

    python -m app.cleanup                       # report what would be deleted (changes nothing)
    python -m app.cleanup --execute             # clear finished scans of both agents
    python -m app.cleanup --execute --history-dir /backups
    python -m app.cleanup --loop 300            # every 5 minutes, for ever (the `cleanup` compose service)

Exit status: 0 done and verified; 2 the target database was not verified (nothing was changed); 3 the integrity check failed; 1 any other error.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import uuid

from app.cleanup import core


def _print(report: core.Report) -> None:
    mode = "EXECUTED" if report.executed else "DRY RUN (nothing was changed)"
    print(f"\n== Scan-data clean-up: {mode} ==")
    print(f"target: database {report.target['database']} on {report.target['host']} ({report.target['server_address']}), PostgreSQL {report.target['version']}")
    if report.history_file:
        print(f"history written before clearing: {report.history_file}")
    for a in report.agents.values():
        rows = a.deleted if report.executed else a.would_delete
        print(f"\n-- {a.label}")
        print(f"   finished scans cleared: {a.scans_cleaned if report.executed else a.scans_eligible} of {a.scans_eligible} eligible; history rows kept: {a.history_retained} (unchanged: {a.history_unchanged})")
        for t, n in rows.items():
            if n:
                print(f"   {'deleted' if report.executed else 'would delete'} {n:>8}  {t}")
        for t, why in a.kept_because_referenced.items():
            print(f"   rows of {t} that {why} still point at are never deleted")
    i = report.integrity
    print("\n-- integrity")
    print(f"   structure unchanged: {i['structure_unchanged']}  (tables {i['structure']['tables']}, constraints {i['structure']['constraints']}, indexes {i['structure']['indexes']})")
    print(f"   other tables lost rows: {i['other_tables_lost_rows'] or 'none'}   invalid constraints: {i['invalid_constraints']}   history unchanged: {i['history_unchanged']}")
    print(f"   RESULT: {'OK' if i['ok'] else 'FAILED'}   ({report.seconds}s)")


async def _main(args) -> int:
    settings = core.Settings.from_env()
    if args.grace_minutes is not None:
        settings.grace_minutes = args.grace_minutes
    if args.respect_paused_reviews:
        settings.respect_paused_reviews = True
    agents = ("five_page", "enterprise") if args.agent == "all" else (args.agent,)
    if args.loop:
        await core.loop(args.loop, settings=settings, agents=agents, history_dir=args.history_dir)
        return 0
    try:
        report = await core.run(execute=args.execute, settings=settings, agents=agents, history_dir=args.history_dir, only_scan=uuid.UUID(args.scan_id) if args.scan_id else None)
    except core.TargetNotVerified as exc:
        print(f"STOPPED: the target database could not be verified, nothing was changed.\n  {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report.to_dict(), default=str, indent=2))
    else:
        _print(report)
    return 0 if report.integrity.get("ok") else 3


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m app.cleanup", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--execute", action="store_true", help="really delete (the default only reports)")
    p.add_argument("--agent", choices=("all", "five_page", "enterprise"), default="all")
    p.add_argument("--grace-minutes", type=int, default=None, help="how long after a scan finished before its data is cleared (default CLEANUP_GRACE_MINUTES, 1440 = 24 hours)")
    p.add_argument("--history-dir", default=None, help="write the scan history (CSV) here before clearing")
    p.add_argument("--loop", type=int, default=0, metavar="SECONDS", help="repeat every SECONDS for ever")
    p.add_argument("--scan-id", default=None, help="only this scan")
    p.add_argument("--respect-paused-reviews", action="store_true", help="do not clear a five-page scan whose analysis run is paused for a person's review")
    p.add_argument("--json", action="store_true")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        sys.exit(asyncio.run(_main(args)))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
