"""Is the connection this API uses actually subject to row-level security?

Migration 0018 forces RLS on 61 tables and creates a least-privilege role to connect
as. None of that does anything if the application connects as a superuser, and for a
while it did: a live audit found the API on `postgres`, which carries `rolsuper` and
`rolbypassrls` and owns every table, so all 61 forced policies were decoration and
tenant isolation rested entirely on application-level org_id filtering.

Nothing failed. Nothing logged. The policies were simply never consulted.

THREE WAYS TO ESCAPE RLS, ALL CHECKED
-------------------------------------
    rolsuper      -- superusers bypass RLS unconditionally
    rolbypassrls  -- the explicit grant to ignore it
    table owner   -- owners bypass their own policies unless FORCE is set

The third is the subtle one. FORCE ROW LEVEL SECURITY closes it, and 0018 sets that
on every table with a policy -- but an owner connection on a table that gained a
policy later, without FORCE, would silently read everything. So ownership is reported
rather than assumed harmless.

WHY THIS REFUSES TO BOOT IN PRODUCTION AND ONLY WARNS ELSEWHERE
---------------------------------------------------------------
A developer pointing at the owner role to run migrations or inspect data is doing
something ordinary. A production API on that role is a cross-tenant data leak waiting
for one missing `where org_id =`. So the same finding is a warning in development and
a refusal to start in production -- and either way it is stated at boot, which is the
thing that was missing before.
"""

from __future__ import annotations

import logging
import os

from sqlalchemy import text

from app.config import Settings, get_settings
from app.db.session import engine

logger = logging.getLogger(__name__)

_QUERY = text("""
    select
      current_user as role_name,
      (select rolsuper     from pg_roles where rolname = current_user) as is_superuser,
      (select rolbypassrls from pg_roles where rolname = current_user) as bypasses_rls,
      (select count(*) from pg_tables
        where schemaname = 'public' and tableowner = current_user) as tables_owned,
      (select count(*) from pg_class c join pg_namespace n on n.oid = c.relnamespace
        where n.nspname = 'public' and c.relkind = 'r' and c.relforcerowsecurity)
        as tables_forcing_rls
""")


class InsecureDatabaseRoleError(RuntimeError):
    """The API is connected in a way that makes row-level security unenforceable."""


async def describe_connection_privileges() -> dict:
    """What the database says about the role we are actually connected as."""
    async with engine.connect() as conn:
        row = (await conn.execute(_QUERY)).mappings().one()
    return dict(row)


def rls_is_enforceable(privileges: dict) -> bool:
    """True only when none of the three escapes applies.

    Owning tables counts as an escape here even though FORCE closes it, because a
    table added later without FORCE would reopen it silently and this check exists
    precisely to catch what nothing else notices.
    """
    return not (
        privileges["is_superuser"]
        or privileges["bypasses_rls"]
        or privileges["tables_owned"] > 0
    )


async def assert_rls_is_enforceable(settings: Settings | None = None) -> dict:
    """Report the connection's privileges at boot; refuse to start in production.

    Returns the privilege dict so a caller (a health endpoint, a test) can show it.
    """
    settings = settings or get_settings()
    privileges = await describe_connection_privileges()

    if rls_is_enforceable(privileges):
        logger.info(
            "Database role %r is subject to row-level security "
            "(not superuser, no BYPASSRLS, owns no tables); %d tables force RLS.",
            privileges["role_name"], privileges["tables_forcing_rls"],
        )
        return privileges

    reasons = []
    if privileges["is_superuser"]:
        reasons.append("it is a SUPERUSER")
    if privileges["bypasses_rls"]:
        reasons.append("it has BYPASSRLS")
    if privileges["tables_owned"]:
        reasons.append(f"it OWNS {privileges['tables_owned']} tables")

    message = (
        f"Row-level security is NOT enforced for this API: the database role "
        f"{privileges['role_name']!r} bypasses it because {' and '.join(reasons)}. "
        f"{privileges['tables_forcing_rls']} tables force RLS and none of those policies "
        "will be consulted. Tenant isolation currently depends entirely on every query "
        "carrying its own org_id filter. Complete migration 0018's cutover: point "
        "DATABASE_URL at consiva_app (see backend/cutover_rls.py) and keep the "
        "privileged URL under WORKER_DATABASE_URL."
    )

    if settings.app_env == "production":
        # One deliberate escape hatch, and it has to be set on purpose.
        #
        # Without it, shipping this check would take down any production deployment
        # whose database cutover has not been done yet -- turning a latent security
        # gap into an immediate outage, which is not an improvement anybody asked
        # for. With it, an operator can deploy first and cut over second, and the log
        # says on every boot that they are running unprotected.
        #
        # It is spelled out in full rather than made a boolean, so nobody sets it by
        # copying a config file without reading it.
        if os.getenv("ALLOW_PRIVILEGED_DB_ROLE") == "i-accept-no-database-tenant-isolation":
            logger.error(
                "%s -- STARTING ANYWAY because ALLOW_PRIVILEGED_DB_ROLE is set. "
                "This deployment has NO database-level tenant isolation.", message,
            )
            return privileges
        raise InsecureDatabaseRoleError(message)

    logger.warning("%s (this would refuse to start in production)", message)
    return privileges
