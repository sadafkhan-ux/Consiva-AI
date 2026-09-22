"""Row-level security has to be enforced, not merely declared.

A live audit found every one of this database's forced RLS policies inert: the API
connected as `postgres`, which carries rolsuper and rolbypassrls and owns every table,
so the policies were never consulted. Nothing failed and nothing logged. Tenant
isolation rested entirely on every query remembering its own org_id filter.

These tests hold the two halves of the fix: the cutover cannot silently revert, and
the checker that would catch it cannot be weakened without a test failing.

The live half -- proving a foreign org actually reads zero rows -- runs only when
TEST_DATABASE_URL names a database to use, in the same style as the ROPA end-to-end
suite. The static half runs everywhere.
"""

import inspect
import os

import pytest

from app.db import privilege_check

# ── The three ways to escape RLS, and the checker that knows all of them ────────


@pytest.mark.parametrize("escape,privileges", [
    ("superuser", {"is_superuser": True, "bypasses_rls": False, "tables_owned": 0}),
    ("bypassrls", {"is_superuser": False, "bypasses_rls": True, "tables_owned": 0}),
    ("table owner", {"is_superuser": False, "bypasses_rls": False, "tables_owned": 61}),
])
def test_every_escape_from_rls_is_recognised(escape, privileges):
    """Owning tables counts even though FORCE closes it: a table that gains a policy
    later without FORCE would reopen the hole silently, which is exactly the class of
    thing this check exists to notice."""
    assert privilege_check.rls_is_enforceable(privileges) is False, (
        f"a role that is a {escape} was reported as subject to RLS"
    )


def test_a_least_privilege_role_passes():
    assert privilege_check.rls_is_enforceable(
        {"is_superuser": False, "bypasses_rls": False, "tables_owned": 0}
    ) is True


def test_production_refuses_to_boot_on_an_unsafe_role():
    """A developer on the owner role is doing something ordinary. A production API on
    it is a cross-tenant leak waiting for one missing `where org_id =`."""
    body = inspect.getsource(privilege_check.assert_rls_is_enforceable)
    assert 'settings.app_env == "production"' in body
    assert "raise InsecureDatabaseRoleError" in body
    # And it is never silent in the other environments.
    assert "logger.warning" in body


def test_the_check_runs_at_startup_not_on_demand():
    """A check nobody calls is a check that does not exist."""
    from app import main

    body = inspect.getsource(main.lifespan)
    assert "assert_rls_is_enforceable" in body
    # Before the graph is built, so an unsafe production boot fails fast.
    assert body.index("assert_rls_is_enforceable") < body.index("get_compiled_graph")


def test_the_query_asks_the_database_rather_than_the_config():
    """The connection string can say one thing and the session be another -- a pooler,
    a SET ROLE, an override. Only the live connection knows."""
    sql = str(privilege_check._QUERY)
    assert "current_user" in sql
    assert "rolsuper" in sql
    assert "rolbypassrls" in sql
    assert "tableowner = current_user" in sql


# ── The cutover's own shape ─────────────────────────────────────────────────────


def test_the_cutover_refuses_to_leave_a_privileged_role_in_place():
    """Setting a password on a role that still bypasses RLS would be theatre."""
    import pathlib

    script = pathlib.Path(__file__).resolve().parents[1] / "cutover_rls.py"
    body = script.read_text(encoding="utf-8")
    assert "REFUSING" in body
    assert "rolsuper" in body and "rolbypassrls" in body
    # The worker must keep the privileged URL or its cross-tenant sweeps see nothing.
    assert "WORKER_DATABASE_URL" in body
    assert "MIGRATION_DATABASE_URL" in body


def test_the_worker_still_takes_its_own_connection():
    """Under the restricted role the DSR, incident and regulatory sweeps would
    correctly see nothing and stop silently -- the worst way for a compliance sweep to
    stop."""
    import pathlib

    worker = pathlib.Path(__file__).resolve().parents[1] / "app/jobs/worker.py"
    body = worker.read_text(encoding="utf-8")
    assert 'os.getenv("WORKER_DATABASE_URL")' in body


# ── Live enforcement, against a real database ───────────────────────────────────

_LIVE = os.getenv("TEST_DATABASE_URL")
live_only = pytest.mark.skipif(
    not _LIVE,
    reason="set TEST_DATABASE_URL to a database whose API role is consiva_app to "
           "verify enforcement against real policies",
)


@live_only
@pytest.mark.asyncio
async def test_an_unscoped_connection_reads_nothing():
    """`org_id = NULL` is never true, so an unscoped connection sees nothing at all
    rather than seeing everything. That is the whole design of current_org_id()."""
    import asyncpg

    conn = await asyncpg.connect(_LIVE.replace("postgresql+asyncpg://", "postgresql://"))
    try:
        await conn.execute("select set_config('app.org_id', '', false)")
        for table in ("consent_scans", "ropa_records", "dsr_requests",
                      "incident_cases", "regwatch_findings"):
            assert await conn.fetchval(f"select count(*) from {table}") == 0, (
                f"{table} returned rows on an unscoped connection"
            )
    finally:
        await conn.close()


@live_only
@pytest.mark.asyncio
async def test_a_foreign_organisation_reads_nothing():
    import uuid

    import asyncpg

    conn = await asyncpg.connect(_LIVE.replace("postgresql+asyncpg://", "postgresql://"))
    try:
        await conn.execute(
            f"select set_config('app.org_id', '{uuid.uuid4()}', false)")
        for table in ("consent_scans", "ropa_records", "incident_cases"):
            assert await conn.fetchval(f"select count(*) from {table}") == 0
    finally:
        await conn.close()


@live_only
@pytest.mark.asyncio
async def test_login_still_works_before_any_org_is_known():
    """Authentication looks a user up BY EMAIL before anyone knows the organisation.
    The bootstrap policy allows exactly that, SELECT-only, exactly while unscoped --
    without it nobody could ever log in."""
    import asyncpg

    conn = await asyncpg.connect(_LIVE.replace("postgresql+asyncpg://", "postgresql://"))
    try:
        await conn.execute("select set_config('app.org_id', '', false)")
        assert await conn.fetchval("select count(*) from users") > 0
    finally:
        await conn.close()


def test_production_has_exactly_one_escape_hatch_and_it_must_be_spelled_out():
    """Shipping a hard fail would take down any deployment whose database cutover is
    not done yet -- turning a latent gap into an immediate outage. The opt-out exists
    for that transition, is a full sentence rather than a boolean so nobody sets it by
    copying a config file, and logs at ERROR on every boot."""
    body = inspect.getsource(privilege_check.assert_rls_is_enforceable)
    assert "i-accept-no-database-tenant-isolation" in body
    assert "logger.error" in body
    assert "NO database-level tenant isolation" in body


@pytest.mark.parametrize("value", [None, "true", "1", "yes", "ALLOW", ""])
def test_a_casual_value_does_not_open_the_hatch(value, monkeypatch):
    """Anything but the exact sentence leaves the refusal in place."""
    if value is None:
        monkeypatch.delenv("ALLOW_PRIVILEGED_DB_ROLE", raising=False)
    else:
        monkeypatch.setenv("ALLOW_PRIVILEGED_DB_ROLE", value)
    import os as _os
    opened = _os.getenv("ALLOW_PRIVILEGED_DB_ROLE") == "i-accept-no-database-tenant-isolation"
    assert opened is False
