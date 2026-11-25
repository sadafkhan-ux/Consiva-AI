"""Enterprise scan endpoints: start, status, coverage, cancel, resume. Prefix /api/v1/enterprise (router.py).

Nothing here crawls. The request is validated, the scan row is written, and a background job does the work; every read is scoped to the caller's organisation and a scan of another
organisation is "not found", as everywhere else in this API.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.exceptions import NotFoundError
from app.core.integration_auth import get_application_scan_caller, get_caller
from app.core.security import CurrentUser
from app.db.models import Website
from app.db.session import get_db
from app.enterprise import errors, scans, store
from app.enterprise.api.schemas import (
    ActionResponse,
    CoverageResponse,
    CoverageUrl,
    CreateEnterpriseScan,
    CreateEnterpriseScanResponse,
    EnterpriseScanStatus,
    UrlCounts,
)
from app.enterprise.config import get_enterprise_settings
from app.enterprise.crawl import frontier
from app.enterprise.jobs import JOB_TYPE
from app.enterprise.models import EntFrontier
from app.jobs import queue
from app.scanner._domain import site_host

router = APIRouter(prefix="/scans", tags=["Enterprise scanner"])


def _require_enabled() -> None:
    if not get_enterprise_settings().enabled:
        raise errors.EnterpriseDisabled("Not found")


def _counts(raw: dict[str, int]) -> UrlCounts:
    return UrlCounts(**raw, total=sum(raw.values()))


def _progress(c: UrlCounts) -> int:
    return 0 if c.total == 0 else round(100 * (c.done + c.failed + c.excluded) / c.total)


@router.post("", response_model=CreateEnterpriseScanResponse, status_code=202, summary="Start a full-website scan")
async def create_scan(
    body: CreateEnterpriseScan,
    user: CurrentUser = Depends(get_application_scan_caller),
    db: AsyncSession = Depends(get_db),
) -> CreateEnterpriseScanResponse:
    _require_enabled()
    # Same gate as the application profile: a person must be an administrator (a service key has already been checked for its scope).
    if user.key_name is None and user.role != "admin":
        raise errors.EnterpriseNotPermitted(
            "Full-site scans are limited to organisation administrators and to service keys granted the consent:scan:application scope.")
    if not body.authorized:
        raise errors.AttestationRequired("`authorized` must be true: it attests that you own or may scan this domain.")
    org_id = uuid.UUID(user.org_id)
    # The same rule as the 5-page scan: DNS ownership verification is required only when the deployment says so (SCANNER_REQUIRE_DOMAIN_VERIFICATION). The `authorized` attestation above
    # is always required, and the address is always checked for safety (SSRF) before anything is fetched.
    if get_settings().scanner_require_domain_verification:
        verified = await db.scalar(select(Website.id).where(
            Website.org_id == org_id, Website.domain == site_host(body.website_url), Website.verified_at.is_not(None)))
        if verified is None:
            raise errors.DomainNotVerified("A full-site scan needs a domain that has passed DNS ownership verification.")
    try:
        scan = await scans.create_scan(db, org_id=org_id, website_url=body.website_url, created_by_user_id=uuid.UUID(user.user_id))
    except ValueError as exc:
        raise errors.AttestationRequired(str(exc)) from exc
    await queue.enqueue(db, org_id=org_id, job_type=JOB_TYPE, payload={"scan_id": str(scan.id), "org_id": str(org_id)})
    await store.audit(db, scan.id, user.user_id, "scan_created", {"website_url": body.website_url, "options": scan.options})
    await db.commit()
    return CreateEnterpriseScanResponse(scan_id=scan.id, status=scan.status, message="Enterprise scan queued", options=scan.options)


async def _load(db: AsyncSession, scan_id: uuid.UUID, user: CurrentUser):
    _require_enabled()
    scan = await scans.get_scan(db, scan_id, uuid.UUID(user.org_id))
    if scan is None:
        raise NotFoundError(f"Enterprise scan {scan_id} not found")
    return scan


@router.get("/{scan_id}/status", response_model=EnterpriseScanStatus, summary="Status and URL counts")
async def get_status(scan_id: uuid.UUID, user: CurrentUser = Depends(get_caller), db: AsyncSession = Depends(get_db)) -> EnterpriseScanStatus:
    scan = await _load(db, scan_id, user)
    c = _counts(await frontier.counts(db, scan_id))
    return EnterpriseScanStatus(
        scan_id=scan.id, website_url=scan.website_url, status=scan.status, urls=c, progress=_progress(c), stop_reason=scan.stop_reason,
        error=scan.error, options=scan.options, created_at=scan.created_at, started_at=scan.started_at, completed_at=scan.completed_at,
        data_cleared=scans.results_cleared(scan))


@router.get("/{scan_id}/coverage", response_model=CoverageResponse, summary="Every discovered URL and what became of it")
async def get_coverage(
    scan_id: uuid.UUID,
    status: str | None = Query(default=None, pattern="^(pending|in_progress|done|failed|excluded)$"),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    user: CurrentUser = Depends(get_caller),
    db: AsyncSession = Depends(get_db),
) -> CoverageResponse:
    scan = await _load(db, scan_id, user)
    if scans.results_cleared(scan):                      # the URL queue was deleted with the results
        from app.enterprise.api.results import cleared_message
        raise errors.ScanResultsCleared(cleared_message(scan))
    where = [EntFrontier.scan_id == scan_id] + ([EntFrontier.status == status] if status else [])
    total = await db.scalar(select(func.count()).select_from(EntFrontier).where(*where)) or 0
    rows = (await db.scalars(
        select(EntFrontier).where(*where).order_by(EntFrontier.depth, EntFrontier.discovered_at, EntFrontier.id).limit(limit).offset(offset))).all()
    items = [
        CoverageUrl(url=r.original_url, depth=r.depth, status=r.status, discovery_sources=list(r.discovery_sources or []), http_status=r.http_status,
                    status_reason=r.status_reason, last_error=r.last_error)
        for r in rows
    ]
    return CoverageResponse(scan_id=scan_id, urls=_counts(await frontier.counts(db, scan_id)), items=items, total=total, limit=limit, offset=offset)


@router.post("/{scan_id}/cancel", response_model=ActionResponse, summary="Cancel a scan")
async def cancel(scan_id: uuid.UUID, user: CurrentUser = Depends(get_caller), db: AsyncSession = Depends(get_db)) -> ActionResponse:
    scan = await _load(db, scan_id, user)
    if scan.status not in ("queued", "running"):
        raise errors.InvalidScanState(f"A {scan.status} scan cannot be cancelled.")
    await scans.transition(db, scan_id, uuid.UUID(user.org_id), "cancelled", stop_reason="cancelled")
    await store.audit(db, scan_id, user.user_id, "scan_cancelled", {})
    await db.commit()
    return ActionResponse(scan_id=scan_id, status="cancelled", message="Scan cancelled. Evidence already collected is kept.")


@router.post("/{scan_id}/resume", response_model=ActionResponse, status_code=202, summary="Carry on a scan that stopped before it finished")
async def resume(scan_id: uuid.UUID, user: CurrentUser = Depends(get_application_scan_caller), db: AsyncSession = Depends(get_db)) -> ActionResponse:
    """For a scan still `running` whose worker is gone, one the website stopped by blocking the scanner, or one a time or memory limit stopped with pages still waiting: queues it again.
    Pages already done are not visited again; pages the dead worker held, and pages the site refused, are taken back. A scan stopped at the page limit (url_limit) is not resumed: it
    would stop again at once, so scan again with a higher limit."""
    scan = await _load(db, scan_id, user)
    org_id = uuid.UUID(user.org_id)
    if scans.is_blocked_stop(scan):
        reopened = await frontier.reopen_blocked(db, scan_id)
        scan = await scans.reopen_blocked(db, scan_id, org_id)
        await store.audit(db, scan_id, user.user_id, "scan_reopened_after_block", {"pages_requeued": reopened})
    elif scans.is_resumable_limit_stop(scan):
        limit = scan.stop_reason
        waiting = await frontier.reopen_unreached(db, scan_id)
        scan = await scans.reopen_limit_stopped(db, scan_id, org_id)
        await store.audit(db, scan_id, user.user_id, "scan_reopened_after_limit", {"stop_reason": limit, "pages_waiting": waiting})
    elif scan.status not in ("queued", "running"):
        raise errors.InvalidScanState(f"A {scan.status} scan cannot be resumed.")
    await queue.enqueue(db, org_id=org_id, job_type=JOB_TYPE, payload={"scan_id": str(scan_id), "org_id": str(org_id)})
    await store.audit(db, scan_id, user.user_id, "scan_resumed", {})
    await db.commit()
    return ActionResponse(scan_id=scan_id, status=scan.status, message="Scan queued to continue from where it stopped.")
