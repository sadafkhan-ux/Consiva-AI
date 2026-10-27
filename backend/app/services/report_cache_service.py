"""Pre-generated PDF reports: kept, checked for freshness, and shared between simultaneous requests.

A report is printed by headless Chromium (services/report_pdf_service.py), 1.5-3 s each. This keeps each finished PDF in the database (`report_pdfs`, migration
0043) beside a FINGERPRINT of everything the report is drawn from. A request is answered from the stored copy only when the fingerprint still matches the
scan's current one; any change (a new analysis run, a finding approved or rejected, a re-scan, a new day in India, a new scoring methodology or weights, a new
build of the web app) gives a different fingerprint, so the stored copy is ignored and the report is printed again. A download is never an out-of-date report.

Two more things keep it fast under load: identical requests that arrive together share ONE print (a click on a slow connection, a double click, two people
opening the same scan), and the worker prints a scan's reports as soon as its analysis finishes (job `render_reports`), so the first download is already waiting.
No model is called anywhere here; the report is drawn from stored data.
"""

import asyncio
import hashlib
import json
import logging
import time
import uuid
from datetime import timedelta

import httpx
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.core.tokens import issue_access_token
from app.db.models import (
    AgentRun,
    AgentRunStage,
    ConsentFinding,
    ConsentScan,
    ReportPdf,
    ScanScreenshot,
)
from app.db.session import async_session_factory
from app.scoring import dpdp_score
from app.services import report_pdf_service
from app.services.report_pdf_service import RenderedReport

logger = logging.getLogger(__name__)

#: Bump when the report's content changes in a way the fingerprint cannot see (a change to the web app is seen through its bundle names).
RENDER_VERSION = "1"
#: The reports a scan gets pre-generated, most used first.
PREWARM_VARIANTS = ("summary", "long", "full", "free")
_FRONTEND_SIGNATURE_TTL = 60.0
_frontend_signature: tuple[float, str] | None = None
_inflight: dict[tuple[str, str, str], asyncio.Task] = {}


async def frontend_signature(settings: Settings) -> str:
    """Identifies the web app that is printed: its index page names the hashed bundle files, so every build changes it. A report printed by an older build is
    therefore not served after a deploy. If the app cannot be reached the signature is 'unknown' (and the report cannot be printed either)."""
    global _frontend_signature
    now = time.monotonic()
    if _frontend_signature and now - _frontend_signature[0] < _FRONTEND_SIGNATURE_TTL:
        return _frontend_signature[1]
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            body = (await client.get(settings.report_frontend_url.rstrip("/") + "/")).content
        sig = hashlib.sha256(body).hexdigest()[:16]
    except Exception:  # noqa: BLE001 -- an unreachable web app only costs the cache
        sig = "unknown"
    _frontend_signature = (now, sig)
    return sig


def _digest(parts: list) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode("utf-8")).hexdigest()


async def fingerprint(db: AsyncSession, scan: ConsentScan, settings: Settings | None = None) -> str:
    """Everything the report of this scan is drawn from, reduced to one value. Four small queries."""
    settings = settings or get_settings()
    runs = (await db.execute(select(AgentRun.id, AgentRun.status, AgentRun.completed_at).where(AgentRun.scan_id == scan.id).order_by(AgentRun.id))).all()
    findings = (await db.execute(select(ConsentFinding.id, ConsentFinding.status).where(ConsentFinding.scan_id == scan.id).order_by(ConsentFinding.id))).all()
    stages = (await db.execute(
        select(AgentRunStage.stage, AgentRunStage.status, AgentRunStage.completed_at, AgentRunStage.agent_run_id)
        .where(AgentRunStage.scan_id == scan.id).order_by(AgentRunStage.created_at, AgentRunStage.stage))).all()
    shot = (await db.execute(select(ScanScreenshot.status, ScanScreenshot.captured_at).where(ScanScreenshot.scan_id == scan.id))).first()
    return _digest([
        RENDER_VERSION, str(scan.id), scan.status, scan.completed_at,
        [(str(r[0]), r[1], r[2]) for r in runs], [(str(f[0]), f[1]) for f in findings],
        [(s[0], s[1], s[2], str(s[3])) for s in stages], tuple(shot) if shot else None,
        dpdp_score.today_in_india().isoformat(), dpdp_score.METHODOLOGY_VERSION, settings.dpdp_score_weights or {},
        await frontend_signature(settings),
    ])


async def get_stored(db: AsyncSession, scan_id: uuid.UUID, variant: str, current: str) -> RenderedReport | None:
    """The stored PDF of this scan and report type, only if it was printed from exactly the current state."""
    row = (await db.execute(
        select(ReportPdf.fingerprint, ReportPdf.filename, ReportPdf.pdf).where(ReportPdf.scan_id == scan_id, ReportPdf.variant == variant))).first()
    if row is None or row[0] != current:
        return None
    return RenderedReport(pdf=bytes(row[2]), filename=row[1])


async def _store(scan_id: uuid.UUID, variant: str, current: str, rendered: RenderedReport, settings: Settings) -> None:
    """Keep the PDF (best effort: a failure here costs only the next download its speed) and drop copies older than the retention window."""
    try:
        async with async_session_factory() as db:
            await db.execute(delete(ReportPdf).where(ReportPdf.scan_id == scan_id, ReportPdf.variant == variant))
            db.add(ReportPdf(scan_id=scan_id, variant=variant, fingerprint=current, filename=rendered.filename, pdf=rendered.pdf, size_bytes=len(rendered.pdf)))
            await db.execute(delete(ReportPdf).where(ReportPdf.created_at < func.now() - timedelta(days=settings.report_pdf_retention_days)))
            await db.commit()
    except Exception:
        logger.warning("Could not keep the PDF report of scan %s (%s)", scan_id, variant, exc_info=True)


async def get_or_render(
    db: AsyncSession, scan: ConsentScan, variant: str, *, token: str, user_id: str, org_id: str, role: str | None, settings: Settings | None = None,
) -> tuple[RenderedReport, str]:
    """The report of one completed scan: the stored copy when it is current, otherwise printed now (once, however many ask at the same moment) and stored.
    Returns (report, "stored" | "printed")."""
    settings = settings or get_settings()
    scan_id = scan.id          # read now: the rollback below expires the row, and reading it afterwards would need the database again
    current = await fingerprint(db, scan, settings)
    stored = await get_stored(db, scan_id, variant, current)
    if stored is not None:
        return stored, "stored"

    # Give the database connection back BEFORE the slow part. Printing takes seconds, and the browser that prints calls this API (and so needs connections of
    # its own); a request that kept its connection while it waited starved the small pool and the print with it (measured: ten simultaneous downloads of an
    # unstored report exhausted the pool and failed).
    await db.rollback()

    key = (str(scan_id), variant, current)
    task = _inflight.get(key)
    if task is None:
        async def work() -> RenderedReport:
            try:
                rendered = await report_pdf_service.render_report_pdf(
                    scan_id, version=current, token=token, user_id=user_id, org_id=org_id, role=role, settings=settings, variant=variant)
                await _store(scan_id, variant, current, rendered, settings)
                return rendered
            finally:
                _inflight.pop(key, None)

        task = asyncio.ensure_future(work())
        _inflight[key] = task
    # shield: one caller going away must not cancel the print the others are waiting for
    return await asyncio.shield(task), "printed"


async def queue_prewarm(scan_id: uuid.UUID, org_id: uuid.UUID) -> None:
    """Queue the worker job that prints this scan's reports. Best effort: it never fails the caller, and a report that was not pre-generated is simply printed
    on the first download. Called when an analysis run ends (including when it pauses for human review, which is when the customer first looks at the report)
    and again when the review finishes, because approving or rejecting a finding changes what the report says."""
    from app.jobs import queue

    try:
        async with async_session_factory() as db:
            await queue.enqueue(db, org_id=org_id, job_type="render_reports", payload={"scan_id": str(scan_id)})
            await db.commit()
    except Exception:
        logger.warning("Could not queue the PDF pre-generation of scan %s", scan_id, exc_info=True)


async def _wait_until_quiet(scan_id: uuid.UUID, seconds: float = 20.0) -> None:
    """The job is queued from inside the analysis's last stage; wait until no stage of the scan is still running, so the fingerprint taken here is the one a
    later download will see (a report printed while a stage was still open would never match)."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        async with async_session_factory() as db:
            running = (await db.execute(select(func.count()).select_from(AgentRunStage).where(AgentRunStage.scan_id == scan_id, AgentRunStage.status == "running"))).scalar_one()
        if not running:
            return
        await asyncio.sleep(0.5)


async def prewarm(scan_id: uuid.UUID, variants: tuple[str, ...] = PREWARM_VARIANTS) -> dict[str, str]:
    """Print and keep the reports of a scan nobody has asked for yet. Runs in the worker, with a short-lived token for the scan's own organisation (the report
    page reads the scan through the same API as any user, so it needs one). Returns what happened per report type."""
    settings = get_settings()
    await _wait_until_quiet(scan_id)
    async with async_session_factory() as db:
        scan = await db.get(ConsentScan, scan_id)
        if scan is None or scan.status != "completed":
            return {}
        owner = str(scan.authorized_by_user_id) if scan.authorized_by_user_id else str(uuid.UUID(int=0))
        org = str(scan.org_id)
        token, _ = issue_access_token(user_id=owner, org_id=org, role="member", settings=settings, ttl_hours=1)
        out: dict[str, str] = {}
        for variant in variants:
            try:
                scan = await db.get(ConsentScan, scan_id)    # fresh each time: printing hands the connection back, which expires the row
                if scan is None:
                    break
                _, how = await get_or_render(db, scan, variant, token=token, user_id=owner, org_id=org, role="member", settings=settings)
                out[variant] = how
            except Exception as exc:  # noqa: BLE001 -- a pre-generation failure never fails a job that did its real work
                logger.warning("Pre-generating the %s report of scan %s failed: %s", variant, scan_id, type(exc).__name__)
                out[variant] = "failed"
        logger.info("Reports of scan %s: %s", scan_id, out)
        return out
