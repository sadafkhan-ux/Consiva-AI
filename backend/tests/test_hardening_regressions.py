"""Regressions for the seven findings a live debug sweep turned up.

Every one of these was invisible to the test suite at the time, because each was a
property of configuration, of the database, or of a verifier's defaults rather than of
a function's return value. They are pinned here so they cannot come back quietly.

Where a check needs the database it is skipped rather than failed when none is
reachable, so this file still runs in CI against a bare checkout.
"""

import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt
import pytest
from dotenv import dotenv_values

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent


# ── Finding 2: the production compose defaulted to development ──────────────────

def test_prod_compose_never_defaults_app_env_to_development():
    """A file named .prod that falls back to development registers the dev token
    minter, which hands a valid token to anyone who reaches the URL."""
    compose = REPO / "docker-compose.prod.yml"
    if not compose.exists():
        pytest.skip("docker-compose.prod.yml not present")
    text = compose.read_text(encoding="utf-8")

    unsafe = re.findall(r"APP_ENV:\s*\$\{APP_ENV:-([^}]*)\}", text)
    assert not unsafe, (
        f"APP_ENV falls back to {unsafe} in the production compose file; "
        "it must fail closed with ${APP_ENV:?...} instead"
    )
    settings = re.findall(r"APP_ENV:\s*\$\{APP_ENV([^}]*)\}", text)
    assert settings, "APP_ENV is not set from the environment at all"
    assert all(v.startswith(":?") for v in settings), (
        f"every APP_ENV reference must be required, found: {settings}"
    )


# ── Finding 3: a token with no expiry was accepted forever ──────────────────────

def _decode_options(source: str, function: str) -> str:
    """The body of one function, for checking what it passes to jwt.decode."""
    start = source.index(f"def {function}")
    tail = source[start:]
    end = tail.find("\ndef ", 1)
    return tail[:end] if end > 0 else tail


def test_the_first_party_verifier_requires_an_expiry():
    from app.core import tokens

    body = _decode_options(Path(tokens.__file__).read_text(encoding="utf-8"), "decode_access_token")
    assert '"require": ["exp"]' in body, (
        "decode_access_token does not require exp; PyJWT honours an expiry it finds "
        "and says nothing about one that is absent, so a token minted without it "
        "would be valid forever"
    )


def test_the_legacy_verifier_requires_an_expiry_on_both_algorithms():
    from app.core import security

    source = Path(security.__file__).read_text(encoding="utf-8")
    decodes = [m for m in re.findall(r"jwt\.decode\((.*?)\n\s*\)", source, re.DOTALL)]
    assert len(decodes) >= 2, f"expected both the JWKS and HS256 decodes, found {len(decodes)}"
    for call in decodes:
        assert '"require": ["exp"]' in call, f"a jwt.decode without a required exp: {call[:120]}"


def test_a_token_without_an_expiry_is_rejected_end_to_end():
    """The property itself, through the real dependency, not just the source."""
    from fastapi import HTTPException
    from fastapi.security import HTTPAuthorizationCredentials

    from app.config import get_settings
    from app.core.security import get_current_user

    settings = get_settings()
    claims = {"sub": str(uuid.uuid4()), "org_id": str(uuid.uuid4()), "aud": "authenticated"}

    no_expiry = jwt.encode(claims, settings.supabase_jwt_secret, algorithm="HS256")
    with pytest.raises(HTTPException) as caught:
        get_current_user(
            HTTPAuthorizationCredentials(scheme="Bearer", credentials=no_expiry), settings
        )
    assert caught.value.status_code == 401

    with_expiry = jwt.encode(
        claims | {"exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp())},
        settings.supabase_jwt_secret, algorithm="HS256",
    )
    user = get_current_user(
        HTTPAuthorizationCredentials(scheme="Bearer", credentials=with_expiry), settings
    )
    assert user.org_id == claims["org_id"]


def test_the_dev_minter_issues_an_expiry():
    """It minted the one credential in the system with no lifetime at all."""
    from app.api.v1.routes import dev

    source = Path(dev.__file__).read_text(encoding="utf-8")
    assert '"exp"' in source, "the demo token carries no expiry"
    assert "DEMO_TTL_HOURS" in source


# ── Finding 1: row-level security was inert, and would have broken the app ──────

def test_current_org_id_does_not_reach_into_the_auth_schema():
    """The trap: the policies called auth.jwt(), so a least-privilege role got
    `permission denied for schema auth` on every query -- meaning the obvious
    hardening step took the whole application down."""
    activation = BACKEND / "migrations" / "0018_rls_activation.sql"
    assert activation.exists(), "migration 0018 is missing"
    text = activation.read_text(encoding="utf-8")

    definition = text[text.index("create or replace function current_org_id"):]
    definition = definition[: definition.index("$$", definition.index("$$") + 2)]
    assert "auth.jwt" not in definition, "current_org_id still depends on the auth schema"
    assert "current_setting('app.org_id'" in definition

    # `true` as the second argument means "NULL if unset" rather than raising, and
    # NULL is the answer that makes an unscoped connection see nothing.
    assert "current_setting('app.org_id', true)" in definition


def test_the_scope_binding_is_registered_once_on_the_router():
    """Not a parameter on eighty-seven endpoints, and nothing can forget it."""
    from app.api.v1 import router as router_module

    source = Path(router_module.__file__).read_text(encoding="utf-8")
    assert "bind_request_scope" in source
    assert "dependencies=[Depends(bind_request_scope)]" in source


def test_the_scope_binding_is_async_and_touches_no_database():
    """Two bugs in one test.

    Async: a sync dependency runs in a worker thread, and a ContextVar set there is
    set in a copy of the context and gone by the time the handler runs. The first
    version of this was sync, and the result was that every query correctly returned
    nothing.

    No database: doing the SQL in the dependency puts a round-trip in front of every
    request, including ones about to fail validation that never need a connection.
    """
    import inspect

    from app.core import security

    assert inspect.iscoroutinefunction(security.bind_request_scope)

    body = inspect.getsource(security.bind_request_scope)
    assert "set_org_scope" in body
    assert "await " not in body.split('"""')[-1], (
        "bind_request_scope awaits something; it should only record the scope and let "
        "the after_begin hook do the stamping"
    )


def test_an_unscoped_connection_is_the_safe_default():
    from app.db import session

    session.clear_org_scope()
    assert session.get_org_scope() is None

    session.set_org_scope("11111111-1111-1111-1111-111111111111")
    assert session.get_org_scope() == "11111111-1111-1111-1111-111111111111"
    session.clear_org_scope()


def test_the_scope_must_be_a_uuid():
    """It is interpolated into SQL, so it is parsed rather than trusted."""
    from app.db import session

    with pytest.raises(ValueError):
        session.set_org_scope("'; drop table incident_cases; --")
    session.clear_org_scope()


def test_the_worker_keeps_its_own_connection_string():
    """The worker is deliberately cross-tenant -- the SLA sweeps walk every
    organisation. Under the restricted role it would see nothing and stop sweeping
    silently, which is the worst way for a deadline tracker to stop."""
    source = (BACKEND / "app" / "jobs" / "worker.py").read_text(encoding="utf-8")
    assert "WORKER_DATABASE_URL" in source
    override = source.index("WORKER_DATABASE_URL")
    assert source.index("from app.db.session import") > override, (
        "the override must run before app.db.session is imported, because that module "
        "builds the engine at import time"
    )


# ── Finding 6: Agent 2's status column had no CHECK constraint ──────────────────

def test_the_ropa_status_vocabulary_matches_what_the_code_writes():
    """The constraint is only worth having if it lists every value. A first pass at
    this missed 'analyzing', which is written by the evidence-push path, and broke
    three end-to-end tests the moment it was applied."""
    migration = (BACKEND / "migrations" / "0017_ropa_run_hygiene.sql").read_text(encoding="utf-8")
    constrained = set(re.findall(
        r"check \(status in \(([^)]*)\)\)", migration
    )[0].replace("'", "").replace(" ", "").split(","))

    written = set()
    for path in (BACKEND / "app" / "services" / "ropa_run_service.py",
                 BACKEND / "app" / "db" / "repositories" / "ropa_repository.py"):
        source = path.read_text(encoding="utf-8")
        written |= set(re.findall(r'run\.status\s*=\s*"([a-z_]+)"', source))
        written |= set(re.findall(r'ingest_mode=ingest_mode,\s*status="([a-z_]+)"', source))

    missing = written - constrained
    assert not missing, (
        f"the code writes {sorted(missing)} but the CHECK constraint rejects them; "
        "every such write will fail with a CheckViolationError"
    )


# ── Finding 7: discovery could be triggered repeatedly with no guard ────────────

def test_discovery_refuses_to_start_twice_for_one_source():
    """Eight identical POSTs produced eight runs, each opening its own connection to
    the customer's production database."""
    import inspect

    from app.services import ropa_run_service

    body = inspect.getsource(ropa_run_service.run_discovery_for_source)
    assert "find_active_run_for_source" in body
    assert "DiscoveryAlreadyInProgressError" in body


def test_only_non_terminal_runs_block_a_retry():
    """A failed run must not wedge the source forever -- re-running after a failure is
    exactly what an operator does next."""
    import inspect

    from app.db.repositories import ropa_repository

    body = inspect.getsource(ropa_repository.find_active_run_for_source)
    assert '"pending", "discovering"' in body
    assert "completed" not in body.split('"""')[-1]
    assert "failed" not in body.split('"""')[-1]


# ── Finding 4: a migration applied outside the runner ───────────────────────────

def test_every_migration_on_disk_is_numbered_uniquely_and_in_order():
    """The ledger is what a deployment trusts. Two files claiming one number, or a
    gap, means somebody applied something by hand."""
    files = sorted((BACKEND / "migrations").glob("*.sql"))
    numbers = [int(f.stem.split("_", 1)[0]) for f in files]
    assert len(set(numbers)) == len(numbers), f"duplicate migration numbers: {numbers}"
    assert numbers == list(range(1, len(numbers) + 1)), (
        f"migration numbers are not contiguous from 1: {numbers}"
    )


# ── Live-database checks, skipped when there is none ────────────────────────────

# conftest sets a dummy DATABASE_URL so imports validate; these need the REAL one
# from backend/.env, the same approach test_ropa_end_to_end.py uses.
_REAL_DATABASE_URL = dotenv_values(BACKEND / ".env").get("DATABASE_URL")

live_only = pytest.mark.skipif(
    not _REAL_DATABASE_URL, reason="needs a real DATABASE_URL in backend/.env"
)


def _dsn() -> str:
    return _REAL_DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://", 1)


@live_only
@pytest.mark.asyncio
async def test_rls_is_forced_on_every_tenant_table():
    """An owner bypasses its own policies unless FORCE is set, and the application
    owns every table it reads."""
    import asyncpg

    conn = await asyncpg.connect(_dsn())
    try:
        unforced = await conn.fetch("""
            select c.relname from pg_class c
            join pg_namespace n on n.oid = c.relnamespace
            where n.nspname = 'public' and c.relkind = 'r'
              and c.relrowsecurity and not c.relforcerowsecurity
        """)
        assert not unforced, (
            f"RLS is enabled but not forced on: {[r['relname'] for r in unforced]} -- "
            "the owner bypasses these"
        )
    finally:
        await conn.close()


@live_only
@pytest.mark.asyncio
async def test_no_tenant_table_is_left_without_a_policy():
    import asyncpg

    conn = await asyncpg.connect(_dsn())
    try:
        naked = await conn.fetch("""
            select c.relname from pg_class c
            join pg_namespace n on n.oid = c.relnamespace
            where n.nspname = 'public' and c.relkind = 'r'
              and exists (select 1 from information_schema.columns
                          where table_schema = 'public' and table_name = c.relname
                            and column_name = 'org_id')
              and (not c.relrowsecurity
                   or not exists (select 1 from pg_policies
                                  where schemaname = 'public' and tablename = c.relname))
        """)
        assert not naked, f"tables holding org_id with no RLS policy: {[r['relname'] for r in naked]}"
    finally:
        await conn.close()
