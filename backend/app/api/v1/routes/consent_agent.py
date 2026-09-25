"""The /consent-agent integration API.

A translation layer over the existing Consent Agent, not a second implementation of
it. Every request here ends up in the same `scan_service.request_scan` the console
uses, enqueued on the same `agent_jobs` queue, executed by the same worker, and read
back through the same row-level-security-scoped session. Nothing in this module
scans, classifies, retrieves or reasons.

What it adds is the contract an external caller needs and the console does not:
one call instead of two (the console drives crawl and analysis separately, an
integrator wants to POST a URL and poll), a single status field instead of three
that can disagree, an idempotency key, an hourly quota, optional webhooks, and a
stable machine-readable error envelope.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.schemas.consent_agent import (
    ApiError,
    CancelResponse,
    CreateScanRequest,
    CreateScanResponse,
    FindingsResponse,
    ScanListResponse,
    ScanResultResponse,
    ScanStatusResponse,
    SummaryResponse,
)
from app.config import get_settings
from app.core.exceptions import ConsivaError, NotFoundError, RateLimitExceededError
from app.core.security import CurrentUser, get_current_user
from app.db.session import get_db
from app.jobs import queue
from app.services import (
    audit_service,
    consent_agent_api_service as api_service,
    scan_service,
    webhook_service,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/consent-agent",
    tags=["Consent Agent API"],
    responses={
        400: {"model": ApiError, "description": "Malformed request or unsafe URL"},
        401: {"model": ApiError, "description": "Missing or invalid credentials"},
        403: {"model": ApiError, "description": "Not authorized for this domain or scan"},
        404: {"model": ApiError, "description": "No such scan in this organisation"},
        409: {"model": ApiError, "description": "Conflicts with the scan's current state"},
        429: {"model": ApiError, "description": "Rate limit or quota exceeded"},
    },
)


class ScanConflictError(ConsivaError):
    """The scan exists but is not in a state this operation accepts."""

    status_code = 409


class WebhookNotConfiguredError(ConsivaError):
    """A webhook was requested on a deployment that cannot sign one.

    501 rather than 400: the request is valid and the caller can do nothing to fix it.
    It is the server that is missing a capability, and saying 400 would send an
    integrator hunting for a mistake in their own payload.
    """

    status_code = 501


async def _load_scan(db: AsyncSession, scan_id: uuid.UUID, user: CurrentUser):
    """Fetch a scan, scoped to the caller's organisation.

    Tenant isolation here is belt AND braces: this query filters on org_id, and the
    session it runs in is subject to row-level security (migration 0018) which makes a
    foreign-organisation row unreadable even if this filter were ever dropped. A caller
    asking for another tenant's scan gets 404, not 403 -- confirming existence would
    leak that the id is real.
    """
    scan = await scan_service.scan_repository.get_scan(db, scan_id, uuid.UUID(user.org_id))
    if scan is None:
        raise NotFoundError(f"Scan {scan_id} not found")
    return scan


def _clamp_options(body: CreateScanRequest) -> dict:
    """The caller's requested options, reduced to what the server will actually do.

    Clamped rather than rejected: an integrator asking for 100 pages on a deployment
    configured for 25 wants a scan, not a 400. The effective values are stored on the
    scan and returned in the result, so the response always says what really ran
    instead of echoing back what was asked for.
    """
    settings = get_settings()
    requested = body.scan_options
    max_pages = min(requested.max_pages or settings.scanner_max_pages, settings.scanner_max_pages)
    states = requested.scan_consent_states
    return {
        "max_pages": max_pages,
        "scan_consent_states": states,
        # The three per-state flags are only meaningful when consent-state testing is
        # on at all; an option set that says "don't test consent states, but do test
        # after accept" is contradictory, and resolving it here keeps that ambiguity
        # out of the scanner.
        "scan_before_consent": bool(requested.scan_before_consent),
        "scan_after_accept": bool(states and requested.scan_after_accept),
        "scan_after_reject": bool(states and requested.scan_after_reject),
    }


@router.post(
    "/scans",
    response_model=CreateScanResponse,
    status_code=202,
    summary="Start a consent scan",
    description=(
        "Queues a scan and returns immediately with a `scan_id`. The browser crawl and "
        "the analysis both run in the background on the existing job worker -- this "
        "request never waits for them.\n\n"
        "Send an `Idempotency-Key` header to make retries safe: a repeat with the same "
        "key returns the original scan instead of paying for a second crawl.\n\n"
        "`authorized` must be true. It is an explicit attestation that you own or are "
        "permitted to scan the domain."
    ),
)
async def create_scan(
    body: CreateScanRequest,
    response: Response,
    request: Request,
    idempotency_key: str | None = Header(
        default=None, alias="Idempotency-Key",
        description="Opaque caller-chosen string, unique per intended scan. Scoped to "
                    "your organisation.",
    ),
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CreateScanResponse:
    org_id = uuid.UUID(user.org_id)

    if idempotency_key is not None:
        existing = await api_service.find_by_idempotency_key(db, org_id=org_id, key=idempotency_key)
        if existing is not None:
            # 200, not 202: nothing was accepted for processing by THIS request.
            response.status_code = 200
            status = await api_service.get_status(db, existing)
            logger.info(
                "consent_agent.scan.idempotent_replay scan_id=%s org_id=%s key=%s",
                existing.id, org_id, idempotency_key,
            )
            return CreateScanResponse(
                scan_id=existing.id, status=status["status"],
                message="Existing scan returned for this Idempotency-Key.",
                idempotent_replay=True,
            )

    settings = get_settings()
    # Hourly quota, on top of the per-day ceiling scan_service already enforces. A
    # browser crawl plus an LLM call is expensive enough that an integrator looping on
    # this endpoint is a capacity problem long before the daily limit notices.
    recent = await api_service.count_scans_since(
        db, org_id=org_id, since=datetime.now(UTC) - timedelta(hours=1)
    )
    if recent >= settings.consent_api_scans_per_hour:
        raise RateLimitExceededError(
            f"Scan quota reached ({settings.consent_api_scans_per_hour}/hour for this "
            "organisation). Retry later or contact support to raise the limit."
        )

    if body.webhook_url:
        # Checked before the scan is accepted, not at delivery time twenty minutes
        # later. Without a signing secret the delivery would be refused (an unsigned
        # webhook is one the receiver cannot authenticate), so accepting the scan would
        # mean promising a callback that is never coming.
        if not webhook_service.is_configured():
            raise WebhookNotConfiguredError(
                "Webhook callbacks are not available on this deployment: no signing "
                "secret is configured, and an unsigned callback cannot be "
                "authenticated by the receiver. Omit webhook_url and poll "
                "GET /scans/{scan_id}/status instead."
            )
        # Validated now so a bad target is a 4xx the caller can fix. Re-checked at
        # delivery time too, because DNS can be repointed in between.
        try:
            await webhook_service.validate_target(body.webhook_url)
        except ValueError as exc:
            raise ConsivaError(str(exc)) from exc

    options = _clamp_options(body)

    # The existing service. It owns the authorization attestation, the per-day rate
    # limit, domain-verification policy, the SSRF check on the target URL, the audit
    # entry and the queue write -- none of which is reimplemented here.
    scan = await scan_service.request_scan(
        db, org_id=org_id, user_id=uuid.UUID(user.user_id),
        url=body.website_url, authorized=body.authorized,
        # This API queues ONE job that does the crawl and the analysis together, so
        # request_scan must not also queue its own crawl -- that ran the whole scan
        # twice on the first live call.
        enqueue_job=False,
    )

    scan.idempotency_key = idempotency_key
    scan.webhook_url = body.webhook_url
    scan.scan_options = options
    # `auto_analyze` is what makes this one call instead of two: the worker reads it
    # after a successful crawl and enqueues the analysis itself. The console flow does
    # not set it and is unaffected.
    await queue.enqueue(
        db, org_id=org_id, job_type="consent_api_chain",
        payload={"scan_id": str(scan.id), "org_id": str(org_id), "user_id": str(user.user_id)},
    )
    await audit_service.record(
        db, org_id=org_id, actor_user_id=uuid.UUID(user.user_id),
        action="consent_api.scan.requested", entity_type="consent_scan", entity_id=scan.id,
    )
    await db.commit()

    logger.info(
        "consent_agent.scan.queued scan_id=%s org_id=%s user_id=%s url=%s max_pages=%s webhook=%s",
        scan.id, org_id, user.user_id, body.website_url, options["max_pages"], bool(body.webhook_url),
    )
    return CreateScanResponse(
        scan_id=scan.id, status="queued", message="Scan queued successfully",
    )


@router.get(
    "/scans",
    response_model=ScanListResponse,
    summary="List your scans",
    description=(
        "Recent scans for your organisation, newest first.\n\n"
        "Summary rows only -- no evidence or findings bodies. Fetch a scan by id for "
        "those. Filter with `status`, page with `limit`/`offset`."
    ),
)
async def list_scans(
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    status: str | None = Query(
        default=None,
        description="Filter by the SCAN's stored state (pending, running, completed, "
                    "failed, cancelled).",
    ),
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ScanListResponse:
    rows, total = await api_service.list_scans(
        db, org_id=uuid.UUID(user.org_id), limit=limit, offset=offset, status=status
    )
    return ScanListResponse(scans=rows, total=total, limit=limit, offset=offset)


@router.get(
    "/scans/{scan_id}/status",
    response_model=ScanStatusResponse,
    summary="Poll scan status",
    description=(
        "Cheap to poll. `progress` is computed from completed pipeline stages, never "
        "from elapsed time, and reaches 100 only when the scan is in a terminal state.\n\n"
        "`status` is a single value derived from the platform's separate crawl and "
        "analysis states. A failed analysis still reports `completed` when the "
        "deterministic rules produced findings -- `error.recoverable` says so."
    ),
)
async def get_scan_status(
    scan_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ScanStatusResponse:
    scan = await _load_scan(db, scan_id, user)
    return ScanStatusResponse(**await api_service.get_status(db, scan))


@router.get(
    "/scans/{scan_id}",
    response_model=ScanResultResponse,
    summary="Full scan result",
    description=(
        "Everything the pipeline produced: evidence, consent-state breakdown, findings, "
        "per-stage timings and real measured token usage. Poll `/status` until "
        "`completed` before calling this -- it is valid earlier but will be partial."
    ),
)
async def get_scan_result(
    scan_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ScanResultResponse:
    scan = await _load_scan(db, scan_id, user)
    return ScanResultResponse(**await api_service.get_result(db, scan))


@router.get(
    "/scans/{scan_id}/findings",
    response_model=FindingsResponse,
    summary="Findings only",
    description=(
        "The compliance findings, without the evidence payload.\n\n"
        "`confidence` is null unless the pipeline recorded one -- it is never filled "
        "in with a plausible default. `requires_human_review` is true on every "
        "high-severity finding regardless of what the model asked for."
    ),
)
async def get_scan_findings(
    scan_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FindingsResponse:
    scan = await _load_scan(db, scan_id, user)
    return FindingsResponse(**await api_service.get_findings(db, scan))


@router.get(
    "/scans/{scan_id}/summary",
    response_model=SummaryResponse,
    summary="Dashboard summary",
    description=(
        "Compact counts for a dashboard card.\n\n"
        "`consent_states_tested.accept`/`.reject` are true only when the control was "
        "actually operated -- an attempted click that failed reads false, because its "
        "observations do not establish the consent state they are named after."
    ),
)
async def get_scan_summary(
    scan_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SummaryResponse:
    scan = await _load_scan(db, scan_id, user)
    return SummaryResponse(**await api_service.get_summary(db, scan))


@router.post(
    "/scans/{scan_id}/cancel",
    response_model=CancelResponse,
    summary="Cancel a scan",
    description=(
        "Cancels a queued or running scan. Queued jobs are removed before they start.\n\n"
        "A crawl already in flight finishes its current page -- the browser runs in an "
        "isolated subprocess under its own time budget, and killing it mid-navigation "
        "is what leaves orphaned Chromium processes. Whatever evidence it collected is "
        "kept; no further analysis is queued."
    ),
)
async def cancel_scan(
    scan_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CancelResponse:
    scan = await _load_scan(db, scan_id, user)
    if scan.status in ("completed", "failed", "cancelled"):
        raise ScanConflictError(
            f"Scan {scan_id} is already {scan.status} and cannot be cancelled."
        )

    removed = await queue.cancel_jobs_for_scan(db, scan_id=scan_id, org_id=uuid.UUID(user.org_id))
    await api_service.mark_cancelled(db, scan)
    await audit_service.record(
        db, org_id=uuid.UUID(user.org_id), actor_user_id=uuid.UUID(user.user_id),
        action="consent_api.scan.cancelled", entity_type="consent_scan", entity_id=scan.id,
    )
    await db.commit()

    logger.info(
        "consent_agent.scan.cancelled scan_id=%s org_id=%s jobs_removed=%s",
        scan_id, user.org_id, removed,
    )
    return CancelResponse(
        scan_id=scan.id, status="cancelled",
        message=(
            f"Scan cancelled; {removed} queued job(s) removed. Work already in flight "
            "finishes its current step and is then discarded."
        ),
    )
