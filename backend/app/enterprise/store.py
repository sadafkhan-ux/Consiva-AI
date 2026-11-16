"""Persistence of the Enterprise assessment layer: parameter assessments, score results, findings and the audit trail.

History is kept. A parameter's result is superseded by a newer one (a review, a re-analysis) and never edited; every score calculation is a row; an audit event is appended for each
change a person or a process makes. Reading returns only the current assessment of each parameter.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.enterprise.models import EntAssessment, EntAuditEvent, EntFinding, EntScoreResult
from app.enterprise.scoring.catalogue import BY_ID, CATALOGUE_VERSION
from app.enterprise.scoring.engine import STATES, Assessment, ScoreResult, apply_review


async def audit(db: AsyncSession, scan_id: uuid.UUID, actor: str, event: str, detail: dict | None = None) -> None:
    db.add(EntAuditEvent(scan_id=scan_id, actor=actor, event=event, detail=detail or {}))
    await db.flush()


async def save_assessments(db: AsyncSession, scan_id: uuid.UUID, assessments: list[Assessment], *, assessor: str, provider: str | None = None, model: str | None = None) -> int:
    """Record each assessment as the parameter's current one, superseding the previous current one. A result identical to the current one is not recorded again. Returns how many were written."""
    written = 0
    for a in assessments:
        if a.parameter_id not in BY_ID:
            raise ValueError(f"unknown parameter {a.parameter_id!r}")
        if a.state not in STATES:
            raise ValueError(f"unknown state {a.state!r}")
        current = await db.scalar(select(EntAssessment).where(EntAssessment.scan_id == scan_id, EntAssessment.parameter_id == a.parameter_id, EntAssessment.status == "current"))
        refs = list(a.evidence_refs)
        if current is not None:
            if (current.state, current.evidence_refs, current.note, current.assessor) == (a.state, refs, a.note, assessor) and current.confidence == a.confidence:
                continue
            current.status = "superseded"
            await db.flush()
        db.add(EntAssessment(
            scan_id=scan_id, parameter_id=a.parameter_id, state=a.state, evidence_refs=refs, confidence=a.confidence, note=a.note, assessor=assessor, provider=provider, model=model,
            reviewed_by=a.reviewed_by, review_reason=a.review_reason, catalogue_version=CATALOGUE_VERSION))
        await db.flush()
        written += 1
    return written


def _to_assessment(r: EntAssessment) -> Assessment:
    return Assessment(r.parameter_id, r.state, tuple(r.evidence_refs or ()), r.confidence, r.note or "", r.reviewed_by, r.review_reason)


async def load_current(db: AsyncSession, scan_id: uuid.UUID) -> list[Assessment]:
    rows = (await db.scalars(select(EntAssessment).where(EntAssessment.scan_id == scan_id, EntAssessment.status == "current").order_by(EntAssessment.parameter_id))).all()
    return [_to_assessment(r) for r in rows]


async def load_current_rows(db: AsyncSession, scan_id: uuid.UUID) -> list[EntAssessment]:
    return list((await db.scalars(select(EntAssessment).where(EntAssessment.scan_id == scan_id, EntAssessment.status == "current").order_by(EntAssessment.parameter_id))).all())


async def load_history(db: AsyncSession, scan_id: uuid.UUID, parameter_id: str) -> list[EntAssessment]:
    return list((await db.scalars(select(EntAssessment).where(EntAssessment.scan_id == scan_id, EntAssessment.parameter_id == parameter_id).order_by(EntAssessment.created_at))).all())


async def save_review(db: AsyncSession, scan_id: uuid.UUID, parameter_id: str, *, state: str, reviewer: str, reason: str) -> Assessment:
    """A person's decision on one parameter. It supersedes the engine's result, keeps the evidence it rested on, and is written to the audit trail with who and why."""
    rows = {r.parameter_id: r for r in await load_current_rows(db, scan_id)}
    base = _to_assessment(rows[parameter_id]) if parameter_id in rows else Assessment(parameter_id, "not_assessed")
    reviewed = apply_review(base, reviewer=reviewer, state=state, reason=reason)
    if state in ("met", "partly_met", "not_met") and not reviewed.evidence_refs:
        reviewed = Assessment(parameter_id, state, (), None, base.note, reviewed.reviewed_by, reviewed.review_reason)
    await save_assessments(db, scan_id, [reviewed], assessor="review")
    await audit(db, scan_id, reviewer, "assessment_reviewed", {"parameter_id": parameter_id, "from": base.state, "to": state, "reason": reason})
    return reviewed


async def save_score(db: AsyncSession, scan_id: uuid.UUID, result: ScoreResult) -> uuid.UUID:
    row = EntScoreResult(scan_id=scan_id, version=result.version, catalogue_fingerprint=result.catalogue_fingerprint, as_of=result.as_of, score=result.score, coverage=result.coverage,
                         basis=result.basis, result=result.to_dict())
    db.add(row)
    await db.flush()
    return row.id


async def latest_score(db: AsyncSession, scan_id: uuid.UUID) -> EntScoreResult | None:
    return await db.scalar(select(EntScoreResult).where(EntScoreResult.scan_id == scan_id).order_by(EntScoreResult.created_at.desc()).limit(1))


async def score_history(db: AsyncSession, scan_id: uuid.UUID) -> list[EntScoreResult]:
    return list((await db.scalars(select(EntScoreResult).where(EntScoreResult.scan_id == scan_id).order_by(EntScoreResult.created_at))).all())


@dataclass
class FindingDraft:
    finding_key: str
    parameter_id: str
    title: str
    description: str
    severity: str
    state: str
    legal_refs: list[str] = field(default_factory=list)
    affected_urls: list[str] = field(default_factory=list)
    affected_count: int = 0
    evidence_refs: list[str] = field(default_factory=list)
    expected: str = ""
    observed: str = ""
    reason: str = ""
    confidence: str | None = None
    recommendation: str = ""
    human_review: bool = False
    verification_status: str = "unverified"
    schedule_item: int | None = None
    penalty_ceiling_cr: int | None = None


async def replace_findings(db: AsyncSession, scan_id: uuid.UUID, drafts: list[FindingDraft]) -> int:
    """The scan's findings are those of its current assessments: written afresh each time the assessments change, so a finding never outlives the result that raised it."""
    await db.execute(text("delete from ent_findings where scan_id = :s"), {"s": scan_id})
    for d in drafts:
        db.add(EntFinding(scan_id=scan_id, **d.__dict__))
    await db.flush()
    return len(drafts)


async def list_findings(db: AsyncSession, scan_id: uuid.UUID, *, offset: int = 0, limit: int = 50, severity: str | None = None) -> tuple[list[EntFinding], int]:
    q = select(EntFinding).where(EntFinding.scan_id == scan_id)
    if severity:
        q = q.where(EntFinding.severity == severity)
    total = await db.scalar(select(func.count()).select_from(q.subquery())) or 0
    order = text("case severity when 'high' then 0 when 'medium' then 1 when 'low' then 2 else 3 end, parameter_id, finding_key")
    rows = (await db.scalars(q.order_by(order).offset(offset).limit(limit))).all()
    return list(rows), total


def today() -> date:
    return date.today()
