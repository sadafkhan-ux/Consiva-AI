"""Enterprise results endpoints: summary, coverage declaration, parameters, findings, evidence, URL and document inventories, score history, change detection, analysis and report.
Prefix /api/v1/enterprise/scans (router.py). The shapes are documented in docs/ENTERPRISE_API_CONTRACT.md.

Every route is scoped to the caller's organisation: a scan of another organisation is "not found". All lists are paginated. Counts are actual counts; what cannot be known yet is null.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConsivaError, NotFoundError
from app.core.integration_auth import get_application_scan_caller, get_caller
from app.core.security import CurrentUser
from app.db.session import get_db
from app.enterprise import changes, errors, pipeline, scans, store
from app.enterprise.config import get_enterprise_settings
from app.enterprise.coverage import coverage_declaration
from app.enterprise.jobs import JOB_TYPE
from app.enterprise.models import EntEvidence, EntFrontier, EntScan
from app.enterprise.scoring.catalogue import DUTY_KINDS, GROUP_LABELS, LEGAL_REVIEW_STATUS, PARAMETERS, Parameter
from app.enterprise.scoring.engine import status_label
from app.jobs import queue

router = APIRouter(prefix="/scans", tags=["Enterprise results"])

MAX_EVIDENCE_CHARS = 200_000
EXCERPT_CHARS = 300
DOCUMENT_PATTERN = r"\.(pdf|docx?|odt|rtf|xlsx?|pptx?)(\?|#|$)"


def _require_enabled() -> None:
    if not get_enterprise_settings().enabled:
        raise errors.EnterpriseDisabled("Not found")


async def _scan(db: AsyncSession, scan_id: uuid.UUID, user: CurrentUser) -> EntScan:
    _require_enabled()
    scan = await scans.get_scan(db, scan_id, uuid.UUID(user.org_id))
    if scan is None:
        raise NotFoundError(f"Enterprise scan {scan_id} not found")
    return scan


def _page(items: list, total: int, offset: int, limit: int) -> dict:
    return {"items": items, "total": total, "offset": offset, "limit": limit}


# ---- list ---------------------------------------------------------------------------------------------------------------------------------------------------------

@router.get("", summary="The organisation's Enterprise scans, newest first")
async def list_scans(offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200), user: CurrentUser = Depends(get_caller), db: AsyncSession = Depends(get_db)) -> dict:
    _require_enabled()
    org = uuid.UUID(user.org_id)
    total = await db.scalar(select(func.count()).select_from(EntScan).where(EntScan.org_id == org)) or 0
    rows = (await db.scalars(select(EntScan).where(EntScan.org_id == org).order_by(EntScan.created_at.desc()).offset(offset).limit(limit))).all()
    from app.enterprise.method3 import service as method3
    items = []
    for s in rows:
        decl = await coverage_declaration(db, s.id)
        sc = await store.latest_score(db, s.id)
        m3 = await method3.latest(db, s.id)
        # `score` is the official score: the 96-parameter method 3 score, the one the report prints (owner's decision, 24 Nov 2026). The field keeps its name and type.
        items.append({
            "scan_id": str(s.id), "website_url": s.website_url, "status": s.status, "analysis_status": s.analysis_status, "completion": decl["completion"],
            "score": _m3_score(m3), "score_version": m3.version if m3 is not None else None,
            "coverage": sc.coverage if sc else None, "created_at": s.created_at, "completed_at": s.completed_at})
    return _page(items, total, offset, limit)


def _m3_score(row) -> float | None:
    return None if row is None or row.score is None else float(row.score)


def _official_score(row) -> dict | None:
    """The official score of an Enterprise scan: the 96-parameter method 3 result, as the report prints it. None until it has been calculated."""
    if row is None:
        return None
    return {"score": _m3_score(row), "marks_earned": float(row.marks_earned), "applicable_max": float(row.applicable_max), "version": row.version, "basis": row.basis,
            "as_of": row.as_of.isoformat(), "calculated_at": row.created_at}


# ---- summary and declaration ----------------------------------------------------------------------------------------------------------------------------------------

def _score_view(sc) -> dict | None:
    if sc is None:
        return None
    r = sc.result
    return {
        "score": r["score"], "name": r["name"], "version": r["version"], "basis": r["basis"], "coverage": r["coverage"], "points_available": r.get("points_available", 100),
        "reason_no_score": r["reason_no_score"], "earned_points": r["points_earned"], "points_in_play": r["points_in_play"], "not_yet_in_force": r["not_yet_in_force"],
        "by_group": {g: {("earned_points" if k == "points_earned" else k): v for k, v in d.items()} for g, d in r["by_group"].items()}, "needs_attention": r["needs_attention"], "as_of": r["as_of"], "calculated_at": sc.created_at}


async def _has_report(db: AsyncSession, scan_id: uuid.UUID) -> bool:
    return bool(await db.scalar(text("select 1 from ent_reports where scan_id = :s and status = 'ready' limit 1"), {"s": scan_id}))


@router.get("/{scan_id}/summary", summary="Everything the scan page needs in one call")
async def summary(scan_id: uuid.UUID, user: CurrentUser = Depends(get_caller), db: AsyncSession = Depends(get_db)) -> dict:
    scan = await _scan(db, scan_id, user)
    decl = await coverage_declaration(db, scan_id)
    from app.enterprise.method3 import service as method3
    return {
        "official_score": _official_score(await method3.latest(db, scan_id)),        # additive: the 96-parameter score; `score` below is the earlier 51-parameter result, kept for compatibility
        "scan_id": str(scan.id), "website_url": scan.website_url, "domain": scan.domain, "crawl_status": scan.status, "analysis_status": scan.analysis_status, "completion": decl["completion"],
        "score": _score_view(await store.latest_score(db, scan_id)), "declaration": decl, "legal_review_status": LEGAL_REVIEW_STATUS, "report_available": await _has_report(db, scan_id),
        "options": scan.options, "created_at": scan.created_at, "started_at": scan.started_at, "completed_at": scan.completed_at, "error": scan.error or scan.analysis_error,
        "stop_reason": scan.stop_reason, "previous_scan_id": str(scan.previous_scan_id) if scan.previous_scan_id else None}


@router.get("/{scan_id}/declaration", summary="The coverage declaration: actual counts of what was found, read, analysed, left out and failed")
async def declaration(scan_id: uuid.UUID, user: CurrentUser = Depends(get_caller), db: AsyncSession = Depends(get_db)) -> dict:
    await _scan(db, scan_id, user)
    return await coverage_declaration(db, scan_id)


# ---- parameters ---------------------------------------------------------------------------------------------------------------------------------------------------

def _parameter_view(p: Parameter, row, line: dict | None, as_of: date) -> dict:
    return {
        "id": p.id, "name": p.name, "group": p.group, "group_label": GROUP_LABELS[p.group], "kind": p.kind, "points": p.points, "partial_allowed": p.partial_allowed,
        "human_review_required": p.human_review, "conditional": p.conditional, "legal_refs": list(p.legal_refs), "in_force": p.in_force.isoformat() if p.in_force else None,
        "in_force_on_as_of": None if p.in_force is None else p.in_force <= as_of, "penalty_ceiling_cr": p.penalty_ceiling_cr if p.kind in DUTY_KINDS else None,
        "schedule_item": p.schedule_item if p.kind in DUTY_KINDS else None,
        # `state` is the scored result (met / partly_met / not_met), `status` its label (Pass / Partially Pass / Fail); `assessed_state` is what the assessor recorded before that.
        "state": line["state"] if line else "not_met", "status": status_label(line["state"]) if line else "Fail", "assessed_state": row.state if row else "not_assessed",
        "on_evidence": line.get("on_evidence", True) if line else False,
        "earned": line["earned"] if line else 0.0, "in_play": line["in_play"] if line else 0.0, "note": row.note if row else "",
        "confidence": row.confidence if row else None, "assessor": row.assessor if row else None, "model": row.model if row else None, "reviewed_by": row.reviewed_by if row else None,
        "review_reason": row.review_reason if row else None, "evidence_refs": list(row.evidence_refs or []) if row else [], "adjustment": line.get("adjustment") if line else None,
        "assessed_at": row.created_at if row else None}


async def parameter_views(db: AsyncSession, scan_id: uuid.UUID) -> list[dict]:
    rows = {r.parameter_id: r for r in await store.load_current_rows(db, scan_id)}
    sc = await store.latest_score(db, scan_id)
    lines = {x["parameter_id"]: x for x in (sc.result["lines"] if sc else [])}
    as_of = date.fromisoformat(sc.result["as_of"]) if sc else date.today()
    return [_parameter_view(p, rows.get(p.id), lines.get(p.id), as_of) for p in PARAMETERS]


@router.get("/{scan_id}/parameters", summary="All parameters with their current assessment")
async def parameters(scan_id: uuid.UUID, user: CurrentUser = Depends(get_caller), db: AsyncSession = Depends(get_db)) -> dict:
    await _scan(db, scan_id, user)
    items = await parameter_views(db, scan_id)
    return _page(items, len(items), 0, len(items))


@router.get("/{scan_id}/parameters/{parameter_id}/history", summary="Every assessment of one parameter, oldest first")
async def parameter_history(scan_id: uuid.UUID, parameter_id: str, user: CurrentUser = Depends(get_caller), db: AsyncSession = Depends(get_db)) -> dict:
    await _scan(db, scan_id, user)
    if parameter_id not in {p.id for p in PARAMETERS}:
        raise NotFoundError(f"Unknown parameter {parameter_id}")
    rows = await store.load_history(db, scan_id, parameter_id)
    items = [{"state": r.state, "note": r.note, "confidence": r.confidence, "assessor": r.assessor, "provider": r.provider, "model": r.model, "reviewed_by": r.reviewed_by,
              "review_reason": r.review_reason, "status": r.status, "evidence_refs": list(r.evidence_refs or []), "created_at": r.created_at} for r in rows]
    return _page(items, len(items), 0, len(items))


class ReviewBody(BaseModel):
    state: Literal["met", "partly_met", "not_met", "not_assessed", "not_applicable"]
    reason: str = Field(..., min_length=5, max_length=2000)


@router.post("/{scan_id}/parameters/{parameter_id}/review", summary="A person's decision on one parameter")
async def review_parameter(scan_id: uuid.UUID, parameter_id: str, body: ReviewBody, user: CurrentUser = Depends(get_application_scan_caller), db: AsyncSession = Depends(get_db)) -> dict:
    """Supersedes the engine's result (the old one is kept in the history), recalculates the score, rewrites the findings and appends an audit event."""
    await _scan(db, scan_id, user)
    param = next((p for p in PARAMETERS if p.id == parameter_id), None)
    if param is None:
        raise NotFoundError(f"Unknown parameter {parameter_id}")
    if body.state == "partly_met" and not param.partial_allowed:
        raise errors.InvalidScanState(f"{parameter_id} has no partial rule, so 'partly met' is not a valid result.")
    await store.save_review(db, scan_id, parameter_id, state=body.state, reviewer=user.user_id, reason=body.reason)
    await pipeline.recalculate(db, scan_id, actor=user.user_id)
    await db.commit()
    from app.enterprise.method3 import service as method3
    await method3.run_safe(scan_id, uuid.UUID(user.org_id), actor=user.user_id)            # the 96-parameter result follows the review too; a failure there never fails the review
    return next(v for v in await parameter_views(db, scan_id) if v["id"] == parameter_id)


# ---- findings -----------------------------------------------------------------------------------------------------------------------------------------------------

@router.get("/{scan_id}/findings", summary="Findings, most serious first")
async def findings(scan_id: uuid.UUID, severity: Literal["high", "medium", "low", "info"] | None = None, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200),
                   user: CurrentUser = Depends(get_caller), db: AsyncSession = Depends(get_db)) -> dict:
    await _scan(db, scan_id, user)
    rows, total = await store.list_findings(db, scan_id, offset=offset, limit=limit, severity=severity)
    items = [{
        "id": str(f.id), "finding_key": f.finding_key, "parameter_id": f.parameter_id, "title": f.title, "description": f.description, "severity": f.severity, "state": f.state,
        "legal_refs": f.legal_refs, "affected_urls": f.affected_urls, "affected_count": f.affected_count, "evidence_refs": f.evidence_refs, "expected": f.expected, "observed": f.observed,
        "reason": f.reason, "confidence": f.confidence, "recommendation": f.recommendation, "human_review": f.human_review, "verification_status": f.verification_status,
        "schedule_item": f.schedule_item, "penalty_ceiling_cr": f.penalty_ceiling_cr} for f in rows]
    return _page(items, total, offset, limit)


# ---- evidence -----------------------------------------------------------------------------------------------------------------------------------------------------

@router.get("/{scan_id}/evidence", summary="The evidence index (no content)")
async def evidence_index(scan_id: uuid.UUID, kind: str | None = None, source_url: str | None = None, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200),
                         user: CurrentUser = Depends(get_caller), db: AsyncSession = Depends(get_db)) -> dict:
    await _scan(db, scan_id, user)
    where = [EntEvidence.scan_id == scan_id] + ([EntEvidence.kind == kind] if kind else []) + ([EntEvidence.source_url == source_url] if source_url else [])
    total = await db.scalar(select(func.count()).select_from(EntEvidence).where(*where)) or 0
    rows = (await db.execute(
        select(EntEvidence.id, EntEvidence.kind, EntEvidence.source_url, EntEvidence.page_number, EntEvidence.byte_size, EntEvidence.detection_method, EntEvidence.captured_at,
               EntEvidence.content_hash, func.left(EntEvidence.content, EXCERPT_CHARS).label("excerpt"))
        .where(*where).order_by(EntEvidence.captured_at, EntEvidence.id).offset(offset).limit(limit))).all()
    items = [{"id": str(r.id), "kind": r.kind, "source_url": r.source_url, "page_number": r.page_number, "byte_size": r.byte_size, "detection_method": r.detection_method,
              "captured_at": r.captured_at, "content_hash": r.content_hash, "excerpt": r.excerpt} for r in rows]
    return _page(items, total, offset, limit)


@router.get("/{scan_id}/evidence/{evidence_id}", summary="One evidence record, with its content")
async def evidence_item(scan_id: uuid.UUID, evidence_id: uuid.UUID, user: CurrentUser = Depends(get_caller), db: AsyncSession = Depends(get_db)) -> dict:
    await _scan(db, scan_id, user)
    e = await db.scalar(select(EntEvidence).where(EntEvidence.id == evidence_id, EntEvidence.scan_id == scan_id))
    if e is None:
        raise NotFoundError("Evidence not found")
    return {"id": str(e.id), "kind": e.kind, "source_url": e.source_url, "page_number": e.page_number, "byte_size": e.byte_size, "detection_method": e.detection_method,
            "captured_at": e.captured_at, "content_hash": e.content_hash, "content_type": e.content_type, "content": e.content[:MAX_EVIDENCE_CHARS], "truncated": len(e.content) > MAX_EVIDENCE_CHARS}


# ---- URL and document inventories ----------------------------------------------------------------------------------------------------------------------------------

def _url_item(r: EntFrontier) -> dict:
    import re
    return {"url": r.original_url, "final_url": r.final_url, "depth": r.depth, "status": r.status, "analysis_status": r.analysis_status, "http_status": r.http_status,
            "status_reason": r.status_reason, "last_error": r.last_error, "discovery_sources": list(r.discovery_sources or []),
            "is_document": bool(re.search(DOCUMENT_PATTERN, r.normalized_url.lower())), "extraction_status": r.extraction_status,
            "title": r.page_title, "text_chars": r.text_chars, "content_hash": r.content_hash, "extracted_at": r.extracted_at, "evidence_note": r.evidence_note,
            "category": r.page_category, "category_reason": r.category_reason, "chunks_created": r.chunks_created, "chunks_analysed": r.chunks_analysed, "analysis_note": r.analysis_note}


@router.get("/{scan_id}/urls", summary="The URL inventory")
async def urls(scan_id: uuid.UUID, status: str | None = Query(default=None, pattern="^(pending|in_progress|done|failed|excluded)$"),
               analysis_status: str | None = Query(default=None, pattern="^(not_started|done|failed|not_applicable)$"), q: str | None = Query(default=None, max_length=200),
               offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200), user: CurrentUser = Depends(get_caller), db: AsyncSession = Depends(get_db)) -> dict:
    await _scan(db, scan_id, user)
    where = [EntFrontier.scan_id == scan_id]
    if status:
        where.append(EntFrontier.status == status)
    if analysis_status:
        where.append(EntFrontier.analysis_status == analysis_status)
    if q:
        where.append(EntFrontier.original_url.ilike(f"%{q.replace('%', '').replace('_', '')}%"))
    total = await db.scalar(select(func.count()).select_from(EntFrontier).where(*where)) or 0
    rows = (await db.scalars(select(EntFrontier).where(*where).order_by(EntFrontier.depth, EntFrontier.discovered_at, EntFrontier.id).offset(offset).limit(limit))).all()
    return _page([_url_item(r) for r in rows], total, offset, limit)


@router.get("/{scan_id}/documents", summary="The document inventory with extraction status")
async def documents(scan_id: uuid.UUID, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200), user: CurrentUser = Depends(get_caller), db: AsyncSession = Depends(get_db)) -> dict:
    await _scan(db, scan_id, user)
    where = [EntFrontier.scan_id == scan_id, func.lower(EntFrontier.normalized_url).op("~")(DOCUMENT_PATTERN)]
    total = await db.scalar(select(func.count()).select_from(EntFrontier).where(*where)) or 0
    rows = (await db.scalars(select(EntFrontier).where(*where).order_by(EntFrontier.discovered_at, EntFrontier.id).offset(offset).limit(limit))).all()
    pages = {r.source_url: r.n for r in (await db.execute(text(
        "select source_url, count(*) as n from ent_evidence where scan_id = :s and kind = 'document_text' group by source_url"), {"s": scan_id})).all()}
    items = [{"url": r.original_url, "final_url": r.final_url, "content_type": r.content_type, "status": r.status, "extraction_status": r.extraction_status, "extraction_method": r.extraction_method,
              "pages_read": pages.get(r.final_url or r.original_url, 0), "analysis_status": r.analysis_status, "status_reason": r.status_reason} for r in rows]
    return _page(items, total, offset, limit)


# ---- history, changes ----------------------------------------------------------------------------------------------------------------------------------------------

@router.get("/{scan_id}/method3", summary="The method 3.0.0 result: 96 parameters, URL-wise results, category marks and the score out of the applicable maximum")
async def method3_result(scan_id: uuid.UUID, user: CurrentUser = Depends(get_caller), db: AsyncSession = Depends(get_db)) -> dict:
    """Additive (20 Nov 2026): the 51-parameter endpoints keep their shape; this returns the latest method 3.0.0 calculation as stored."""
    await _scan(db, scan_id, user)
    from app.enterprise.method3 import service as method3
    row = await method3.latest(db, scan_id)
    if row is None:
        raise NotFoundError("Method 3.0.0 has not been calculated for this scan yet (it runs after the analysis).")
    return {"id": str(row.id), "created_at": row.created_at, **row.result}


@router.get("/{scan_id}/method3/history", summary="Every method 3.0.0 calculation, newest first")
async def method3_history(scan_id: uuid.UUID, user: CurrentUser = Depends(get_caller), db: AsyncSession = Depends(get_db)) -> dict:
    await _scan(db, scan_id, user)
    from app.enterprise.models import EntMethod3Result
    rows = (await db.scalars(select(EntMethod3Result).where(EntMethod3Result.scan_id == scan_id).order_by(EntMethod3Result.created_at.desc()))).all()
    items = [{"id": str(r.id), "version": r.version, "score": None if r.score is None else float(r.score), "marks_earned": float(r.marks_earned),
              "applicable_max": float(r.applicable_max), "basis": r.basis, "as_of": r.as_of.isoformat(), "created_at": r.created_at} for r in rows]
    return _page(items, len(items), 0, len(items))


@router.get("/{scan_id}/score/history", summary="Every score calculation, newest first")
async def score_history(scan_id: uuid.UUID, user: CurrentUser = Depends(get_caller), db: AsyncSession = Depends(get_db)) -> dict:
    await _scan(db, scan_id, user)
    rows = list(reversed(await store.score_history(db, scan_id)))
    items = [{"id": str(r.id), "version": r.version, "score": r.score, "coverage": r.coverage, "basis": r.basis, "as_of": r.as_of.isoformat(), "created_at": r.created_at} for r in rows]
    return _page(items, len(items), 0, len(items))


@router.get("/{scan_id}/changes", summary="What changed since an earlier scan of the same site")
async def get_changes(scan_id: uuid.UUID, against: uuid.UUID, user: CurrentUser = Depends(get_caller), db: AsyncSession = Depends(get_db)) -> dict:
    await _scan(db, scan_id, user)
    await _scan(db, against, user)                       # the earlier scan must be the caller's too
    return await changes.compare(db, scan_id, against)


# ---- analysis and report ------------------------------------------------------------------------------------------------------------------------------------------

@router.post("/{scan_id}/analyze", status_code=202, summary="Run (or re-run) the analysis phase")
async def analyze(scan_id: uuid.UUID, user: CurrentUser = Depends(get_application_scan_caller), db: AsyncSession = Depends(get_db)) -> dict:
    scan = await _scan(db, scan_id, user)
    if scan.status != "completed":
        raise errors.InvalidScanState(f"A scan whose crawl is {scan.status} cannot be analysed yet.")
    if scan.analysis_status == "running":
        raise errors.InvalidScanState("The analysis is already running.")
    org = uuid.UUID(user.org_id)
    await queue.enqueue(db, org_id=org, job_type=JOB_TYPE, payload={"scan_id": str(scan_id), "org_id": str(org)})
    await store.audit(db, scan_id, user.user_id, "analysis_requested", {})
    await db.commit()
    return {"scan_id": str(scan_id), "analysis_status": scan.analysis_status, "message": "Analysis queued."}


def _report_service():
    try:
        from app.enterprise.report import service
    except ImportError as exc:
        raise ConsivaError("The Enterprise report generator is not installed on this deployment.") from exc
    return service


class _NotReady(ConsivaError):
    status_code = 409


@router.post("/{scan_id}/report", status_code=202, summary="Generate a new report version")
async def create_report(scan_id: uuid.UUID, user: CurrentUser = Depends(get_application_scan_caller), db: AsyncSession = Depends(get_db)) -> dict:
    scan = await _scan(db, scan_id, user)
    if scan.status != "completed":
        raise _NotReady(f"A report needs a finished crawl; this scan is {scan.status}.")
    row = await _report_service().generate_report(db, scan_id)
    await store.audit(db, scan_id, user.user_id, "report_generated", {"version": row.version, "status": row.status})
    await db.commit()
    return {"scan_id": str(scan_id), "version": row.version, "status": row.status}


@router.get("/{scan_id}/reports", summary="Report versions")
async def reports(scan_id: uuid.UUID, user: CurrentUser = Depends(get_caller), db: AsyncSession = Depends(get_db)) -> dict:
    await _scan(db, scan_id, user)
    rows = (await db.execute(text("select version, status, page_count, byte_size, sha256, generated_at, score_result_id from ent_reports where scan_id = :s order by version desc"), {"s": scan_id})).all()
    items = [{"version": r.version, "status": r.status, "page_count": r.page_count, "byte_size": r.byte_size, "sha256": r.sha256, "generated_at": r.generated_at,
              "score_result_id": str(r.score_result_id) if r.score_result_id else None} for r in rows]
    return _page(items, len(items), 0, len(items))


@router.get("/{scan_id}/report.pdf", summary="The report as a PDF", response_class=Response)
async def report_pdf(scan_id: uuid.UUID, version: int | None = None, user: CurrentUser = Depends(get_caller), db: AsyncSession = Depends(get_db)) -> Response:
    await _scan(db, scan_id, user)
    q = "select version, pdf from ent_reports where scan_id = :s and status = 'ready' and pdf is not null" + (" and version = :v" if version else "") + " order by version desc limit 1"
    row = (await db.execute(text(q), {"s": scan_id, **({"v": version} if version else {})})).first()
    if row is None:
        raise NotFoundError("No report has been generated for this scan yet.")
    return Response(content=bytes(row.pdf), media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="consiva-enterprise-report-{scan_id}-v{row.version}.pdf"'})
