"""Generates an Enterprise report version and stores it. Each run is a NEW row of `ent_reports` (version = highest + 1); an earlier version is never overwritten.

A failure is recorded as a row with status `failed` and the error, and that row is returned: nothing is swallowed, and the caller sees the failure in the returned row.
"""

from __future__ import annotations

import hashlib
import logging
import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.enterprise.models import EntReport
from app.enterprise.report.data import ReportData, gather
from app.enterprise.report.html import render_html
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


async def generate_report(db: AsyncSession, scan_id: uuid.UUID) -> EntReport:
    """Gather, render, print and store a new report version. Returns the stored row: status `ready` with the PDF, or `failed` with the error."""
    data: ReportData | None = None
    try:
        data = await gather(db, scan_id)
        html = render_html(data)
        pdf, pages = await render_pdf(html)
    except Exception as exc:
        logger.exception("enterprise report for scan %s failed", scan_id)
        await db.rollback()
        return await _store(db, scan_id, status="failed", error=f"{type(exc).__name__}: {exc}"[:1000])
    score_id = uuid.UUID(data.score.id) if data.score and data.score.id else None
    return await _store(db, scan_id, score_result_id=score_id, status="ready", pdf=pdf, sha256=hashlib.sha256(pdf).hexdigest(), byte_size=len(pdf), page_count=pages)


async def latest_report(db: AsyncSession, scan_id: uuid.UUID) -> EntReport | None:
    """The newest ready version (with its PDF), or None if no version has been generated successfully."""
    return await db.scalar(select(EntReport).where(EntReport.scan_id == scan_id, EntReport.status == "ready").order_by(EntReport.version.desc()).limit(1))
