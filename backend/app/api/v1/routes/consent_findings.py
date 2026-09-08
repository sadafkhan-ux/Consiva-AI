import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import CurrentUser, get_current_user
from app.db.session import get_db
from app.services import review_service

router = APIRouter(prefix="/api/v1/consent/findings", tags=["consent-findings"])


class ReviewDecisionRequest(BaseModel):
    reason: str | None = None


class RejectFindingRequest(BaseModel):
    # Required on reject (master reference §12 Medium): a rejection with no recorded
    # rationale defeats the audit trail's whole purpose -- "who rejected this and WHY"
    # must always be answerable. Approve keeps reason optional except for high-risk
    # findings (enforced in review_service, where the finding's risk_level is known).
    reason: str

    @field_validator("reason")
    @classmethod
    def _reason_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("reason must not be empty")
        return value.strip()


class EditFindingRequest(BaseModel):
    edited_payload: dict
    reason: str  # required on edit, same audit rationale as reject

    @field_validator("reason")
    @classmethod
    def _reason_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("reason must not be empty")
        return value.strip()


class FindingDecisionResponse(BaseModel):
    id: uuid.UUID
    status: str


@router.post("/{finding_id}/approve", response_model=FindingDecisionResponse)
async def approve_finding(
    finding_id: uuid.UUID,
    body: ReviewDecisionRequest,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    finding = await review_service.approve_finding(
        db, finding_id=finding_id, org_id=uuid.UUID(user.org_id),
        reviewer_user_id=uuid.UUID(user.user_id), reason=body.reason,
    )
    return FindingDecisionResponse(id=finding.id, status=finding.status)


@router.post("/{finding_id}/reject", response_model=FindingDecisionResponse)
async def reject_finding(
    finding_id: uuid.UUID,
    body: RejectFindingRequest,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    finding = await review_service.reject_finding(
        db, finding_id=finding_id, org_id=uuid.UUID(user.org_id),
        reviewer_user_id=uuid.UUID(user.user_id), reason=body.reason,
    )
    return FindingDecisionResponse(id=finding.id, status=finding.status)


@router.post("/{finding_id}/edit", response_model=FindingDecisionResponse)
async def edit_finding(
    finding_id: uuid.UUID,
    body: EditFindingRequest,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    finding = await review_service.edit_finding(
        db, finding_id=finding_id, org_id=uuid.UUID(user.org_id), reviewer_user_id=uuid.UUID(user.user_id),
        edited_payload=body.edited_payload, reason=body.reason,
    )
    return FindingDecisionResponse(id=finding.id, status=finding.status)
