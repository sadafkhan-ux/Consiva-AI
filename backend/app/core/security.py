"""Auth dependency for FastAPI routes.

Accepts a Consiva-issued token FIRST (app/core/tokens.py, the primary path now
that identity lives in this database), then falls back to the legacy Supabase
paths below so a deployment mid-transition keeps working. Once every user has a
local account, delete the Supabase settings and the fallback stops being
reachable on its own.

Legacy Supabase verification (master reference §12 High: "migrate to Supabase's asymmetric
JWKS (ES256) before production"):
  - A real Supabase-issued access token is asymmetric (ES256, verified live against
    this project's actual JWKS endpoint — confirmed reachable and populated with a
    real EC key at SUPABASE_URL/auth/v1/.well-known/jwks.json). This is the only path
    trusted regardless of environment.
  - The HS256 shared-secret path (this project's own /api/v1/dev/demo-token minting)
    is accepted ONLY when app_env=="development" — it's a demo/dev convenience, not a
    real auth mechanism, and now can't reach a production deployment even if a demo
    token leaked, closing the gap the previous placeholder left open.
"""

from dataclasses import dataclass
from functools import lru_cache

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWKClient

from app.config import Settings, get_settings
from app.core import tokens

_bearer_scheme = HTTPBearer(auto_error=False)

_JWKS_ALGORITHMS = ("ES256", "RS256")


@lru_cache
def _jwks_client(supabase_url: str) -> PyJWKClient:
    # PyJWKClient caches fetched keys internally (cache_keys=True) -- one client per
    # supabase_url, reused across requests, so a live JWKS fetch only happens once
    # (then on cache expiry), not on every single request.
    return PyJWKClient(f"{supabase_url}/auth/v1/.well-known/jwks.json", cache_keys=True, lifespan=3600)


@dataclass(frozen=True)
class CurrentUser:
    user_id: str
    org_id: str
    # Present on Consiva-issued tokens; None for a legacy Supabase token, which
    # carries no role. Callers that gate on role must treat None as "not admin".
    role: str | None = None


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    settings: Settings = Depends(get_settings),
) -> CurrentUser:
    if credentials is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")
    token = credentials.credentials

    # ── Path 1: a Consiva-issued token (first-party auth) ──────────────────────
    # Checked first because it is now the primary path. The `iss` probe is
    # unverified and used ONLY to route to the right verifier; decode_access_token
    # does the real signature check, so a forged `iss` buys nothing.
    if tokens.looks_like_consiva_token(token):
        try:
            payload = tokens.decode_access_token(token, settings)
        except tokens.TokenError as exc:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token") from exc
        return _current_user_from(payload)

    # ── Path 2: a Supabase token (legacy, during the transition) ───────────────
    if not settings.supabase_url and not settings.supabase_jwt_secret:
        # Supabase has been removed from this deployment, so a non-Consiva token
        # cannot be verified by anything. Same non-disclosure posture as below.
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token")

    try:
        alg = jwt.get_unverified_header(token).get("alg")
    except jwt.PyJWTError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token") from exc

    try:
        if alg in _JWKS_ALGORITHMS:
            if not settings.supabase_url:
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token")
            signing_key = _jwks_client(settings.supabase_url).get_signing_key_from_jwt(token)
            payload = jwt.decode(token, signing_key.key, algorithms=[alg], audience="authenticated")
        elif alg == "HS256":
            if not settings.supabase_jwt_secret:
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token")
            if settings.app_env != "development":
                # A real Supabase project token is never HS256 once JWKS/ES256 is live
                # (confirmed for this project's own Supabase instance) -- an HS256
                # token outside development is either the dev-only demo-token minter
                # misused, or an attempt to exploit the old shared-secret path. Same
                # 404-flavored non-disclosure posture as the dev router itself: reject
                # outright rather than explain why.
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token")
            payload = jwt.decode(
                token, settings.supabase_jwt_secret, algorithms=["HS256"], audience="authenticated"
            )
        else:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token")
    except jwt.PyJWTError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token") from exc

    return _current_user_from(payload)


def _current_user_from(payload: dict) -> CurrentUser:
    """Shared claim extraction, so a Consiva token and a Supabase token produce
    an identical CurrentUser and no route handler can tell them apart."""
    org_id = payload.get("org_id") or payload.get("app_metadata", {}).get("org_id")
    if not org_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Token missing org_id claim")

    return CurrentUser(user_id=payload["sub"], org_id=org_id, role=payload.get("role"))
