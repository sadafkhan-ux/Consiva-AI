"""Dev-only helper so the demo UI (app/static/demo/) can authenticate without a real
auth flow existing yet. Mints a JWT server-side, signed with the real
SUPABASE_JWT_SECRET — the secret itself never reaches the frontend, only a token.
Hard-gated to APP_ENV=development; returns 404 (not 403, to avoid confirming the
route exists) in any other environment.
"""

from datetime import UTC, datetime, timedelta

import jwt
from fastapi import APIRouter, HTTPException

from app.config import get_settings

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
