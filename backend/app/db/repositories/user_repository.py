"""Reads and writes for first-party auth (migrations/0010_local_auth.sql)."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Organization, User


async def get_user_by_email(db: AsyncSession, email: str) -> User | None:
    """Case-insensitive lookup, matching the `lower(email)` unique index so a
    login can't be defeated by capitalisation."""
    result = await db.execute(select(User).where(func.lower(User.email) == email.strip().lower()))
    return result.scalar_one_or_none()


async def get_user(db: AsyncSession, user_id: uuid.UUID) -> User | None:
    result = await db.execute(select(User).where(User.id == user_id))
    return result.scalar_one_or_none()


async def list_users_for_org(db: AsyncSession, org_id: uuid.UUID) -> list[User]:
    result = await db.execute(
        select(User).where(User.org_id == org_id).order_by(User.created_at)
    )
    return list(result.scalars().all())


async def create_organization(db: AsyncSession, *, name: str, slug: str | None = None) -> Organization:
    row = Organization(name=name, slug=slug)
    db.add(row)
    await db.flush()
    return row


async def get_organization(db: AsyncSession, org_id: uuid.UUID) -> Organization | None:
    result = await db.execute(select(Organization).where(Organization.id == org_id))
    return result.scalar_one_or_none()


async def create_user(
    db: AsyncSession,
    *,
    org_id: uuid.UUID,
    email: str,
    password_hash: str,
    full_name: str | None = None,
    role: str = "member",
) -> User:
    row = User(
        org_id=org_id,
        email=email.strip().lower(),
        password_hash=password_hash,
        full_name=full_name,
        role=role,
    )
    db.add(row)
    await db.flush()
    return row


async def record_login(db: AsyncSession, user: User) -> None:
    user.last_login_at = datetime.now(UTC)
    await db.flush()
