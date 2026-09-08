"""Domain-ownership verification (master reference §12 Critical) and Continuous
Monitoring (Build Plan Component 9) endpoints. See services/verification_service.py
and services/monitoring_service.py for the actual flows; these routes only expose them."""

import uuid
from datetime import datetime
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError
from app.core.security import CurrentUser, get_current_user
from app.db.repositories import monitoring_repository
from app.db.session import get_db
from app.services import monitoring_service, verification_service

router = APIRouter(prefix="/api/v1/consent/websites", tags=["websites"])


def _clean_domain(value: str) -> str:
    """Accepts either a bare domain or a full URL; normalizes to a lowercase hostname."""
    value = value.strip().lower()
    if "://" in value:
        value = urlparse(value).netloc or value
    return value.split("/", 1)[0]


class VerificationTokenResponse(BaseModel):
    domain: str
    txt_record: str
    instructions: str


class VerifyDomainRequest(BaseModel):
    domain: str

    @field_validator("domain")
    @classmethod
    def _domain_not_blank(cls, value: str) -> str:
        cleaned = _clean_domain(value)
        if not cleaned or "." not in cleaned:
            raise ValueError("domain must be a valid hostname, e.g. example.com")
        return cleaned


class VerifyDomainResponse(BaseModel):
    domain: str
    verified_at: datetime


@router.get("/verification-token", response_model=VerificationTokenResponse)
async def get_verification_token(
    domain: str = Query(..., description="Domain (or URL) to generate the ownership token for"),
    user: CurrentUser = Depends(get_current_user),
):
    cleaned = _clean_domain(domain)
    token = verification_service.expected_token(uuid.UUID(user.org_id), cleaned)
    txt_record = f"consiva-verify={token}"
    return VerificationTokenResponse(
        domain=cleaned,
        txt_record=txt_record,
        instructions=(
            f"Add a DNS TXT record on {cleaned} with the exact value '{txt_record}', "
            "wait for propagation, then POST /api/v1/consent/websites/verify."
        ),
    )


@router.post("/verify", response_model=VerifyDomainResponse)
async def verify_domain(
    body: VerifyDomainRequest,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    verified_at = await verification_service.verify_domain(db, org_id=uuid.UUID(user.org_id), domain=body.domain)
    await db.commit()
    return VerifyDomainResponse(domain=body.domain, verified_at=verified_at)


class SetScheduleRequest(BaseModel):
    interval_hours: int = Field(gt=0)
    enabled: bool = True


class ScheduleResponse(BaseModel):
    id: uuid.UUID
    website_id: uuid.UUID
    interval_hours: int
    enabled: bool
    baseline_scan_id: uuid.UUID | None
    last_triggered_scan_id: uuid.UUID | None
    next_run_at: datetime

    model_config = {"from_attributes": True}


class PromoteBaselineRequest(BaseModel):
    scan_id: uuid.UUID


class ScanDiffResponse(BaseModel):
    id: uuid.UUID
    baseline_scan_id: uuid.UUID
    new_scan_id: uuid.UUID
    added: dict
    removed: dict
    changed: dict
    has_material_change: bool
    created_at: datetime

    model_config = {"from_attributes": True}


@router.put("/{website_id}/monitoring", response_model=ScheduleResponse)
async def set_monitoring_schedule(
    website_id: uuid.UUID,
    body: SetScheduleRequest,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    schedule = await monitoring_service.set_schedule(
        db, org_id=uuid.UUID(user.org_id), website_id=website_id,
        interval_hours=body.interval_hours, enabled=body.enabled,
    )
    await db.commit()
    return ScheduleResponse.model_validate(schedule)


@router.get("/{website_id}/monitoring", response_model=ScheduleResponse)
async def get_monitoring_schedule(
    website_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    schedule = await monitoring_repository.get_schedule_for_website(db, website_id, uuid.UUID(user.org_id))
    if schedule is None:
        raise NotFoundError(f"No monitoring schedule for website {website_id}")
    return ScheduleResponse.model_validate(schedule)


@router.post("/{website_id}/monitoring/promote-baseline", response_model=ScheduleResponse)
async def promote_baseline(
    website_id: uuid.UUID,
    body: PromoteBaselineRequest,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    org_id = uuid.UUID(user.org_id)
    schedule = await monitoring_repository.get_schedule_for_website(db, website_id, org_id)
    if schedule is None:
        raise NotFoundError(f"No monitoring schedule for website {website_id}")
    updated = await monitoring_service.promote_baseline(
        db, schedule_id=schedule.id, org_id=org_id, scan_id=body.scan_id
    )
    await db.commit()
    return ScheduleResponse.model_validate(updated)


@router.get("/{website_id}/diffs", response_model=list[ScanDiffResponse])
async def list_diffs(
    website_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    diffs = await monitoring_repository.list_diffs_for_website(db, website_id, uuid.UUID(user.org_id))
    return [ScanDiffResponse.model_validate(d) for d in diffs]
