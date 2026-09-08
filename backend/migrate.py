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
    python migrate.py --status                 # show which migrations are/aren't applied, no changes

Uses asyncpg directly (not SQLAlchemy's text()) because these files are genuine
multi-statement SQL scripts (multiple CREATE/ALTER/DO blocks per file) -- SQLAlchemy's
async driver rejects "cannot insert multiple commands into a prepared statement" for
that, a real error hit and worked around earlier in this project's own history.
"""

import argparse
import asyncio
import sys
from pathlib import Path

import asyncpg
from dotenv import dotenv_values

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

_CREATE_TRACKING_TABLE = """
create table if not exists schema_migrations (
    version     text primary key,
    applied_at  timestamptz not null default now()
);
"""


def _load_database_url(override: str | None) -> str:
    if override:
        return override
    env_path = Path(__file__).parent / ".env"
    values = dotenv_values(env_path)
    database_url = values.get("DATABASE_URL")
    if not database_url:
        print("ERROR: DATABASE_URL not found in .env and no --database-url given.", file=sys.stderr)
        sys.exit(1)
    # asyncpg wants a plain postgresql:// URL, not SQLAlchemy's +asyncpg driver qualifier.
    return database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


def _discover_migrations() -> list[Path]:
    return sorted(MIGRATIONS_DIR.glob("*.sql"), key=lambda p: p.stem)


async def _applied_versions(conn: asyncpg.Connection) -> set[str]:
    rows = await conn.fetch("select version from schema_migrations")
    return {r["version"] for r in rows}


async def run(database_url: str, *, search_path: str | None, status_only: bool) -> None:
    conn = await asyncpg.connect(database_url, timeout=15)
    try:
        if search_path:
            # Quoted so a test schema name can't be used for SQL injection via this
            # CLI flag; identifiers here are always developer-supplied, not user input.
            await conn.execute(f'SET search_path TO "{search_path}", public')
            print(f"search_path set to: {search_path}, public")

        await conn.execute(_CREATE_TRACKING_TABLE)
        applied = await _applied_versions(conn)
        migrations = _discover_migrations()

        if not migrations:
            print(f"No .sql files found in {MIGRATIONS_DIR}")
            return

        pending = [m for m in migrations if m.stem not in applied]

        if status_only:
            print(f"{'VERSION':45s} STATUS")
            for m in migrations:
                print(f"{m.stem:45s} {'applied' if m.stem in applied else 'PENDING'}")
            return

        if not pending:
            print(f"Nothing to do -- all {len(migrations)} migrations already applied.")
            return

        print(f"Applying {len(pending)} pending migration(s) (of {len(migrations)} total)...")
        for path in pending:
            sql = path.read_text(encoding="utf-8")
            print(f"  -> {path.stem} ... ", end="", flush=True)
            async with conn.transaction():
                await conn.execute(sql)
                await conn.execute(
                    "insert into schema_migrations (version) values ($1) on conflict (version) do nothing",
                    path.stem,
                )
            print("done")

        print(f"All {len(pending)} pending migration(s) applied successfully.")
    finally:
        await conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--database-url", default=None, help="Override DATABASE_URL from .env")
    parser.add_argument(
        "--search-path", default=None,
        help="Run migrations into this schema instead of the connection's default "
             "(schema must already exist; not created here). For fresh-schema testing.",
    )
    parser.add_argument("--status", action="store_true", help="Show applied/pending migrations, make no changes")
    args = parser.parse_args()

    database_url = _load_database_url(args.database_url)
    asyncio.run(run(database_url, search_path=args.search_path, status_only=args.status))


if __name__ == "__main__":
    main()
