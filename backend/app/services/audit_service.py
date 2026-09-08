import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories import audit_repository


async def record(
    db: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_user_id: uuid.UUID | None,
    action: str,
    entity_type: str,
    entity_id: uuid.UUID,
    before: dict | None = None,
    after: dict | None = None,
    agent_run_id: uuid.UUID | None = None,
    model_name: str | None = None,
) -> None:
    """Service-layer entry point for audit writes (docs/architecture §K) — routes/other
    services call this rather than the repository directly, so every caller goes
    through one place if audit behavior ever needs to change (e.g. fan-out to an
    external SIEM)."""
    await audit_repository.record(
        db,
        org_id=org_id,
        actor_user_id=actor_user_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        before=before,
        after=after,
        agent_run_id=agent_run_id,
        model_name=model_name,
    )
