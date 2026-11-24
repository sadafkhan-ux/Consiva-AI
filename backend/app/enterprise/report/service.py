"""Generates an Enterprise report version and stores it. Each run is a NEW row of `ent_reports` (version = highest + 1); an earlier version is never overwritten.

A failure is recorded as a row with status `failed` and the error, and that row is returned: nothing is swallowed, and the caller sees the failure in the returned row.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from urllib.parse import urlparse

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.enterprise.models import EntReport
from app.enterprise.report.data import ReportData, gather
from app.enterprise.report.html import MAX_LEVEL, MAX_PAGES, render_html
from app.enterprise.report.pdf import render_pdf

logger = logging.getLogger(__name__)
VERSION_RETRIES = 3


async def _next_version(db: AsyncSession, scan_id: uuid.UUID) -> int:
    latest = await db.scalar(select(EntReport.version).where(EntReport.scan_id == scan_id).order_by(EntReport.version.desc()).limit(1))
    return (latest or 0) + 1


async def _store(db: AsyncSession, scan_id: uuid.UUID, **fields) -> EntReport:
    """Insert a new version row. Two generators racing for the same version collide on the unique key: the loser takes the next number."""
    for attempt in range(VERSION_RETRIES):
        row = EntReport(scan_id=scan_id, version=await _next_version(db, scan_id), **fields)
        try:
            async with db.begin_nested():
                db.add(row)
                await db.flush()
        except IntegrityError:
            if attempt == VERSION_RETRIES - 1:
                raise
            continue
        await db.commit()
        return row
    raise RuntimeError("unreachable")


async def render_within_limit(data: ReportData) -> tuple[bytes, int]:
    """Print the report, tightening the long lists one level at a time until it fits MAX_PAGES (40). Every capped list states its full size and the API that returns all of it, so a
    tighter level drops rows from the PDF, never from the record. If even the tightest level is longer, that version is kept and the overrun is logged."""
    pdf, pages = b"", 0
    for level in range(MAX_LEVEL + 1):
        pdf, pages = await render_pdf(render_html(data, level=level))
        if pages <= MAX_PAGES:
            return pdf, pages
    logger.warning("enterprise report for scan %s is %d pages at the tightest level (limit %d)", data.scan.scan_id, pages, MAX_PAGES)
    return pdf, pages


async def _generate_method3(db: AsyncSession, scan_id: uuid.UUID) -> EntReport | None:
    """The method 3.0.0 report (owner's approval of 20 Nov 2026), when it is the configured report and the scan has a method 3.0.0 result. None means: print the 51-parameter report."""
    from app.enterprise.config import get_enterprise_settings
    from app.enterprise.method3 import service as method3
    from app.enterprise.report import method3 as m3report
    if get_enterprise_settings().report_method != "3":
        return None
    if await method3.latest(db, scan_id) is None:
        # The report is the 96-parameter report only (owner's decision, 24 Nov 2026): a scan with no 96-parameter result yet is scored now, and if that cannot be done the
        # report fails with the reason. It never falls back to the 51-parameter report.
        org_id = await db.scalar(text("select org_id from ent_scans where id = :s"), {"s": scan_id})
        if org_id is not None:
            await method3.run_safe(scan_id, org_id, actor="report")
        if await method3.latest(db, scan_id) is None:
            return await _store(db, scan_id, status="failed", error="The 96-parameter (method 3) score could not be calculated for this scan, so no report was printed. Re-run the analysis, then generate the report again.")
    try:
        data = await m3report.gather(db, scan_id)
        header, footer = m3report.header_footer(urlparse(data.site).netloc or data.site, data.result.get("catalogue_version", ""), data.scan_id)
        pdf, pages = await render_pdf(m3report.render_html(data), header=header, footer=footer, margin=m3report.MARGIN)
    except Exception as exc:
        logger.exception("enterprise method 3.0.0 report for scan %s failed", scan_id)
        await db.rollback()
        return await _store(db, scan_id, status="failed", error=f"method 3.0.0 report: {type(exc).__name__}: {exc}"[:1000])
    from app.enterprise import store
    score = await store.latest_score(db, scan_id)
    return await _store(db, scan_id, score_result_id=score.id if score is not None else None, status="ready", pdf=pdf, sha256=hashlib.sha256(pdf).hexdigest(), byte_size=len(pdf),
                        page_count=pages)


async def generate_report(db: AsyncSession, scan_id: uuid.UUID) -> EntReport:
    """Gather, render, print and store a new report version. Returns the stored row: status `ready` with the PDF, or `failed` with the error."""
    m3 = await _generate_method3(db, scan_id)
    if m3 is not None:
        return m3
    data: ReportData | None = None
    try:
        data = await gather(db, scan_id)
        pdf, pages = await render_within_limit(data)
    except Exception as exc:
        logger.exception("enterprise report for scan %s failed", scan_id)
        await db.rollback()
        return await _store(db, scan_id, status="failed", error=f"{type(exc).__name__}: {exc}"[:1000])
    score_id = uuid.UUID(data.score.id) if data.score and data.score.id else None
    return await _store(db, scan_id, score_result_id=score_id, status="ready", pdf=pdf, sha256=hashlib.sha256(pdf).hexdigest(), byte_size=len(pdf), page_count=pages)


async def latest_report(db: AsyncSession, scan_id: uuid.UUID) -> EntReport | None:
    """The newest ready version (with its PDF), or None if no version has been generated successfully."""
    return await db.scalar(select(EntReport).where(EntReport.scan_id == scan_id, EntReport.status == "ready").order_by(EntReport.version.desc()).limit(1))
