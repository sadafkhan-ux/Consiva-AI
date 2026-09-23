"""Complete the least-privilege cutover on a CI database, and prove it took.

Why CI does this at all, rather than testing RLS on the connection it already has:

Migrations run as the bootstrap role. That role OWNS every table and, on the standard
Postgres images, is also a superuser. Both bypass row-level security -- an owner unless
FORCE is set, a superuser unconditionally. So a policy check run on the migrating
connection reads the whole table and reports success no matter how broken the policies
are. Measured on a scratch database while writing this: scoped to organisation A, that
connection saw both organisations' rows.

That is exactly P0-1, the finding that sat in this codebase for weeks while 61 tables
of forced RLS were never once consulted. A pipeline that repeated the mistake would
certify the bug as fixed.

So CI runs the same cutover a deployment runs, then points the isolation tests at the
restricted role. The password is generated per run and written to GITHUB_ENV; it never
touches the repository.

Refuses if consiva_app turns out to be privileged after all -- a cutover onto a role
that still bypasses RLS is theatre, and silently proving nothing is the failure this
whole arrangement exists to prevent.
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import re
import secrets
import string
import sys

import asyncpg

ALPHABET = string.ascii_letters + string.digits


async def main() -> int:
    raw = os.getenv("DATABASE_URL")
    if not raw:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 1

    # A Postgres ROLE is cluster-wide, not per-database. Rotating consiva_app's
    # password here therefore changes it for every database in the cluster -- so
    # running this against a scratch database on a developer machine silently
    # invalidates the credential in backend/.env and the API starts failing to
    # connect on its next pool checkout. That is not hypothetical; it is exactly
    # what happened while rehearsing this pipeline locally, and the symptom
    # (`password authentication failed for user "consiva_app"`) appears minutes
    # later, nowhere near the cause.
    #
    # CI has a database per run and nothing else to break, so it is allowed. Anywhere
    # else this refuses and points at the script that does the job properly --
    # cutover_rls.py rotates the password AND rewrites .env to match.
    if not os.getenv("GITHUB_ACTIONS") and os.getenv("ALLOW_LOCAL_ROLE_ROTATION") != "yes":
        print(
            "REFUSING: this rotates a CLUSTER-WIDE role password and does not update "
            "backend/.env, so every other database on this server -- including your "
            "development one -- would stop authenticating. Use cutover_rls.py locally; "
            "it does both. Set ALLOW_LOCAL_ROLE_ROTATION=yes only if you are certain.",
            file=sys.stderr,
        )
        return 1

    password = "".join(secrets.choice(ALPHABET) for _ in range(32))
    owner_dsn = raw.replace("postgresql+asyncpg://", "postgresql://")

    conn = await asyncpg.connect(owner_dsn)
    try:
        await conn.execute(f"alter role consiva_app login password '{password}'")
        role = await conn.fetchrow(
            "select rolsuper, rolbypassrls from pg_roles where rolname = 'consiva_app'"
        )
        owned = await conn.fetchval(
            "select count(*) from pg_tables "
            "where schemaname = 'public' and tableowner = 'consiva_app'"
        )
    finally:
        await conn.close()

    if role["rolsuper"] or role["rolbypassrls"] or owned:
        print(
            f"REFUSING: consiva_app is not least-privilege (super={role['rolsuper']}, "
            f"bypassrls={role['rolbypassrls']}, owns={owned}). The isolation tests "
            "would pass without testing anything.",
            file=sys.stderr,
        )
        return 1

    restricted = re.sub(r"//[^@]+@", f"//consiva_app:{password}@", owner_dsn)

    _export(restricted)
    print("consiva_app ready: not a superuser, no BYPASSRLS, owns no tables")
    return 0


def _export(restricted: str) -> None:
    """Hand the restricted DSN to the following steps, without printing it.

    Synchronous and outside the coroutine: it is a one-line append with nothing for an
    event loop to interleave, and blocking file IO inside an async function is the kind
    of thing that is harmless here and a bug three refactors later.
    """
    github_env = os.getenv("GITHUB_ENV")
    if not github_env:
        # Running locally. Says what to do rather than echoing a password into shell
        # history.
        print("GITHUB_ENV not set; export RESTRICTED_DATABASE_URL yourself to use this")
        return
    with pathlib.Path(github_env).open("a", encoding="utf-8") as handle:
        handle.write(f"RESTRICTED_DATABASE_URL={restricted}\n")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
