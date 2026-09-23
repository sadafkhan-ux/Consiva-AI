"""Dev-only helper so the demo UI (app/static/demo/) can authenticate without a real
auth flow existing yet. Mints a JWT server-side, signed with the real
SUPABASE_JWT_SECRET — the secret itself never reaches the frontend, only a token.
Hard-gated to APP_ENV=development; returns 404 (not 403, to avoid confirming the
route exists) in any other environment.
"""

from datetime import UTC, datetime, timedelta

import jwt
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core import tokens
from app.db.models import Organization, User
from app.db.session import get_db

router = APIRouter(prefix="/api/v1/dev", tags=["dev"])

DEMO_ORG_ID = "00000000-0000-0000-0000-000000000001"
DEMO_USER_ID = "00000000-0000-0000-0000-000000000002"
DEMO_TTL_HOURS = 12


@router.post("/demo-token")
async def demo_token():
    settings = get_settings()
    if settings.app_env != "development":
        raise HTTPException(status_code=404)

    # An expiry, because the verifiers now require one -- and because a demo token
    # that never expired was the one credential in the system with no lifetime at all.
    expires = datetime.now(UTC) + timedelta(hours=DEMO_TTL_HOURS)
    token = jwt.encode(
        {
            "sub": DEMO_USER_ID,
            "org_id": DEMO_ORG_ID,
            "aud": "authenticated",
            "exp": int(expires.timestamp()),
        },
        settings.supabase_jwt_secret,
        algorithm="HS256",
    )
    return {"token": token, "org_id": DEMO_ORG_ID, "expires_at": expires.isoformat()}


@router.post("/auto-login")
async def auto_login(db: AsyncSession = Depends(get_db)) -> dict:
    """Sign in as a real user with no password, for local testing only.

    WHY THIS EXISTS RATHER THAN THE LOGIN SCREEN BEING DELETED
    ----------------------------------------------------------
    Removing authentication outright is not an option that leaves a working app.
    Every one of the API's 113 operations takes the organisation from the verified
    token and nowhere else, and since migration 0018's cutover the database enforces
    that scope too -- an unscoped connection reads zero rows from every table. Strip
    the token and the console loads to a set of empty screens.

    So this skips the FORM, not the mechanism. It issues an ordinary access token
    through the same `issue_access_token` the real login uses, for a real user in the
    database, with the same claims and the same expiry. Everything downstream --
    org scoping, RLS, audit attribution -- behaves exactly as it does for a signed-in
    person, because as far as the rest of the system is concerned, one is.

    THREE THINGS KEEP THIS OUT OF PRODUCTION
    ----------------------------------------
    1. `api/v1/router.py` only registers this router when APP_ENV=development, so
       outside development the path does not exist in the routing table at all.
    2. The check below, as defence in depth against that registration changing.
    3. The frontend only calls it under Vite's DEV flag, so a production bundle has
       no code path that reaches it.

    404, not 403, for the same reason the demo-token route above uses 404: a 403
    confirms the endpoint is there.
    """
    settings = get_settings()
    if settings.app_env != "development":
        raise HTTPException(status_code=404)

    # A real row, so the token carries a real org and the console shows real data.
    # Ordered by created_at so repeated calls land on the same account rather than
    # whichever row the planner happened to return.
    user = (
        await db.execute(select(User).order_by(User.created_at).limit(1))
    ).scalar_one_or_none()
    if user is None:
        raise HTTPException(
            status_code=409,
            detail=(
                "auto-login needs a user to sign in as, and this database has none. "
                "Create one with `python create_user.py` first."
            ),
        )

    org = (
        await db.execute(select(Organization).where(Organization.id == user.org_id))
    ).scalar_one_or_none()

    token, expires_in = tokens.issue_access_token(
        user_id=str(user.id),
        org_id=str(user.org_id),
        role=user.role,
        settings=settings,
    )
    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_in": expires_in,
        "user": {
            "id": str(user.id),
            "email": user.email,
            "role": user.role,
            "org_id": str(user.org_id),
            "org_name": org.name if org else None,
        },
        # Said in the payload, not only in a docstring, so the banner the console
        # shows is reading a fact rather than repeating an assumption.
        "auth_bypassed": True,
        "note": (
            "Development auto-login: no password was checked. This endpoint does not "
            "exist when APP_ENV is anything but development."
        ),
    }
