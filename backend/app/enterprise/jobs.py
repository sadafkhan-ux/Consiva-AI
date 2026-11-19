"""The background job for an Enterprise scan. Registered with the worker under the job type `enterprise_scan` (jobs/worker.py)."""

from __future__ import annotations

import logging
import uuid

from app.enterprise import pipeline
from app.enterprise.crawl import runner

logger = logging.getLogger(__name__)
JOB_TYPE = "enterprise_scan"


async def run_enterprise_scan(payload: dict) -> None:
    """Crawl one scan, then analyse it, then print its report: the score and the PDF only after the whole workflow has run (owner's sequence, 19 Nov 2026). Calling it again for the same scan
    resumes it, so a job the worker re-runs after a crash is safe: the crawl carries on from its frontier, and the analysis continues by chunk. A scan that was cancelled or whose crawl
    failed (blocked included) is not analysed; its evidence is kept and it can be resumed."""
    scan_id, org_id = uuid.UUID(payload["scan_id"]), uuid.UUID(payload["org_id"])
    status = await runner.run(scan_id, org_id)
    if status != "completed" and not await _blocked_with_pages(scan_id, org_id):
        logger.info("enterprise scan %s ended as %s: no analysis", scan_id, status)
        return
    # A scan the website blocked part-way is still analysed and reported for the pages that WERE inspected (owner's decision, 19 Nov 2026), and labelled as blocked and incomplete
    # everywhere (the coverage declaration and the report): it is never presented as a website-wide assessment. Resuming it re-runs the analysis and the report.
    analysis = await pipeline.run_analysis_phase(scan_id, org_id)
    if analysis in ("completed", "partial"):
        await _generate_report(scan_id, org_id)


async def _blocked_with_pages(scan_id: uuid.UUID, org_id: uuid.UUID) -> bool:
    """The crawl was stopped because the website blocked the scanner, and at least one page was inspected before that."""
    from sqlalchemy import text

    from app.db.session import async_session_factory, set_org_scope
    from app.enterprise import scans
    set_org_scope(org_id)
    async with async_session_factory() as db:
        scan = await scans.get_scan(db, scan_id, org_id)
        if scan is None or not scans.is_blocked_stop(scan):
            return False
        done = await db.scalar(text("select count(*) from ent_frontier where scan_id = :s and status = 'done'"), {"s": scan_id})
    return bool(done)


async def _generate_report(scan_id: uuid.UUID, org_id: uuid.UUID) -> None:
    """The report version of the finished workflow. A failure is recorded as a failed report version (report/service.py) and logged; it never undoes the scan or its score."""
    from app.db.session import async_session_factory, set_org_scope
    from app.enterprise.report.service import generate_report
    try:
        set_org_scope(org_id)
        async with async_session_factory() as db:
            row = await generate_report(db, scan_id)
            logger.info("enterprise scan %s: report version %s %s (%s pages)", scan_id, row.version, row.status, row.page_count)
    except Exception:
        logger.exception("enterprise scan %s: the report could not be generated after the analysis", scan_id)
