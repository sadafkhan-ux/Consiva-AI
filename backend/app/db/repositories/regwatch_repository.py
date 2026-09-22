"""Persistence for Agent 5 (Regulatory Watch).

Every read takes an `org_id` and filters on it. That is the actual tenancy
enforcement: row-level security is now genuinely active for a least-privilege role
(migration 0018), but the API still runs as the owner on most deployments, so these
filters are the layer that is always doing the work.

The one deliberate exception is `list_due_sources`, which is called by the worker on
behalf of every tenant and is documented as such below.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    AuditLog,
    RegWatchAction,
    RegWatchApproval,
    RegWatchBaseline,
    RegWatchChange,
    RegWatchCollection,
    RegWatchFinding,
    RegWatchImpact,
    RegWatchSource,
)

# ── Sources ──────────────────────────────────────────────────────────────────────


async def create_source(db: AsyncSession, row: RegWatchSource) -> RegWatchSource:
    db.add(row)
    await db.flush()
    return row


async def get_source(
    db: AsyncSession, source_id: uuid.UUID, org_id: uuid.UUID
) -> RegWatchSource | None:
    result = await db.execute(
        select(RegWatchSource).where(
            RegWatchSource.id == source_id, RegWatchSource.org_id == org_id
        )
    )
    return result.scalar_one_or_none()


async def find_source_by_name(
    db: AsyncSession, org_id: uuid.UUID, name: str
) -> RegWatchSource | None:
    result = await db.execute(
        select(RegWatchSource).where(
            RegWatchSource.org_id == org_id, func.lower(RegWatchSource.name) == name.strip().lower()
        )
    )
    return result.scalar_one_or_none()


async def list_sources(
    db: AsyncSession, org_id: uuid.UUID, *, enabled_only: bool = False
) -> list[RegWatchSource]:
    stmt = select(RegWatchSource).where(RegWatchSource.org_id == org_id)
    if enabled_only:
        stmt = stmt.where(RegWatchSource.enabled.is_(True))
    result = await db.execute(stmt.order_by(RegWatchSource.name))
    return list(result.scalars().all())


async def list_due_sources(db: AsyncSession, *, limit: int = 100) -> list[RegWatchSource]:
    """Sources whose check interval has elapsed.

    Deliberately NOT org-scoped: called by the worker, which acts for every tenant
    rather than on behalf of a signed-in user. The only caller is
    app/services/regwatch_run_service.py -- never a request handler. This is the same
    arrangement, and the same caveat, as Agents 3 and 4's SLA sweeps.

    A source that has never been checked sorts first: `last_checked_at IS NULL` is not
    "checked a long time ago", it is "never", and a newly registered source should be
    collected promptly rather than waiting out a full interval.
    """
    now = datetime.now(UTC)
    result = await db.execute(
        select(RegWatchSource)
        .where(
            RegWatchSource.enabled.is_(True),
            (RegWatchSource.last_checked_at.is_(None))
            | (
                RegWatchSource.last_checked_at
                < now - func.make_interval(0, 0, 0, 0, 0, RegWatchSource.check_interval_minutes)
            ),
        )
        .order_by(RegWatchSource.last_checked_at.nulls_first())
        .limit(limit)
    )
    return list(result.scalars().all())


def is_due(source: RegWatchSource, *, now: datetime | None = None) -> bool:
    """The same question in Python, for callers holding a row already.

    Kept beside the query so the two definitions of "due" cannot drift -- the query is
    what the sweep uses, this is what a single-source check uses, and a disagreement
    between them would mean a source the UI calls due that the worker never picks up.
    """
    if not source.enabled:
        return False
    if source.last_checked_at is None:
        return True
    moment = now or datetime.now(UTC)
    return moment - source.last_checked_at >= timedelta(minutes=source.check_interval_minutes)


# ── Collections (append-only: there is no update path, deliberately) ────────────


async def add_collection(
    db: AsyncSession, org_id: uuid.UUID, row: RegWatchCollection
) -> RegWatchCollection:
    """`org_id` is an ASSERTION, not a filter. The row is built by the service from the
    source it is collecting, so it should already carry the right tenant; checking
    anyway means a future caller that builds one from the wrong source fails loudly
    rather than filing another organisation's evidence."""
    if row.org_id != org_id:
        raise ValueError(
            f"refusing to file a collection for org {row.org_id} against a source in org {org_id}"
        )
    db.add(row)
    await db.flush()
    return row


async def get_collection(
    db: AsyncSession, collection_id: uuid.UUID, org_id: uuid.UUID
) -> RegWatchCollection | None:
    result = await db.execute(
        select(RegWatchCollection).where(
            RegWatchCollection.id == collection_id, RegWatchCollection.org_id == org_id
        )
    )
    return result.scalar_one_or_none()


async def list_collections(
    db: AsyncSession, source_id: uuid.UUID, org_id: uuid.UUID, *, limit: int = 50
) -> list[RegWatchCollection]:
    result = await db.execute(
        select(RegWatchCollection)
        .where(RegWatchCollection.source_id == source_id, RegWatchCollection.org_id == org_id)
        .order_by(RegWatchCollection.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def last_successful_collection(
    db: AsyncSession, source_id: uuid.UUID, org_id: uuid.UUID
) -> RegWatchCollection | None:
    result = await db.execute(
        select(RegWatchCollection)
        .where(
            RegWatchCollection.source_id == source_id,
            RegWatchCollection.org_id == org_id,
            RegWatchCollection.status == "collected",
        )
        .order_by(RegWatchCollection.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


# ── Baselines ────────────────────────────────────────────────────────────────────


async def current_baseline(
    db: AsyncSession, source_id: uuid.UUID, org_id: uuid.UUID
) -> RegWatchBaseline | None:
    result = await db.execute(
        select(RegWatchBaseline)
        .where(
            RegWatchBaseline.source_id == source_id,
            RegWatchBaseline.org_id == org_id,
            RegWatchBaseline.superseded_at.is_(None),
        )
        .order_by(RegWatchBaseline.version.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def next_baseline_version(
    db: AsyncSession, source_id: uuid.UUID, org_id: uuid.UUID
) -> int:
    result = await db.execute(
        select(func.coalesce(func.max(RegWatchBaseline.version), 0)).where(
            RegWatchBaseline.source_id == source_id, RegWatchBaseline.org_id == org_id
        )
    )
    return int(result.scalar_one()) + 1


async def supersede_baselines(
    db: AsyncSession, source_id: uuid.UUID, org_id: uuid.UUID, *, now: datetime | None = None
) -> None:
    """Retire the live baseline rather than deleting it. A baseline an organisation
    compared against last month is part of the record even once it has moved on."""
    moment = now or datetime.now(UTC)
    result = await db.execute(
        select(RegWatchBaseline).where(
            RegWatchBaseline.source_id == source_id,
            RegWatchBaseline.org_id == org_id,
            RegWatchBaseline.superseded_at.is_(None),
        )
    )
    for row in result.scalars().all():
        row.superseded_at = moment
    await db.flush()


async def create_baseline(db: AsyncSession, row: RegWatchBaseline) -> RegWatchBaseline:
    """The column is NOT NULL, but a None slipping through SQLAlchemy would surface as
    an IntegrityError several frames from the cause. Said plainly here instead."""
    if row.approved_by_user_id is None:
        raise ValueError(
            "a baseline cannot be created without the user who approved it; the agent "
            "does not advance its own baseline"
        )
    db.add(row)
    await db.flush()
    return row


async def list_baselines(
    db: AsyncSession, source_id: uuid.UUID, org_id: uuid.UUID
) -> list[RegWatchBaseline]:
    result = await db.execute(
        select(RegWatchBaseline)
        .where(RegWatchBaseline.source_id == source_id, RegWatchBaseline.org_id == org_id)
        .order_by(RegWatchBaseline.version.desc())
    )
    return list(result.scalars().all())


# ── Changes ──────────────────────────────────────────────────────────────────────


async def create_change(db: AsyncSession, row: RegWatchChange) -> RegWatchChange:
    db.add(row)
    await db.flush()
    return row


async def get_change(
    db: AsyncSession, change_id: uuid.UUID, org_id: uuid.UUID
) -> RegWatchChange | None:
    result = await db.execute(
        select(RegWatchChange).where(
            RegWatchChange.id == change_id, RegWatchChange.org_id == org_id
        )
    )
    return result.scalar_one_or_none()


async def list_changes(
    db: AsyncSession, source_id: uuid.UUID, org_id: uuid.UUID, *, limit: int = 50
) -> list[RegWatchChange]:
    result = await db.execute(
        select(RegWatchChange)
        .where(RegWatchChange.source_id == source_id, RegWatchChange.org_id == org_id)
        .order_by(RegWatchChange.detected_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


# ── Findings ─────────────────────────────────────────────────────────────────────


async def create_finding(db: AsyncSession, row: RegWatchFinding) -> RegWatchFinding:
    db.add(row)
    await db.flush()
    return row


async def get_finding(
    db: AsyncSession, finding_id: uuid.UUID, org_id: uuid.UUID
) -> RegWatchFinding | None:
    result = await db.execute(
        select(RegWatchFinding).where(
            RegWatchFinding.id == finding_id, RegWatchFinding.org_id == org_id
        )
    )
    return result.scalar_one_or_none()


async def reference_exists(db: AsyncSession, org_id: uuid.UUID, reference: str) -> bool:
    result = await db.execute(
        select(RegWatchFinding.id).where(
            RegWatchFinding.org_id == org_id, RegWatchFinding.reference == reference
        )
    )
    return result.first() is not None


async def list_findings(
    db: AsyncSession, org_id: uuid.UUID, *, status: str | None = None, limit: int = 100
) -> list[RegWatchFinding]:
    stmt = select(RegWatchFinding).where(RegWatchFinding.org_id == org_id)
    if status:
        stmt = stmt.where(RegWatchFinding.status == status)
    result = await db.execute(stmt.order_by(RegWatchFinding.created_at.desc()).limit(limit))
    return list(result.scalars().all())


async def list_open_findings_for_source(
    db: AsyncSession, source_id: uuid.UUID, org_id: uuid.UUID, *, statuses: tuple[str, ...]
) -> list[RegWatchFinding]:
    """Findings a fresh change may supersede.

    The caller passes the statuses -- `lifecycle.SUPERSEDABLE` -- rather than this
    module carrying its own idea of which ones a human has already decided.
    """
    result = await db.execute(
        select(RegWatchFinding)
        .where(
            RegWatchFinding.source_id == source_id,
            RegWatchFinding.org_id == org_id,
            RegWatchFinding.status.in_(statuses),
        )
        .order_by(RegWatchFinding.created_at)
    )
    return list(result.scalars().all())


# ── Impacts ──────────────────────────────────────────────────────────────────────


async def replace_impacts(
    db: AsyncSession, finding_id: uuid.UUID, org_id: uuid.UUID, rows: list[RegWatchImpact]
) -> None:
    """Re-deriving the impact map replaces the derived rows wholesale.

    Rows a person entered (`derived_from='manual'`) are KEPT: a reviewer who recorded
    that this change touches a contract the agent cannot see has said something no
    rule could know, and re-running the mapper must not erase it. Same rule as Agent
    4's affected-data map.
    """
    existing = await db.execute(
        select(RegWatchImpact).where(
            RegWatchImpact.finding_id == finding_id,
            RegWatchImpact.org_id == org_id,
            RegWatchImpact.derived_from != "manual",
        )
    )
    for row in existing.scalars().all():
        await db.delete(row)
    for row in rows:
        db.add(row)
    await db.flush()


async def add_impact(db: AsyncSession, row: RegWatchImpact) -> RegWatchImpact:
    db.add(row)
    await db.flush()
    return row


async def get_impact(
    db: AsyncSession, impact_id: uuid.UUID, org_id: uuid.UUID
) -> RegWatchImpact | None:
    """One impact row, org-scoped. A row in another org returns None, exactly as a
    missing one does -- distinguishing them would confirm the id."""
    result = await db.execute(
        select(RegWatchImpact).where(
            RegWatchImpact.id == impact_id, RegWatchImpact.org_id == org_id
        )
    )
    return result.scalar_one_or_none()


async def list_impacts(
    db: AsyncSession, finding_id: uuid.UUID, org_id: uuid.UUID
) -> list[RegWatchImpact]:
    result = await db.execute(
        select(RegWatchImpact)
        .where(RegWatchImpact.finding_id == finding_id, RegWatchImpact.org_id == org_id)
        .order_by(RegWatchImpact.target_kind, RegWatchImpact.target_label)
    )
    return list(result.scalars().all())


# ── Approvals (append-only) ──────────────────────────────────────────────────────


async def record_approval(
    db: AsyncSession,
    *,
    org_id: uuid.UUID,
    finding_id: uuid.UUID,
    reviewer_user_id: uuid.UUID,
    subject: str,
    decision: str,
    reason: str | None = None,
    edited_payload: dict | None = None,
    expires_at: datetime | None = None,
) -> RegWatchApproval:
    row = RegWatchApproval(
        org_id=org_id, finding_id=finding_id, reviewer_user_id=reviewer_user_id,
        subject=subject, decision=decision, reason=reason,
        edited_payload=edited_payload, expires_at=expires_at,
    )
    db.add(row)
    await db.flush()
    return row


async def list_approvals(
    db: AsyncSession, finding_id: uuid.UUID, org_id: uuid.UUID
) -> list[RegWatchApproval]:
    result = await db.execute(
        select(RegWatchApproval)
        .where(RegWatchApproval.finding_id == finding_id, RegWatchApproval.org_id == org_id)
        .order_by(RegWatchApproval.created_at)
    )
    return list(result.scalars().all())


async def latest_approval(
    db: AsyncSession, finding_id: uuid.UUID, org_id: uuid.UUID, *, subject: str
) -> RegWatchApproval | None:
    result = await db.execute(
        select(RegWatchApproval)
        .where(
            RegWatchApproval.finding_id == finding_id,
            RegWatchApproval.org_id == org_id,
            RegWatchApproval.subject == subject,
        )
        .order_by(RegWatchApproval.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


# ── Actions ──────────────────────────────────────────────────────────────────────


async def add_actions(db: AsyncSession, org_id: uuid.UUID, rows: list[RegWatchAction]) -> None:
    for row in rows:
        if row.org_id != org_id:
            raise ValueError("refusing to write an action into another org's finding")
        db.add(row)
    await db.flush()


async def get_action(
    db: AsyncSession, action_id: uuid.UUID, org_id: uuid.UUID
) -> RegWatchAction | None:
    result = await db.execute(
        select(RegWatchAction).where(
            RegWatchAction.id == action_id, RegWatchAction.org_id == org_id
        )
    )
    return result.scalar_one_or_none()


async def list_actions(
    db: AsyncSession, finding_id: uuid.UUID, org_id: uuid.UUID
) -> list[RegWatchAction]:
    result = await db.execute(
        select(RegWatchAction)
        .where(RegWatchAction.finding_id == finding_id, RegWatchAction.org_id == org_id)
        .order_by(RegWatchAction.created_at)
    )
    return list(result.scalars().all())


# ── Audit ────────────────────────────────────────────────────────────────────────


async def list_finding_audit(
    db: AsyncSession, finding_id: uuid.UUID, org_id: uuid.UUID, *, limit: int = 500
) -> list[AuditLog]:
    """What happened IN CONSIVA, from the shared append-only audit_logs. Distinct from
    `list_changes`, which is what changed in the world."""
    result = await db.execute(
        select(AuditLog)
        .where(
            AuditLog.org_id == org_id,
            AuditLog.entity_type == "regwatch_finding",
            AuditLog.entity_id == finding_id,
        )
        .order_by(AuditLog.created_at)
        .limit(limit)
    )
    return list(result.scalars().all())
