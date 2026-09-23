"""Two organisations' worth of rows, so CI's RLS check can prove something.

A row-level-security test against an empty database passes without proving anything:
"0 rows because the policy hid them" and "0 rows because there are none" are the same
assertion. CI builds its database fresh from the migrations, so without this the check
would be green and vacuous -- which is the exact shape of the bug this project already
shipped once, where 61 tables of forced RLS were never consulted and nothing said so.

TWO organisations, not one. One org proves an unscoped connection sees nothing. Two
prove that a connection scoped to A cannot see B, which is the property tenant
isolation actually means.

Nothing here is realistic data and none of it is meant to be. It is the smallest set
of rows that makes the assertions real: an org, a user (so the login bootstrap policy
has something to find), a website and a scan.

Idempotent -- fixed UUIDs, and every insert is `on conflict do nothing`.
"""

from __future__ import annotations

import asyncio
import os
import sys

import asyncpg

ORG_A = "aaaaaaaa-0000-4000-8000-000000000001"
ORG_B = "bbbbbbbb-0000-4000-8000-000000000002"

ROWS = [
    ("organizations", "(id, name) values ($1, $2)", [
        (ORG_A, "CI probe org A"),
        (ORG_B, "CI probe org B"),
    ]),
    ("users", "(id, org_id, email, password_hash) values ($1, $2, $3, $4)", [
        ("aaaaaaaa-0000-4000-8000-00000000000a", ORG_A, "a@ci.probe", "not-a-real-hash"),
        ("bbbbbbbb-0000-4000-8000-00000000000b", ORG_B, "b@ci.probe", "not-a-real-hash"),
    ]),
    ("websites", "(id, org_id, domain) values ($1, $2, $3)", [
        ("aaaaaaaa-0000-4000-8000-00000000000c", ORG_A, "a.ci.probe"),
        ("bbbbbbbb-0000-4000-8000-00000000000d", ORG_B, "b.ci.probe"),
    ]),
    ("consent_scans", "(id, org_id, website_id, url, status) values ($1, $2, $3, $4, $5)", [
        ("aaaaaaaa-0000-4000-8000-00000000000e", ORG_A,
         "aaaaaaaa-0000-4000-8000-00000000000c", "https://a.ci.probe/", "completed"),
        ("bbbbbbbb-0000-4000-8000-00000000000f", ORG_B,
         "bbbbbbbb-0000-4000-8000-00000000000d", "https://b.ci.probe/", "completed"),
    ]),
]


async def main() -> int:
    raw = os.getenv("DATABASE_URL")
    if not raw:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 1
    conn = await asyncpg.connect(raw.replace("postgresql+asyncpg://", "postgresql://"))
    try:
        # Writes need a scope like everything else once RLS is enforced. The seed
        # switches org between batches rather than running unscoped, because an
        # unscoped INSERT is exactly what the policies are there to refuse.
        for table, clause, values in ROWS:
            for row in values:
                org = row[1] if table != "organizations" else row[0]
                await conn.execute("select set_config('app.org_id', $1, false)", org)
                await conn.execute(
                    f"insert into {table} {clause} on conflict do nothing",
                    *row,
                )
        await conn.execute("select set_config('app.org_id', '', false)")

        # Report the row counts so a CI log shows the seed landed, rather than only
        # that the script exited 0.
        #
        # Deliberately NOT asserting tenant isolation here. This script runs as the
        # role that owns the tables -- it has to, to write them -- and an owner is
        # subject to RLS only because FORCE is set, while a superuser bypasses it
        # regardless. On a CI image whose bootstrap user is both, every count below
        # reads as the full table, and an assertion here would fail for a reason that
        # has nothing to do with whether the policies are correct.
        #
        # Isolation is asserted where it can be: tests/test_rls_enforcement.py, run
        # against the least-privilege role. That separation is the whole point.
        total = await conn.fetchval("select count(*) from consent_scans")
        orgs = await conn.fetchval("select count(*) from organizations")
        users = await conn.fetchval("select count(*) from users")
        print(f"seeded: {orgs} organisation(s), {users} user(s), {total} scan(s) visible "
              f"to the seeding role")
        if total < 2 or orgs < 2:
            print("SEED FAILED: expected two organisations each with a scan", file=sys.stderr)
            return 1
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
