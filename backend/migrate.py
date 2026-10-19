"""Authoritative migration runner for the Consent Agent database.

Applies every *.sql file in migrations/ (in filename order) against DATABASE_URL,
tracking what's already been applied in a schema_migrations table so re-running this
script is always safe -- it only executes files it hasn't recorded yet. Every
individual migration file is ALSO written to be safe to re-run on its own (IF NOT
EXISTS / ON CONFLICT / guarded CREATE POLICY), so this is defense in depth, not the
only thing making re-runs safe.

Usage:
    python migrate.py                          # apply against DATABASE_URL (.env)
    python migrate.py --database-url postgresql://...   # apply against a specific DB
    python migrate.py --search-path my_test_schema       # apply into a non-public schema
                                                          # (used for fresh-schema testing;
                                                          # the schema itself is NOT created
                                                          # here -- create it first)
    python migrate.py --status                 # show which migrations are/aren't applied;
                                               # READ-ONLY: no lock, no tracking table created
    python migrate.py --check                  # like --status, exit 2 if anything is pending
    python migrate.py --wait-for-db 120        # keep retrying the connection for up to 120 s
                                               # (the Docker deployment uses this)

Safety, in the order it matters (the Docker deployment runs this as a one-shot service that the
API, worker and frontend wait for -- see docker-compose.prod.yml and docs/DEPLOYMENT.md):

  * ONE RUNNER AT A TIME. A session-level Postgres advisory lock is taken before anything is
    read or written, and the applied set is read AFTER it is held, so two deployments racing on
    the same database serialise instead of both trying to apply the same file. The second one
    waits (--lock-timeout), then sees the first one's work and applies nothing.
  * EACH FILE IN ITS OWN TRANSACTION, recorded in the same transaction. A migration that fails is
    rolled back whole and is NOT recorded; earlier ones stay applied. Nothing is ever marked
    applied that did not complete.
  * NOTHING IS APPLIED UNTIL EVERYTHING PENDING HAS BEEN CHECKED. Before the first file runs it
    refuses (exit 3, with the reason and the way out) if it finds: duplicate or malformed
    migration numbers; a pending migration numbered BELOW one already applied (out of order); an
    existing application schema with an EMPTY history table (a database someone built by hand --
    it is never auto-baselined); or a statement Postgres cannot run inside a transaction.
  * A DDL LOCK TIMEOUT per migration, so a migration queued behind a long-running query on a
    table fails clearly instead of hanging the deployment forever.
  * IT NEVER DROPS, TRUNCATES OR RECREATES ANYTHING ITSELF.

Exit codes:  0 ok / nothing to do   1 a migration failed (rolled back, not recorded)
             2 --check: migrations pending   3 refused to run (unsafe state; see message)
             4 database unreachable          5 timed out waiting for the migration lock

Uses asyncpg directly (not SQLAlchemy's text()) because these files are genuine
multi-statement SQL scripts (multiple CREATE/ALTER/DO blocks per file) -- SQLAlchemy's
async driver rejects "cannot insert multiple commands into a prepared statement" for
that, a real error hit and worked around earlier in this project's own history.
"""

import argparse
import asyncio
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import asyncpg
from dotenv import dotenv_values

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_PENDING = 2
EXIT_REFUSED = 3
EXIT_DB_UNREACHABLE = 4
EXIT_LOCK_TIMEOUT = 5

#: Fixed key for the advisory lock. Any constant bigint works; it only has to be the same for
#: every runner that targets the same database, and unlikely to collide with another use.
ADVISORY_LOCK_KEY = 7_204_190_558_313_421

#: A table that exists in every real deployment (migration 0001). If it is present while the
#: history table is empty, the database was built some other way and must be baselined by a person.
_SENTINEL_TABLE = "consent_scans"

_NAME_RE = re.compile(r"^(\d{4})_[A-Za-z0-9_]+$")
NO_TRANSACTION_MARKER = "-- migrate:no-transaction"

_CREATE_TRACKING_TABLE = """
create table if not exists schema_migrations (
    version     text primary key,
    applied_at  timestamptz not null default now()
);
"""

# Statements Postgres refuses to run inside a transaction block. A file containing one cannot be
# applied by the per-file transaction below, and finding that out halfway through a deployment
# (after earlier files already ran) is exactly what the pre-flight check exists to prevent.
_NON_TRANSACTIONAL = re.compile(
    r"\b(?:create\s+(?:unique\s+)?index\s+concurrently|drop\s+index\s+concurrently|"
    r"reindex\b[^;]*\bconcurrently|vacuum\b|alter\s+system\b|create\s+database\b|"
    r"drop\s+database\b|create\s+tablespace\b|drop\s+tablespace\b|"
    r"alter\s+type\b[^;]*\badd\s+value\b)",
    re.IGNORECASE,
)


# ── Configuration ────────────────────────────────────────────────────────────────

def _load_database_url(override: str | None) -> str:
    """Resolve the target database, in precedence order: --database-url, then the
    process environment, then a local .env file.

    The environment has to be checked BEFORE the file. In Docker this script only
    ever sees DATABASE_URL as an environment variable -- compose sets it (see
    docker-compose.prod.yml's `environment:` block) and .dockerignore keeps .env
    out of the image entirely -- so a file-only lookup made
    `docker compose run --rm backend python migrate.py` impossible, which is the
    exact command that compose file's own header documents.
    """
    database_url = override or os.getenv("DATABASE_URL")

    if not database_url:
        values = dotenv_values(Path(__file__).parent / ".env")
        database_url = values.get("DATABASE_URL")

    if not database_url:
        print(
            "ERROR: no database URL. Pass --database-url, or set DATABASE_URL in the "
            "environment or in backend/.env.",
            file=sys.stderr,
        )
        sys.exit(EXIT_REFUSED)

    # asyncpg wants a plain postgresql:// URL, not SQLAlchemy's +asyncpg driver
    # qualifier. Applied to every source, including --database-url: callers
    # naturally pass the same value the app uses, which carries the qualifier.
    return database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


def describe_target(database_url: str) -> str:
    """host/port/database/user -- everything an operator needs to confirm WHICH database this
    is, and never the password."""
    parsed = urlparse(database_url)
    return (
        f"host={parsed.hostname} port={parsed.port or 5432} "
        f"database={(parsed.path or '/').lstrip('/')} user={parsed.username}"
    )


# ── Planning (pure: no database, no I/O -- unit-tested directly) ─────────────────

def number_of(version: str) -> int | None:
    match = _NAME_RE.match(version)
    return int(match.group(1)) if match else None


def discover_migrations(directory: Path | None = None) -> list[Path]:
    return sorted((directory or MIGRATIONS_DIR).glob("*.sql"), key=lambda p: p.stem)


def validate_files(versions: list[str]) -> list[str]:
    """Problems with the migration FILES themselves (not with the database)."""
    problems: list[str] = []
    seen: dict[int, str] = {}
    for version in versions:
        number = number_of(version)
        if number is None:
            problems.append(
                f"{version}.sql does not match the NNNN_name.sql convention (four-digit number, "
                "underscore, letters/digits/underscores)"
            )
            continue
        if number in seen:
            problems.append(
                f"two migrations share number {number:04d}: {seen[number]} and {version}. "
                "Renumber one so the order is unambiguous"
            )
        seen[number] = version
    return problems


@dataclass
class Plan:
    pending: list[str] = field(default_factory=list)
    #: Pending migrations numbered BELOW one that is already applied.
    out_of_order: list[str] = field(default_factory=list)
    #: Versions the database records as applied that this checkout does not have. Not an error --
    #: it is what a rollback to an older image looks like -- but worth saying.
    unknown_applied: list[str] = field(default_factory=list)


def make_plan(on_disk: list[str], applied: set[str]) -> Plan:
    pending = [v for v in on_disk if v not in applied]
    known_applied_numbers = [n for v in on_disk if v in applied and (n := number_of(v)) is not None]
    highest = max(known_applied_numbers, default=None)
    out_of_order = [
        v for v in pending
        if highest is not None and (number_of(v) or 0) < highest
    ]
    return Plan(
        pending=pending,
        out_of_order=out_of_order,
        unknown_applied=sorted(v for v in applied if v not in set(on_disk)),
    )


def _strip_sql_comments(sql: str) -> str:
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    return re.sub(r"--[^\n]*", " ", sql)


def non_transactional_statements(sql: str) -> list[str]:
    """Statements in `sql` that cannot run inside a transaction block. Comments are ignored, so
    a file that merely MENTIONS "CREATE INDEX CONCURRENTLY" in a note (0017 does) is fine."""
    return [" ".join(m.group(0).split()[:3]).upper()
            for m in _NON_TRANSACTIONAL.finditer(_strip_sql_comments(sql))]


def wants_no_transaction(sql: str) -> bool:
    """An explicit, reviewable opt-out: the marker must be among the file's first lines."""
    return any(line.strip().lower() == NO_TRANSACTION_MARKER for line in sql.splitlines()[:15])


# ── Database operations ──────────────────────────────────────────────────────────

# Rejections that are about WHAT we asked for, not about the server being busy or starting.
# (asyncpg.CannotConnectNowError -- "the database system is starting up" -- is deliberately NOT here:
# that one is exactly what the retry loop exists to wait out.)
_PERMANENT_CONNECT_ERRORS = (
    asyncpg.InvalidPasswordError,
    asyncpg.InvalidCatalogNameError,
    asyncpg.InvalidAuthorizationSpecificationError,
)


async def connect_with_retry(database_url: str, wait_seconds: float) -> asyncpg.Connection:
    """Connect, retrying for up to `wait_seconds` (0 = a single attempt). Postgres being
    "started" is not the same as "accepting connections" -- the container can be up while the
    server is still recovering -- so the deployment waits here instead of failing the first
    time and relying on the orchestrator to retry."""
    deadline = time.monotonic() + max(wait_seconds, 0)
    attempt = 0
    while True:
        attempt += 1
        try:
            return await asyncpg.connect(database_url, timeout=15)
        except _PERMANENT_CONNECT_ERRORS as exc:
            # A wrong password or a database that does not exist will not fix itself in two
            # minutes. Retrying only delays the real message and, worse, makes a mis-pointed
            # deployment look like a slow database. Fail now, say which kind of rejection it was.
            raise ConnectionError(
                f"the database server rejected the connection ({type(exc).__name__}) for "
                f"{describe_target(database_url)} -- check the credentials and database name; "
                "this is a configuration error, not a startup delay"
            ) from None
        except (OSError, asyncpg.PostgresError, asyncio.TimeoutError) as exc:
            if time.monotonic() >= deadline:
                raise ConnectionError(
                    f"could not connect to the database ({describe_target(database_url)}) "
                    f"after {attempt} attempt(s): {type(exc).__name__}"
                ) from None
            if attempt == 1 or attempt % 5 == 0:
                print(f"  database not ready yet ({type(exc).__name__}); retrying...", flush=True)
            await asyncio.sleep(2)


async def acquire_lock(conn: asyncpg.Connection, timeout_seconds: float) -> bool:
    """Take the migration advisory lock, waiting up to `timeout_seconds`. Session-level, so it is
    released automatically if this process dies -- a crashed runner can never wedge deployments."""
    deadline = time.monotonic() + max(timeout_seconds, 0)
    announced = False
    while True:
        if await conn.fetchval("select pg_try_advisory_lock($1)", ADVISORY_LOCK_KEY):
            return True
        if time.monotonic() >= deadline:
            return False
        if not announced:
            print("  another migration run holds the lock; waiting for it to finish...", flush=True)
            announced = True
        await asyncio.sleep(1)


async def _tracking_table_exists(conn: asyncpg.Connection) -> bool:
    return await conn.fetchval("select to_regclass('schema_migrations') is not null")


async def _applied_versions(conn: asyncpg.Connection) -> set[str]:
    rows = await conn.fetch("select version from schema_migrations")
    return {r["version"] for r in rows}


async def _application_schema_exists(conn: asyncpg.Connection) -> bool:
    return await conn.fetchval("select to_regclass($1) is not null", _SENTINEL_TABLE)


def _print_status(migrations: list[Path], applied: set[str]) -> None:
    print(f"{'VERSION':45s} STATUS")
    for m in migrations:
        print(f"{m.stem:45s} {'applied' if m.stem in applied else 'PENDING'}")


def _refuse(message: str, remedy: str) -> int:
    print(f"REFUSING TO RUN: {message}", file=sys.stderr)
    print(f"  What to do: {remedy}", file=sys.stderr)
    return EXIT_REFUSED


async def run(
    database_url: str,
    *,
    search_path: str | None = None,
    status_only: bool = False,
    check_only: bool = False,
    wait_for_db: float = 0,
    lock_timeout: float = 300,
    ddl_lock_timeout: float = 60,
    allow_out_of_order: bool = False,
    baseline_through: str | None = None,
    migrations_dir: Path | None = None,
) -> int:
    """Returns the process exit code (see the module docstring)."""
    print(f"Target: {describe_target(database_url)}", flush=True)
    try:
        conn = await connect_with_retry(database_url, wait_for_db)
    except ConnectionError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_DB_UNREACHABLE

    try:
        if search_path:
            # Quoted so a test schema name can't be used for SQL injection via this
            # CLI flag; identifiers here are always developer-supplied, not user input.
            await conn.execute(f'SET search_path TO "{search_path}", public')
            print(f"search_path set to: {search_path}, public")

        migrations = discover_migrations(migrations_dir)
        if not migrations:
            print(f"No .sql files found in {migrations_dir or MIGRATIONS_DIR}")
            return EXIT_OK
        on_disk = [m.stem for m in migrations]
        paths = {m.stem: m for m in migrations}

        # ── Read-only paths: no lock, nothing created, nothing written ──────────
        if status_only or check_only:
            applied = await _applied_versions(conn) if await _tracking_table_exists(conn) else set()
            plan = make_plan(on_disk, applied)
            _print_status(migrations, applied)
            for version in plan.unknown_applied:
                print(f"note: {version} is recorded as applied but is not in this checkout")
            return EXIT_PENDING if (check_only and plan.pending) else EXIT_OK

        problems = validate_files(on_disk)
        if problems:
            return _refuse("; ".join(problems), "fix the migration files in this checkout and redeploy")

        # ── One runner at a time ────────────────────────────────────────────────
        if not await acquire_lock(conn, lock_timeout):
            print(
                f"ERROR: timed out after {lock_timeout:.0f}s waiting for another migration run "
                "to finish. If none is actually running, check pg_locks for a stale session "
                "holding advisory lock %d." % ADVISORY_LOCK_KEY, file=sys.stderr,
            )
            return EXIT_LOCK_TIMEOUT

        await conn.execute(_CREATE_TRACKING_TABLE)
        # Read AFTER the lock is held: a runner that waited must see what the other one did.
        applied = await _applied_versions(conn)

        if baseline_through is not None:
            return await _baseline(conn, on_disk, applied, baseline_through)

        if not applied and await _application_schema_exists(conn):
            return _refuse(
                f"this database already has the application schema (table {_SENTINEL_TABLE!r} "
                "exists) but its migration history is EMPTY, so every migration would be treated "
                "as pending and re-run against live tables.",
                "if the schema really was built from these migrations, verify it matches and record "
                "that explicitly:  python migrate.py --baseline-through <last migration it contains> "
                "(e.g. 0033_ropa_evidence_analysis_job_type). Nothing is baselined automatically.",
            )

        plan = make_plan(on_disk, applied)
        for version in plan.unknown_applied:
            print(f"note: {version} is recorded as applied but is not in this checkout "
                  "(expected after rolling back to an older image)")

        if plan.out_of_order and not allow_out_of_order:
            return _refuse(
                f"pending migration(s) {', '.join(plan.out_of_order)} are numbered BELOW a migration "
                "that is already applied. Applying them now runs them against a schema they were "
                "never written for.",
                "review them; if they are safe to apply late, re-run with --allow-out-of-order. "
                "Otherwise renumber them above the highest applied migration.",
            )

        if not plan.pending:
            print(f"Nothing to do -- all {len(migrations)} migrations already applied.")
            return EXIT_OK

        # Pre-flight EVERY pending file before running ANY of them.
        contents: dict[str, str] = {}
        for version in plan.pending:
            text = paths[version].read_text(encoding="utf-8")
            contents[version] = text
            bad = non_transactional_statements(text)
            if bad and not wants_no_transaction(text):
                return _refuse(
                    f"{version} contains a statement Postgres cannot run inside a transaction "
                    f"({', '.join(sorted(set(bad)))}), and every migration runs in one so a failure "
                    "rolls back cleanly.",
                    f"split that statement into its own migration and mark it with the first-lines "
                    f"comment '{NO_TRANSACTION_MARKER}' after reviewing that it is safe to re-run "
                    "if it is interrupted.",
                )

        print(f"Applying {len(plan.pending)} pending migration(s) (of {len(migrations)} total)...",
              flush=True)
        for version in plan.pending:
            text = contents[version]
            print(f"  -> {version} ... ", end="", flush=True)
            started = time.monotonic()
            try:
                if wants_no_transaction(text):
                    # Not atomic, by explicit opt-in: recorded only AFTER it succeeds, so a failure
                    # leaves it unrecorded and it will be retried (it must be written to allow that).
                    await conn.execute(f"set lock_timeout = '{int(ddl_lock_timeout * 1000)}ms'")
                    await conn.execute(text)
                    await conn.execute(
                        "insert into schema_migrations (version) values ($1) "
                        "on conflict (version) do nothing", version,
                    )
                else:
                    async with conn.transaction():
                        await conn.execute(f"set local lock_timeout = '{int(ddl_lock_timeout * 1000)}ms'")
                        await conn.execute(text)
                        await conn.execute(
                            "insert into schema_migrations (version) values ($1) "
                            "on conflict (version) do nothing", version,
                        )
            except Exception as exc:  # noqa: BLE001 -- any failure must stop the deployment
                print("FAILED", flush=True)
                print(
                    f"\nERROR: migration {version} failed and was rolled back; it is NOT recorded "
                    f"as applied. {type(exc).__name__}: {exc}\n"
                    "Migrations before it in this run stay applied. Fix the migration (or the data "
                    "it tripped on) and deploy again -- the runner resumes from this file.",
                    file=sys.stderr,
                )
                return EXIT_FAILED
            print(f"done ({time.monotonic() - started:.1f}s)")

        print(f"All {len(plan.pending)} pending migration(s) applied successfully.")
        return EXIT_OK
    finally:
        await conn.close()  # also releases the session-level advisory lock


async def _baseline(conn: asyncpg.Connection, on_disk: list[str], applied: set[str], through: str) -> int:
    """Record migrations as applied WITHOUT running them -- for a database someone built by hand.
    Deliberate and explicit: it only ever happens when asked for by name, and only inserts rows."""
    if through not in on_disk:
        return _refuse(f"--baseline-through {through!r} is not a migration in this checkout",
                       "pass one of the version names printed by --status")
    to_record = [v for v in on_disk if v <= through and v not in applied]
    if not to_record:
        print(f"Nothing to baseline -- everything up to {through} is already recorded.")
        return EXIT_OK
    async with conn.transaction():
        for version in to_record:
            await conn.execute(
                "insert into schema_migrations (version) values ($1) on conflict (version) do nothing",
                version,
            )
    print(f"Baselined {len(to_record)} migration(s) as applied WITHOUT running them "
          f"({to_record[0]} .. {to_record[-1]}). Pending migrations will run on the next normal run.")
    return EXIT_OK


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--database-url", default=None, help="Override DATABASE_URL from .env")
    parser.add_argument(
        "--search-path", default=None,
        help="Run migrations into this schema instead of the connection's default "
             "(schema must already exist; not created here). For fresh-schema testing.",
    )
    parser.add_argument("--status", action="store_true",
                        help="Show applied/pending migrations. Read-only: no lock, nothing created.")
    parser.add_argument("--check", action="store_true",
                        help="Like --status, but exit 2 when migrations are pending (for CI/preflight).")
    parser.add_argument("--wait-for-db", type=float, default=_env_float("MIGRATE_WAIT_FOR_DB_SECONDS", 0),
                        help="Keep retrying the connection for this many seconds (default 0 = one attempt; "
                             "env MIGRATE_WAIT_FOR_DB_SECONDS).")
    parser.add_argument("--lock-timeout", type=float, default=_env_float("MIGRATE_LOCK_TIMEOUT_SECONDS", 300),
                        help="Seconds to wait for another migration run to finish (default 300).")
    parser.add_argument("--ddl-lock-timeout", type=float, default=_env_float("MIGRATE_DDL_LOCK_TIMEOUT_SECONDS", 60),
                        help="Per-migration Postgres lock_timeout in seconds: fail instead of hanging "
                             "behind a long-running query (default 60).")
    parser.add_argument("--allow-out-of-order", action="store_true",
                        help="Apply a pending migration numbered below one already applied. Review first.")
    parser.add_argument("--baseline-through", default=None, metavar="VERSION",
                        help="Record migrations up to VERSION as applied WITHOUT running them. For a "
                             "database built by hand whose history table is empty.")
    args = parser.parse_args()

    database_url = _load_database_url(args.database_url)
    code = asyncio.run(run(
        database_url,
        search_path=args.search_path, status_only=args.status, check_only=args.check,
        wait_for_db=args.wait_for_db, lock_timeout=args.lock_timeout,
        ddl_lock_timeout=args.ddl_lock_timeout, allow_out_of_order=args.allow_out_of_order,
        baseline_through=args.baseline_through,
    ))
    sys.exit(code)


if __name__ == "__main__":
    main()
