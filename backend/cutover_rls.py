"""Move the API onto the least-privilege role, completing migration 0018's cutover.

Migration 0018 built every part of tenant isolation except the last one: it created
`consiva_app` NOLOGIN, because a password does not belong in a migration or in git.
Until an operator finishes the cutover the API keeps connecting as a superuser, and a
superuser bypasses RLS unconditionally -- so all 61 forced policies are decoration.

WHAT THIS DOES, AND WHAT IT DELIBERATELY DOES NOT
-------------------------------------------------
Sets a generated password on consiva_app, points DATABASE_URL at it, and pins the
WORKER and MIGRATION URLs to the privileged role.

Those last two are not an oversight, they are the design (0018's ops note):

  * The WORKER is deliberately cross-tenant. The DSR and incident SLA sweeps and the
    regulatory due-source sweep walk every organisation's rows and have no single
    org_id to scope to. Under the restricted role they would correctly see nothing
    and stop sweeping silently, which is the worst way for a compliance sweep to stop.

  * MIGRATIONS need DDL. consiva_app has none, by design.

Idempotent: re-running rotates the password and rewrites the same three keys.
"""

from __future__ import annotations

import asyncio
import pathlib
import re
import secrets
import string
import sys

import asyncpg

from app.config import get_settings

ENV = pathlib.Path(__file__).parent / ".env"
ALPHABET = string.ascii_letters + string.digits


def generate_password(length: int = 40) -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(length))


def set_key(text: str, key: str, value: str) -> str:
    """Replace KEY=... in place, or append it. Keeps every other line untouched."""
    pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
    line = f"{key}={value}"
    if pattern.search(text):
        return pattern.sub(line, text)
    return text.rstrip("\n") + f"\n{line}\n"


async def main() -> int:
    settings = get_settings()
    owner_url = settings.database_url.replace("postgresql+asyncpg://", "postgresql://")

    if "consiva_app" in owner_url:
        print(
            "DATABASE_URL already points at consiva_app. Run this with the OWNER url "
            "in .env, or pass the owner credentials -- consiva_app cannot alter roles.",
            file=sys.stderr,
        )
        return 1

    password = generate_password()
    conn = await asyncpg.connect(owner_url)
    try:
        await conn.execute(f"alter role consiva_app login password '{password}'")
        row = await conn.fetchrow(
            "select rolsuper, rolbypassrls, rolcanlogin from pg_roles "
            "where rolname='consiva_app'"
        )
        owned = await conn.fetchval(
            "select count(*) from pg_tables where schemaname='public' "
            "and tableowner='consiva_app'"
        )
    finally:
        await conn.close()

    # If any of these were true the cutover would be theatre: a superuser, a role with
    # BYPASSRLS, or a table owner all escape the policies we are switching on.
    if row["rolsuper"] or row["rolbypassrls"] or owned:
        print(
            f"REFUSING: consiva_app is not least-privilege "
            f"(super={row['rolsuper']}, bypassrls={row['rolbypassrls']}, owns={owned}). "
            "RLS would still not be enforced.",
            file=sys.stderr,
        )
        return 1

    app_url = re.sub(r"//[^@]+@", f"//consiva_app:{password}@", owner_url)
    app_url_sqla = app_url.replace("postgresql://", "postgresql+asyncpg://")

    text = ENV.read_text(encoding="utf-8")
    # The privileged URL, kept under its own names for the two jobs that need it.
    text = set_key(text, "WORKER_DATABASE_URL", settings.database_url)
    text = set_key(text, "MIGRATION_DATABASE_URL", settings.database_url)
    text = set_key(text, "DATABASE_URL", app_url_sqla)
    ENV.write_text(text, encoding="utf-8")

    print("cutover complete")
    print("  DATABASE_URL            -> consiva_app   (API: RLS enforced)")
    print("  WORKER_DATABASE_URL     -> owner role    (cross-tenant sweeps)")
    print("  MIGRATION_DATABASE_URL  -> owner role    (DDL)")
    print(f"  consiva_app: super={row['rolsuper']} bypassrls={row['rolbypassrls']} "
          f"login={row['rolcanlogin']} owns={owned}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
