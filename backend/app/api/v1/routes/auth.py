"""First-party authentication endpoints.

Replaces the Supabase Auth API as the way a human obtains an access token:

    POST /api/v1/auth/login   email + password  -> access_token
    GET  /api/v1/auth/me      whoami for the current token
    POST /api/v1/auth/users   admin creates another user in their own org

There is deliberately NO public signup. This is a B2B compliance product, so
accounts are provisioned -- the first admin by the create_user.py CLI, everyone
after that by an existing admin. A stranger being able to self-register on a
privacy-compliance platform would be a liability, not a feature.
"""

import re
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.core import passwords, tokens
from app.core.security import CurrentUser, get_current_user
from app.db.repositories import audit_repository, user_repository
from app.db.session import get_db

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


# Pydantic's EmailStr needs the email-validator package, which isn't installed.
# A shape check is enough here: the address is only ever used as a lookup key,
# never to send mail, so full RFC validation buys nothing.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _valid_email(value: str) -> str:
    value = value.strip().lower()
    if not _EMAIL_RE.match(value) or len(value) > 320:
        raise ValueError("must be a valid email address")
    return value


class LoginRequest(BaseModel):
    email: str
    password: str = Field(min_length=1)

    _norm_email = field_validator("email")(_valid_email)


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    org_id: str
    user_id: str
    role: str


class UserCreateRequest(BaseModel):
    email: str
    password: str = Field(min_length=passwords.MIN_PASSWORD_LENGTH)
    full_name: str | None = Field(default=None, max_length=200)
    role: str = Field(default="member", pattern="^(admin|member)$")

    _norm_email = field_validator("email")(_valid_email)


class UserResponse(BaseModel):
    """Note the absence of password_hash -- it must never leave the database."""

    id: str
    email: str
    full_name: str | None
    role: str
    is_active: bool
    last_login_at: str | None = None


@router.post("/login", response_model=LoginResponse)
async def login(
    payload: LoginRequest,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> LoginResponse:
    user = await user_repository.get_user_by_email(db, payload.email)

    # One identical failure for "no such user", "wrong password" and "disabled
    # account". Distinguishing them would turn this endpoint into an account
    # enumerator. verify_password is still called against a dummy hash when the
    # user is missing, so the response time doesn't reveal existence either.
    if user is None:
        passwords.verify_password(payload.password, _DUMMY_HASH)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password")
    if not passwords.verify_password(payload.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password")
    if not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password")

    try:
        access_token, expires_in = tokens.issue_access_token(
            user_id=str(user.id), org_id=str(user.org_id), role=user.role, settings=settings
        )
    except tokens.TokenError as exc:
        # Misconfiguration, not a credential problem -- say so plainly to the
        # operator rather than pretending the password was wrong.
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, str(exc)) from exc

    await user_repository.record_login(db, user)
    await audit_repository.record(
        db, org_id=user.org_id, actor_user_id=user.id, action="auth.login",
        entity_type="user", entity_id=user.id,
    )
    await db.commit()

    return LoginResponse(
        access_token=access_token, expires_in=expires_in,
        org_id=str(user.org_id), user_id=str(user.id), role=user.role,
    )


@router.get("/me", response_model=UserResponse)
async def me(
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> UserResponse:
    row = await user_repository.get_user(db, uuid.UUID(user.user_id))
    if row is None:
        # A valid legacy Supabase token can name a user that has no local row
        # yet -- correct during the transition, so this is a 404 about the
        # profile rather than a 401 about the token.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No local user profile for this token")
    return _user_response(row)


@router.post("/users", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def create_user(
    payload: UserCreateRequest,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> UserResponse:
    """Create a user inside the CALLER'S organization. The org is taken from the
    token, never from the request body, so an admin cannot create users in
    someone else's tenant."""
    if user.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only an admin may create users")

    if await user_repository.get_user_by_email(db, payload.email) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "A user with that email already exists")

    try:
        password_hash = passwords.hash_password(payload.password)
    except passwords.PasswordError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    row = await user_repository.create_user(
        db, org_id=uuid.UUID(user.org_id), email=payload.email,
        password_hash=password_hash, full_name=payload.full_name, role=payload.role,
    )
    await audit_repository.record(
        db, org_id=uuid.UUID(user.org_id), actor_user_id=uuid.UUID(user.user_id),
        action="auth.user_created", entity_type="user", entity_id=row.id,
        after={"email": row.email, "role": row.role},  # never the password or its hash
    )
    await db.commit()
    return _user_response(row)


@router.get("/users", response_model=list[UserResponse])
async def list_users(
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> list[UserResponse]:
    rows = await user_repository.list_users_for_org(db, uuid.UUID(user.org_id))
    return [_user_response(r) for r in rows]


def _user_response(row) -> UserResponse:
    return UserResponse(
        id=str(row.id), email=row.email, full_name=row.full_name, role=row.role,
        is_active=row.is_active,
        last_login_at=row.last_login_at.isoformat() if row.last_login_at else None,
    )


# A real bcrypt hash of a value nobody knows, compared against when the email
# doesn't exist so that a missing account costs the same time as a wrong
# password. Without this, response latency alone reveals which emails are
# registered.
_DUMMY_HASH = "$2b$12$C6UzMDM.H6dfI/f/IKcEeO.HkQyPQnMxvIRkPzL8ZQvVQ8S1qJmXe"
