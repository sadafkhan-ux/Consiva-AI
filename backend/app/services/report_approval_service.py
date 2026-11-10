"""
Release approval of a scan's report.

A DPDP scan report is a customer-facing deliverable. When REPORT_REQUIRE_APPROVAL is on, the PDF can be downloaded by a customer only after an administrator has
approved it: the approval names the person and the time and is bound to the scorecard version it was given for. Approving prints the report and runs the same quality
checks as every print (services/report_qc.py), so a report that fails them cannot be approved. Administrators can always download a report to review it.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.core.exceptions import ReportNotApprovedError
from app.db.models import ReportApproval
from app.services import assessment_service

ADMIN_ROLE = "admin"


@dataclass(frozen=True)
class ApprovalState:
    state: str                      # "approved" | "not_approved" | "stale" | "revoked"
    approved_by_user_id: str | None = None
    approved_at: str | None = None
    report_version: str | None = None
    note: str | None = None
    current_version: str = ""

    @property
    def releasable(self) -> bool:
        return self.state == "approved"


def is_reviewer(role: str | None) -> bool:
    return role == ADMIN_ROLE


async def get_state(db: AsyncSession, scan_id: uuid.UUID) -> ApprovalState:
    current = assessment_service.active_version()
    row = (await db.execute(select(ReportApproval).where(ReportApproval.scan_id == scan_id))).scalar_one_or_none()
    if row is None:
        return ApprovalState("not_approved", current_version=current)
    base = {"approved_by_user_id": str(row.approved_by_user_id), "approved_at": row.approved_at.isoformat() if row.approved_at else None, "report_version": row.report_version, "note": row.note, "current_version": current}
    if row.revoked_at is not None:
        return ApprovalState("revoked", **base)
    if row.report_version != current:
        return ApprovalState("stale", **base)
    return ApprovalState("approved", **base)


async def approve(db: AsyncSession, scan_id: uuid.UUID, user_id: str, note: str | None) -> ApprovalState:
    """Record the approval (replacing an earlier or withdrawn one). The caller has already printed the report through the quality gate."""
    now = datetime.now(UTC)
    row = (await db.execute(select(ReportApproval).where(ReportApproval.scan_id == scan_id))).scalar_one_or_none()
    if row is None:
        row = ReportApproval(scan_id=scan_id, approved_by_user_id=uuid.UUID(user_id), approved_at=now, report_version=assessment_service.active_version(), note=note)
        db.add(row)
    else:
        row.approved_by_user_id, row.approved_at, row.report_version, row.note = uuid.UUID(user_id), now, assessment_service.active_version(), note
        row.revoked_at = row.revoked_by_user_id = None
    await db.commit()
    return await get_state(db, scan_id)


async def revoke(db: AsyncSession, scan_id: uuid.UUID, user_id: str) -> ApprovalState:
    row = (await db.execute(select(ReportApproval).where(ReportApproval.scan_id == scan_id))).scalar_one_or_none()
    if row is not None and row.revoked_at is None:
        row.revoked_at, row.revoked_by_user_id = datetime.now(UTC), uuid.UUID(user_id)
        await db.commit()
    return await get_state(db, scan_id)


async def require_release(db: AsyncSession, scan_id: uuid.UUID, role: str | None, settings: Settings | None = None) -> str:
    """Raise unless the report may be handed to this caller. Returns "released" (approved, or approval not required) or "draft" (a reviewer downloading it unapproved)."""
    settings = settings or get_settings()
    if not settings.report_require_approval:
        return "released"
    state = await get_state(db, scan_id)
    if state.releasable:
        return "released"
    if is_reviewer(role):
        return "draft"
    why = {"stale": "its approval was given for an earlier version of the scoring rules", "revoked": "its approval was withdrawn"}.get(state.state, "it has not been approved yet")
    raise ReportNotApprovedError(f"This report is not available for download: {why}. A reviewer must approve it first.")
