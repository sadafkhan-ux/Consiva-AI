"""Action Module API (Build Plan Component 8)."""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError
from app.core.security import CurrentUser, get_current_user
from app.db.repositories import action_repository, finding_repository
from app.db.session import get_db
from app.services import action_service

router = APIRouter(tags=["actions"])

_VALID_TYPES = {"task", "notification", "config_change"}


class CreateActionRequest(BaseModel):
    action_type: str
    title: str
    description: str | None = None
    assignee_label: str | None = None
    config_payload: dict | None = None

    @field_validator("action_type")
    @classmethod
    def _valid_type(cls, value: str) -> str:
        if value not in _VALID_TYPES:
            raise ValueError(f"action_type must be one of {sorted(_VALID_TYPES)}")
        return value


class TransitionActionRequest(BaseModel):
    to_status: str
    reason: str | None = None


class ActionResponse(BaseModel):
    id: uuid.UUID
    finding_id: uuid.UUID
    action_type: str
    title: str
    description: str | None
    assignee_label: str | None
    config_payload: dict | None
    status: str
    staged_at: datetime | None
    deployed_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


@router.post("/api/v1/consent/findings/{finding_id}/actions", response_model=ActionResponse, status_code=201)
async def create_action(
    finding_id: uuid.UUID,
    body: CreateActionRequest,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    action = await action_service.create_action(
        db, org_id=uuid.UUID(user.org_id), finding_id=finding_id, action_type=body.action_type, title=body.title,
        description=body.description, assignee_label=body.assignee_label, config_payload=body.config_payload,
        created_by_user_id=uuid.UUID(user.user_id),
    )
    return ActionResponse.model_validate(action)


@router.get("/api/v1/consent/findings/{finding_id}/actions", response_model=list[ActionResponse])
async def list_actions(
    finding_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # get_finding enforces org_id via a join to consent_scans -- the same
    # tenant-isolation check every other finding-scoped read uses; a finding_id from
    # another org must 404 here, not silently list another tenant's actions.
    finding = await finding_repository.get_finding(db, finding_id, uuid.UUID(user.org_id))
    if finding is None:
        raise NotFoundError(f"Finding {finding_id} not found")
    actions = await action_repository.list_actions_for_finding(db, finding_id)
    return [ActionResponse.model_validate(a) for a in actions]


@router.post("/api/v1/consent/actions/{action_id}/transition", response_model=ActionResponse)
async def transition_action(
    action_id: uuid.UUID,
    body: TransitionActionRequest,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    action = await action_service.transition_action(
        db, action_id=action_id, org_id=uuid.UUID(user.org_id), actor_user_id=uuid.UUID(user.user_id),
        to_status=body.to_status, reason=body.reason,
    )
    return ActionResponse.model_validate(action)
