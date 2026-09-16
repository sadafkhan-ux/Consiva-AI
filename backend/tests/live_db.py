"""Where the live-database tests get their DSN, and why it is not just backend/.env.

A handful of tests need a real database, because they check things no mock can answer:
whether RLS is actually forced, whether a foreign key really is violated by the wrong
insert order, whether the adapter SDK reads a live catalog correctly.

Each of them used to read DATABASE_URL straight out of backend/.env with
dotenv_values(). That file still holds the Supabase DSN this deployment left behind on
2026-09-09 -- docker-compose.prod.yml overrides DATABASE_URL for the containers and
nothing ever updated the file -- so these tests were quietly asserting against the
abandoned database rather than the running one. It is not a hypothetical:
test_rls_is_forced_on_every_tenant_table reported 21 unforced tables, and the deployed
database had RLS forced on all 53 of its own. A test that names the wrong database does
not just fail; it reports on something nobody is running.

migrate.py had exactly this bug and fixed it the same way -- environment first, file
second -- and its _load_database_url docstring carries the reasoning.

Two entry points, because these tests are not equally safe to aim at a deployment:

  read_only_dsn()  For tests that only SELECT, or that write inside a transaction they
                   roll back. Environment first, so in a container or in CI they
                   measure the database that is actually deployed.

  writable_dsn()   For tests that COMMIT rows. Deliberately NOT satisfied by
                   DATABASE_URL, however convenient that would be: those tests leave
                   real records behind, and DATABASE_URL is routinely the deployment.
                   Only TEST_DATABASE_URL counts -- a variable nobody sets by accident.
"""

import os
from pathlib import Path

from dotenv import dotenv_values

_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"

# conftest.py installs this with os.environ.setdefault so that importing app code
# validates without a database present. It is not a real DSN and must never reach a
# live test -- which it would, now that these lookups consult the environment at all.
# Defined here and imported by conftest, so the two cannot drift apart.
PLACEHOLDER_DSN = "postgresql+asyncpg://user:pass@localhost/test"


def _usable(dsn: str | None) -> str | None:
    return None if not dsn or dsn == PLACEHOLDER_DSN else dsn


def read_only_dsn() -> str | None:
    """TEST_DATABASE_URL, else DATABASE_URL, else backend/.env. None if none is real."""
    return (
        _usable(os.getenv("TEST_DATABASE_URL"))
        or _usable(os.getenv("DATABASE_URL"))
        or _usable(dotenv_values(_ENV_FILE).get("DATABASE_URL"))
    )


def writable_dsn() -> str | None:
    """TEST_DATABASE_URL only. None otherwise, so the test skips rather than committing
    into whatever DATABASE_URL happens to point at."""
    return _usable(os.getenv("TEST_DATABASE_URL"))


def as_sync(dsn: str) -> str:
    """asyncpg wants a plain postgresql:// URL, not SQLAlchemy's +asyncpg qualifier."""
    return dsn.replace("postgresql+asyncpg://", "postgresql://", 1)


READ_ONLY_SKIP_REASON = (
    "no real database: set TEST_DATABASE_URL or DATABASE_URL, or put one in backend/.env"
)
WRITABLE_SKIP_REASON = (
    "this test COMMITS rows, so it runs only against a database you name explicitly: "
    "set TEST_DATABASE_URL to a throwaway database (never the deployment)"
)
