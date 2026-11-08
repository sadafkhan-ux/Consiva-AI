"""Who is calling the Consent Agent API (`/api/v1/consent-agent/*`).

TEMPORARY, OPEN API. The service-key system was removed until a new auth system
replaces it. For now:

  - A request WITH a bearer login token acts as that user, exactly as before.
  - A request with NO token acts as the deployment's default account (the oldest
    user, the same one the console's auto-login signs in as), so the API can be
    called with no credentials at all.

The no-token path exists only while auto-login does (APP_ENV=development or
OPEN_ACCESS=true, see Settings.auto_login_enabled). Anywhere else a missing token
is still a 401, so a production deployment cannot become an open API by accident.

Row-level security still applies: every query runs scoped to the caller's
organisation, the default account's included.
"""

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.core.security import CurrentUser, get_current_user
from app.db.models import User
from app.db.session import apply_org_scope, get_db

_bearer_scheme = HTTPBearer(auto_error=False)


async def default_user(db: AsyncSession) -> User | None:
    """The account an unauthenticated caller acts as: the oldest user, so repeated
    calls always land on the same account."""
    return (
        await db.execute(select(User).order_by(User.created_at).limit(1))
    ).scalar_one_or_none()


async def get_caller(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> CurrentUser:
    if credentials is not None:
        return get_current_user(credentials=credentials, settings=settings)

    if not settings.auto_login_enabled:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Missing bearer token. Unauthenticated API access is only enabled when "
            "APP_ENV=development or OPEN_ACCESS=true.",
        )

    user = await default_user(db)
    if user is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Open API access needs an account to act as, and this database has none. "
            "Create one with `python create_user.py` first.",
        )
    await apply_org_scope(db, user.org_id)
    return CurrentUser(user_id=str(user.id), org_id=str(user.org_id), role=user.role)
