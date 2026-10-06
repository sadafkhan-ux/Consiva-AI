"""Machine-to-machine authentication for external ROPA integration adapters.

Complements app/core/security.py rather than replacing it: that module
authenticates a HUMAN via a Supabase JWT, this one authenticates a SERVICE (an
adapter running inside a customer's own infrastructure) via a long-lived,
revocable, org-scoped key.

The key is generated here, shown to the operator exactly once, and stored only
as a SHA-256 hash -- see migrations/0008 for why a fast hash is the right choice
for a 256-bit random token.
"""

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.core.security import CurrentUser, get_current_user
from app.db.models import RopaIntegrationKey
from app.db.session import apply_org_scope, get_db

_bearer_scheme = HTTPBearer(auto_error=False)

KEY_NAMESPACE = "csv"  # consiva service key
_PREFIX_BYTES = 6
_SECRET_BYTES = 32


@dataclass(frozen=True)
class IntegrationPrincipal:
    """The authenticated caller when a request arrives from a service, not a user."""

    org_id: str
    key_id: uuid.UUID
    name: str
    scopes: tuple[str, ...]


def generate_key() -> tuple[str, str, str]:
    """Return (full_key, key_prefix, key_hash).

    The full key is returned to the caller ONCE and never persisted anywhere.
    """
    prefix = secrets.token_hex(_PREFIX_BYTES)
    secret = secrets.token_urlsafe(_SECRET_BYTES)
    full_key = f"{KEY_NAMESPACE}_{prefix}_{secret}"
    return full_key, prefix, hash_key(full_key)


def hash_key(full_key: str) -> str:
    return hashlib.sha256(full_key.encode("utf-8")).hexdigest()


_PREFIX_LEN = _PREFIX_BYTES * 2  # token_hex doubles the byte count
_MIN_SECRET_LEN = 32


def parse_prefix(full_key: str) -> str | None:
    """Extract the public lookup prefix from a presented key.

    Shape is validated strictly (namespace, hex prefix of the exact expected
    length, and a secret long enough to be one of ours) so a malformed or
    truncated token is rejected before it ever reaches a database lookup.
    """
    parts = full_key.split("_", 2)
    if len(parts) != 3 or parts[0] != KEY_NAMESPACE:
        return None
    prefix, secret = parts[1], parts[2]
    if len(prefix) != _PREFIX_LEN or not all(c in "0123456789abcdef" for c in prefix):
        return None
    if len(secret) < _MIN_SECRET_LEN:
        return None
    return prefix


async def resolve_integration_key(db: AsyncSession, presented: str) -> IntegrationPrincipal | None:
    """Verify a presented key. Returns None for anything invalid -- the caller
    decides the HTTP response, so this never leaks WHY a key failed."""
    prefix = parse_prefix(presented)
    if prefix is None:
        return None

    result = await db.execute(
        select(RopaIntegrationKey).where(RopaIntegrationKey.key_prefix == prefix)
    )
    row = result.scalar_one_or_none()
    if row is None:
        return None

    # Constant-time compare so a timing signal can't be used to brute-force the
    # hash even though the prefix lookup itself is a plain index hit.
    if not secrets.compare_digest(row.key_hash, hash_key(presented)):
        return None
    if not row.enabled or row.revoked_at is not None:
        return None
    if row.expires_at is not None and row.expires_at < datetime.now(UTC):
        return None

    row.last_used_at = datetime.now(UTC)
    await db.flush()

    return IntegrationPrincipal(
        org_id=str(row.org_id),
        key_id=row.id,
        name=row.name,
        scopes=tuple(row.scopes or ()),
    )


async def get_integration_principal(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> IntegrationPrincipal:
    """FastAPI dependency for endpoints an external adapter calls."""
    if credentials is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing integration key")

    principal = await resolve_integration_key(db, credentials.credentials)
    if principal is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or revoked integration key")

    # The key names its organisation; scope the connection to it before the adapter
    # writes anything (migration 0018).
    await apply_org_scope(db, principal.org_id)
    return principal


def require_scope(principal: IntegrationPrincipal, scope: str) -> None:
    if scope not in principal.scopes:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"Integration key {principal.name!r} lacks the required scope {scope!r}",
        )


# ── Accepting either a person or a service on the same endpoint ─────────────────

#: Scope a service key needs to drive the Consent Agent's integration API.
SCOPE_CONSENT_SCAN = "consent:scan"

#: Scope a service key needs to read ROPA results (runs/records/findings/changes)
#: and promote a baseline -- separate from "evidence:write" so a push-only adapter
#: isn't implicitly handed read access, and an integrator asking for read access
#: doesn't have to be granted write too.
SCOPE_EVIDENCE_READ = "evidence:read"

#: Every scope a key may be granted. Enumerated so a typo at mint time is refused
#: rather than stored as a scope that can never match anything.
KNOWN_SCOPES = ("evidence:write", SCOPE_CONSENT_SCAN, SCOPE_EVIDENCE_READ)


async def _authenticate_with_scope(
    *,
    required_scope: str,
    credentials: HTTPAuthorizationCredentials | None,
    db: AsyncSession,
    settings: Settings,
) -> CurrentUser:
    """Authenticate a request from EITHER a signed-in person or a service key
    holding `required_scope`. Shared by every dependency below -- the two
    existing callers (`get_caller`, `get_ropa_reader`) differ only in which
    scope they require.

    WHY BOTH ON ONE ENDPOINT

    A login token is a person's credential and expires in twelve hours by design. That
    is correct for a console and useless for a website calling this API on a schedule:
    the integration would break every night, and the obvious workaround -- a token that
    never expires -- is a password that can never be rotated or revoked.

    A service key is the answer the codebase already had, used until now only by the
    ROPA adapter: long-lived, revocable, org-scoped, stored as a hash, and granted
    named scopes.

    ATTRIBUTION

    The returned `user_id` is the person who MINTED the key, not a synthetic identity.
    They authorised the integration, so an action it takes is genuinely attributable to
    them, and the audit trail keeps pointing at a human. `key_name` carries which key
    was used, so "which integration did this" stays answerable too.

    A key is tried only when the credential looks like one (`csv_` namespace). A bearer
    token that does not is passed to the normal user path, so nothing about existing
    callers changes.
    """
    if credentials is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")

    presented = credentials.credentials
    if parse_prefix(presented) is None:
        # Not a service key. Verify it as a user token exactly as before -- including
        # raising that module's 401, so a bad token gets one answer and not two.
        return get_current_user(credentials=credentials, settings=settings)

    principal = await resolve_integration_key(db, presented)
    if principal is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or revoked integration key")
    require_scope(principal, required_scope)

    row = (await db.execute(
        select(RopaIntegrationKey).where(RopaIntegrationKey.id == principal.key_id)
    )).scalar_one()

    await apply_org_scope(db, principal.org_id)
    return CurrentUser(
        user_id=str(row.created_by_user_id) if row.created_by_user_id else str(principal.key_id),
        org_id=principal.org_id,
        role=None,          # a key is never an admin; role-gated routes stay closed to it
        key_name=principal.name,
    )


async def get_caller(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> CurrentUser:
    """Authenticate a request from either a signed-in person or a service key
    holding `consent:scan` -- the Consent Agent's integration API."""
    return await _authenticate_with_scope(
        required_scope=SCOPE_CONSENT_SCAN, credentials=credentials, db=db, settings=settings,
    )


async def get_ropa_reader(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> CurrentUser:
    """Authenticate a request from either a signed-in person or a service key
    holding `evidence:read` -- ROPA's read endpoints (runs/records/findings/
    changes) and baseline promotion.

    A key that only ever pushed evidence (`evidence:write`) cannot read its own
    results back through this dependency; it needs `evidence:read` granted
    explicitly, same as any other scope.
    """
    return await _authenticate_with_scope(
        required_scope=SCOPE_EVIDENCE_READ, credentials=credentials, db=db, settings=settings,
    )
