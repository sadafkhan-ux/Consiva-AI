"""Issue and verify Consiva's own access tokens.

HS256 is the right choice here, unlike in the Supabase path: we are both the
issuer and the only verifier, so a shared secret is simply a symmetric key we
never have to distribute. Asymmetric signing exists so a THIRD party can verify
without holding the signing key -- nobody needs that here.

Tokens carry the same two claims the rest of the application already reads
(`sub`, `org_id`) so route handlers and CurrentUser need no changes, plus `iss`
to distinguish a Consiva-issued token from a Supabase one during the dual-mode
transition.
"""

from datetime import UTC, datetime, timedelta

import jwt

from app.config import Settings

ISSUER = "consiva"
ALGORITHM = "HS256"
AUDIENCE = "authenticated"  # matches what the Supabase path already expects

DEFAULT_TTL_HOURS = 12


class TokenError(Exception):
    """The token is missing, malformed, expired, or not one of ours."""


def issue_access_token(
    *,
    user_id: str,
    org_id: str,
    role: str,
    settings: Settings,
    ttl_hours: int = DEFAULT_TTL_HOURS,
) -> tuple[str, int]:
    """Return (token, expires_in_seconds).

    12 hours by default: long enough that an operator isn't re-authenticating
    all day, short enough that a leaked token is not a permanent credential.
    Long-lived machine access is what integration keys are for -- see
    app/core/integration_auth.py -- so this never needs to be measured in days.
    """
    now = datetime.now(UTC)
    expires = now + timedelta(hours=ttl_hours)
    payload = {
        "sub": user_id,
        "org_id": org_id,
        "role": role,
        "aud": AUDIENCE,
        "iss": ISSUER,
        "iat": int(now.timestamp()),
        "exp": int(expires.timestamp()),
    }
    token = jwt.encode(payload, _secret(settings), algorithm=ALGORITHM)
    return token, int((expires - now).total_seconds())


def decode_access_token(token: str, settings: Settings) -> dict:
    """Verify a Consiva-issued token. Raises TokenError for anything else."""
    try:
        return jwt.decode(
            token,
            _secret(settings),
            algorithms=[ALGORITHM],
            audience=AUDIENCE,
            issuer=ISSUER,
        )
    except jwt.PyJWTError as exc:
        raise TokenError(str(exc)) from exc


def looks_like_consiva_token(token: str) -> bool:
    """Cheap unverified check used only to ROUTE a token to the right verifier
    during dual-mode. Reads the `iss` claim without validating the signature, so
    it must never be trusted for an authorization decision -- the actual
    verification still happens in decode_access_token.
    """
    try:
        unverified = jwt.decode(token, options={"verify_signature": False})
    except jwt.PyJWTError:
        return False
    return unverified.get("iss") == ISSUER


def _secret(settings: Settings) -> str:
    secret = settings.consiva_jwt_secret
    if not secret:
        # Failing loudly beats signing with a default: a predictable secret
        # would let anyone mint a valid admin token.
        raise TokenError("CONSIVA_JWT_SECRET is not configured; cannot issue or verify tokens")
    return secret
