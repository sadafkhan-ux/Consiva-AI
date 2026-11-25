"""Enterprise scan records (ent_scans): create one, and move it through its lifecycle.

Lifecycle: queued -> running -> completed | failed | cancelled. A completed, failed or cancelled scan is final, with two exceptions that `resume` reopens (back to queued, carrying on from
the frontier): a failed scan the website blocked (reopen_blocked) and a completed scan a time or memory limit stopped (reopen_limit_stopped). A failure always carries its reason.
"""

from __future__ import annotations

import uuid
from urllib.parse import urlparse

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.enterprise.config import get_enterprise_settings
from app.enterprise.models import EntScan

TRANSITIONS: dict[str, frozenset[str]] = {
    "queued": frozenset({"running", "cancelled"}),
    "running": frozenset({"completed", "failed", "cancelled"}),
    "completed": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
}


class InvalidTransition(ValueError):
    pass


def scan_options() -> dict:
    """The limits a scan runs under, taken from the settings when it is created and stored on the scan, so the record says what really applied."""
    s = get_enterprise_settings()
    return {
        "max_urls": s.max_urls, "max_depth": s.max_depth, "concurrency": s.concurrency, "per_domain_rps": s.per_domain_rps,
        "memory_budget_mb": s.memory_budget_mb, "max_runtime_seconds": s.max_runtime_seconds, "max_attempts": s.max_attempts,
    }


async def create_scan(db: AsyncSession, *, org_id: uuid.UUID, website_url: str, created_by_user_id: uuid.UUID | None = None) -> EntScan:
    parsed = urlparse(website_url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("website_url must be an absolute http(s) URL")
    scan = EntScan(org_id=org_id, website_url=website_url, domain=parsed.hostname.lower(), options=scan_options(), created_by_user_id=created_by_user_id)
    db.add(scan)
    await db.flush()
    return scan


async def get_scan(db: AsyncSession, scan_id: uuid.UUID, org_id: uuid.UUID) -> EntScan | None:
    """Scoped to the organisation, as every scan read is: another organisation's scan is simply not found."""
    return await db.scalar(select(EntScan).where(EntScan.id == scan_id, EntScan.org_id == org_id))


async def transition(db: AsyncSession, scan_id: uuid.UUID, org_id: uuid.UUID, to: str, *, error: str | None = None, stop_reason: str | None = None) -> EntScan:
    scan = await get_scan(db, scan_id, org_id)
    if scan is None:
        raise LookupError(f"Enterprise scan {scan_id} not found")
    if to not in TRANSITIONS[scan.status]:
        raise InvalidTransition(f"a {scan.status} scan cannot become {to}")
    if to == "failed" and not error:
        raise ValueError("a failed scan always says why")
    values: dict = {"status": to, "updated_at": func.now()}
    if to == "running":
        values["started_at"] = func.now()
    if to in ("completed", "failed", "cancelled"):
        values["completed_at"] = func.now()
    if error is not None:
        values["error"] = error
    if stop_reason is not None:
        values["stop_reason"] = stop_reason
    await db.execute(update(EntScan).where(EntScan.id == scan_id, EntScan.org_id == org_id).values(**values))
    await db.refresh(scan)
    return scan


#: The stop reasons of a crawl the website's protection stopped (BlockVerdict.kind, lower-cased). Only such a failed scan can be resumed: nothing it collected was wrong, it was refused.
BLOCK_STOPS = ("bot_blocked", "access_blocked")


def is_blocked_stop(scan: EntScan) -> bool:
    return scan.status == "failed" and (scan.stop_reason or "").lower() in BLOCK_STOPS


async def reopen_blocked(db: AsyncSession, scan_id: uuid.UUID, org_id: uuid.UUID) -> EntScan:
    """Resume a scan the website blocked: it becomes queued again with its error cleared, and its frontier keeps every page already inspected (crawl/frontier.reopen_blocked puts the refused
    pages back). This is the one way out of `failed`, and only for a block; every other final state stays final."""
    scan = await get_scan(db, scan_id, org_id)
    if scan is None:
        raise LookupError(f"Enterprise scan {scan_id} not found")
    if not is_blocked_stop(scan):
        raise InvalidTransition(f"a {scan.status} scan that was not stopped by blocking cannot be reopened")
    await db.execute(update(EntScan).where(EntScan.id == scan_id, EntScan.org_id == org_id).values(
        status="queued", error=None, stop_reason=None, completed_at=None, analysis_status="not_started", analysis_error=None, updated_at=func.now()))
    await db.refresh(scan)
    return scan


#: The stop reasons of a crawl a run limit ended (crawl/limits.py) with URLs still pending. Such a completed scan can be resumed: the next run gets its own runtime and memory budget and
#: carries on from the frontier. `url_limit` is NOT here: the page limit counts every page the scan has read, so a resumed run would stop again at once; scan again with a higher limit.
RESUMABLE_LIMIT_STOPS = ("time_limit", "memory_limit")

#: The mark the scan-data cleanup leaves on a scan whose results it deleted (the same value as app/cleanup/registry.py in the privacy-policy-agent copy). The shared cleanup here does not
#: write it yet, so in this copy no scan reads as cleared; the resume rule still refuses a scan whose page text is gone (text_cleared_at).
FULL_CLEAR_MARK = "local scan-data cleanup"


def results_cleared(scan: EntScan) -> bool:
    """True once the scan-data cleanup has deleted this scan's results; only its history row (website, status, times) is left."""
    return bool(scan.purge_summary) and scan.purge_summary.get("cleared_by") == FULL_CLEAR_MARK


def is_resumable_limit_stop(scan: EntScan) -> bool:
    return (scan.status == "completed" and (scan.stop_reason or "").lower() in RESUMABLE_LIMIT_STOPS and scan.analysis_status != "running"
            and not results_cleared(scan) and scan.text_cleared_at is None)          # the analysis is made again from the stored page text, so it must still be there


async def reopen_limit_stopped(db: AsyncSession, scan_id: uuid.UUID, org_id: uuid.UUID) -> EntScan:
    """Resume a scan a run limit stopped: it becomes queued again, keeps every page already inspected, and its analysis, score and report are made again when the crawl finishes
    (the earlier ones stay in their history)."""
    scan = await get_scan(db, scan_id, org_id)
    if scan is None:
        raise LookupError(f"Enterprise scan {scan_id} not found")
    if not is_resumable_limit_stop(scan):
        raise InvalidTransition(f"a {scan.status} scan that was not stopped by a time or memory limit cannot be reopened")
    await db.execute(update(EntScan).where(EntScan.id == scan_id, EntScan.org_id == org_id).values(
        status="queued", error=None, stop_reason=None, completed_at=None, analysis_status="not_started", analysis_error=None, updated_at=func.now()))
    await db.refresh(scan)
    return scan
