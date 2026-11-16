"""The System health screen's API. Administrators only: it shows how the deployment is doing and who is told about it. Nothing here returns a key, a token or the mail password."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConsivaError, NotFoundError
from app.core.security import CurrentUser, get_current_user
from app.db.session import get_db
from app.monitoring import alerts, service
from app.monitoring.config import get_monitoring_settings
from app.monitoring.models import SystemAlert

router = APIRouter(prefix="/api/v1/system/monitoring", tags=["system monitoring"])


class AdminRequired(ConsivaError):
    status_code = 403


def _admin(user: CurrentUser) -> None:
    if user.role != "admin":
        raise AdminRequired("The system health screen is for administrators.")


class EmailStatus(BaseModel):
    enabled: bool
    configured: bool
    problem: str | None
    to: list[str]
    cc: list[str]


class HealthRow(BaseModel):
    source: str
    label: str
    status: Literal["ok", "failing", "unknown"]
    detail: str | None
    latency_ms: int | None
    checked_at: datetime
    last_ok_at: datetime | None
    consecutive_failures: int


class AlertRow(BaseModel):
    id: uuid.UUID
    source: str
    severity: Literal["warning", "critical"]
    status: Literal["open", "acknowledged", "resolved"]
    title: str
    detail: str
    action: str | None
    first_seen_at: datetime
    last_seen_at: datetime
    occurrences: int
    resolved_at: datetime | None
    acknowledged_by: str | None
    acknowledged_at: datetime | None
    last_notified_at: datetime | None
    notify_count: int
    notify_status: str | None


class MonitoringOverview(BaseModel):
    monitoring_enabled: bool
    check_interval_seconds: int
    email: EmailStatus
    health: list[HealthRow]
    alerts: list[AlertRow]
    open_count: int


class TestEmailResult(BaseModel):
    status: Literal["sent", "not_configured", "failed"]
    reason: str | None


def _email_status() -> EmailStatus:
    s = get_monitoring_settings()
    problem = s.email_problem()
    return EmailStatus(enabled=s.email_enabled, configured=problem is None, problem=problem, to=s.to_list, cc=s.cc_list)


async def _overview() -> MonitoringOverview:
    s = get_monitoring_settings()
    rows, alert_rows = await service.health_rows(), await service.alert_rows()
    return MonitoringOverview(
        monitoring_enabled=s.enabled, check_interval_seconds=s.check_interval_seconds, email=_email_status(),
        health=[HealthRow(**r) for r in rows], alerts=[AlertRow(**a) for a in alert_rows],
        open_count=sum(1 for a in alert_rows if a["status"] == "open"))


@router.get("", response_model=MonitoringOverview, summary="Health of each dependency, open alerts, and who is emailed")
async def overview(user: CurrentUser = Depends(get_current_user)) -> MonitoringOverview:
    _admin(user)
    return await _overview()


@router.post("/run", response_model=MonitoringOverview, summary="Run every check now")
async def run_now(user: CurrentUser = Depends(get_current_user)) -> MonitoringOverview:
    _admin(user)
    await service.run_round(force=True)
    return await _overview()


@router.post("/test-email", response_model=TestEmailResult, summary="Send a test email to the configured recipients")
async def test_email(user: CurrentUser = Depends(get_current_user)) -> TestEmailResult:
    _admin(user)
    status, reason = await service.send_test_email()
    return TestEmailResult(status=status, reason=reason)


@router.post("/alerts/{alert_id}/acknowledge", response_model=AlertRow, summary="Acknowledge an alert: it stays visible but stops sending reminders")
async def acknowledge(alert_id: uuid.UUID, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> AlertRow:
    _admin(user)
    alert = await db.scalar(select(SystemAlert).where(SystemAlert.id == alert_id))
    if alert is None:
        raise NotFoundError("Alert not found")
    if alert.status == "open":
        alert.status, alert.acknowledged_by, alert.acknowledged_at = "acknowledged", user.user_id, alerts.utcnow()
        await db.commit()
    return AlertRow(**service.alert_dict(alert))
