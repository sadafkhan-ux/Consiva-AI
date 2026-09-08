import uuid

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Approval, ConsentFinding, ConsentRecommendation, ConsentScan
from app.llm.schemas import ConsentFindingLLM


async def create_finding(
    db: AsyncSession,
    *,
    scan_id: uuid.UUID,
    agent_run_id: uuid.UUID,
    finding: ConsentFindingLLM,
    dpdp_reference: list[dict],
) -> ConsentFinding:
    """`dpdp_reference` is the backend-resolved {chunk_id, section, source_doc, version}
    list (see agents/consent_agent/nodes/create_findings.py) — NOT finding.dpdp_reference,
    which is just the LLM's raw chunk_id citations."""
    row = ConsentFinding(
        scan_id=scan_id,
        agent_run_id=agent_run_id,
        category=finding.category,
        risk_level=finding.risk_level,
        priority=finding.priority,
        finding_text=finding.finding,
        evidence=finding.evidence,
        dpdp_reference=dpdp_reference,
        requires_human_review=finding.requires_human_review,
        status="pending",
    )
    db.add(row)
    await db.flush()
    db.add(ConsentRecommendation(finding_id=row.id, recommendation_text=finding.recommendation))
    await db.flush()
    return row


async def get_finding(db: AsyncSession, finding_id: uuid.UUID, org_id: uuid.UUID) -> ConsentFinding | None:
    """org_id is enforced here (via a join to consent_scans), not just by the caller --
    a defense-in-depth fix: this used to be a naked SELECT-by-id, safe only because
    every current caller happened to re-check ownership afterward."""
    result = await db.execute(
        select(ConsentFinding)
        .join(ConsentScan, ConsentFinding.scan_id == ConsentScan.id)
        .where(ConsentFinding.id == finding_id, ConsentScan.org_id == org_id)
    )
    return result.scalar_one_or_none()


async def list_findings_for_scan(db: AsyncSession, scan_id: uuid.UUID) -> list[ConsentFinding]:
    result = await db.execute(
        select(ConsentFinding).where(ConsentFinding.scan_id == scan_id).order_by(ConsentFinding.created_at)
    )
    return list(result.scalars().all())


async def list_pending_review_for_agent_run(
    db: AsyncSession, agent_run_id: uuid.UUID, org_id: uuid.UUID
) -> list[ConsentFinding]:
    """Scoped to one agent_run (not the whole scan) so a re-analysis's gate doesn't
    wait on a stale finding from an earlier run of the same scan. org_id is enforced
    via a join to consent_scans, not left to the caller."""
    result = await db.execute(
        select(ConsentFinding)
        .join(ConsentScan, ConsentFinding.scan_id == ConsentScan.id)
        .where(
            ConsentFinding.agent_run_id == agent_run_id,
            ConsentFinding.requires_human_review.is_(True),
            ConsentFinding.status == "pending",
            ConsentScan.org_id == org_id,
        )
    )
    return list(result.scalars().all())


async def list_recommendations_for_finding(db: AsyncSession, finding_id: uuid.UUID) -> list[ConsentRecommendation]:
    result = await db.execute(
        select(ConsentRecommendation).where(ConsentRecommendation.finding_id == finding_id)
    )
    return list(result.scalars().all())


async def list_recommendations_for_findings(
    db: AsyncSession, finding_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[ConsentRecommendation]]:
    """Batched variant of list_recommendations_for_finding — one query for N findings
    instead of N, grouped by finding_id in Python. Use this for any listing endpoint;
    reserve the singular version for genuinely single-finding lookups."""
    if not finding_ids:
        return {}
    result = await db.execute(
        select(ConsentRecommendation).where(ConsentRecommendation.finding_id.in_(finding_ids))
    )
    by_finding: dict[uuid.UUID, list[ConsentRecommendation]] = {fid: [] for fid in finding_ids}
    for rec in result.scalars().all():
        by_finding[rec.finding_id].append(rec)
    return by_finding


def _org_scoped_scan_ids(org_id: uuid.UUID):
    """Correlated subquery reused by the UPDATE statements below -- SQLAlchemy's core
    update() can't express a JOIN directly, so tenant scoping goes through
    `scan_id IN (SELECT id FROM consent_scans WHERE org_id = ...)` instead."""
    return select(ConsentScan.id).where(ConsentScan.org_id == org_id)


async def update_finding_status(
    db: AsyncSession, finding_id: uuid.UUID, status: str, org_id: uuid.UUID
) -> ConsentFinding | None:
    """Atomic conditional update: only applies if the finding is still "pending" AND
    belongs to the caller's org (defense-in-depth -- the caller already checks this,
    but the write itself no longer trusts that alone), closing the race window
    between two reviewers deciding the same finding concurrently (see
    review_service.py, which turns a no-op result into FindingAlreadyDecidedError
    rather than silently overwriting a prior decision). Returns None if no row
    matched (already decided, wrong org, or doesn't exist)."""
    result = await db.execute(
        update(ConsentFinding)
        .where(
            ConsentFinding.id == finding_id,
            ConsentFinding.status == "pending",
            ConsentFinding.scan_id.in_(_org_scoped_scan_ids(org_id)),
        )
        .values(status=status)
        .returning(ConsentFinding)
    )
    finding = result.scalar_one_or_none()
    await db.flush()
    return finding


# Whitelisted so an edit can only touch fields that actually exist on ConsentFinding and
# are safe for a human reviewer to override -- never scan_id/agent_run_id/evidence/etc.
EDITABLE_FINDING_FIELDS = {"category", "risk_level", "priority", "finding_text", "requires_human_review"}


async def apply_edit(
    db: AsyncSession, finding_id: uuid.UUID, edited_payload: dict, org_id: uuid.UUID
) -> ConsentFinding | None:
    """Same atomic pending-only + org-scoped guard as update_finding_status -- see its
    docstring."""
    values = {"status": "edited"}
    for key, value in edited_payload.items():
        if key in EDITABLE_FINDING_FIELDS:
            values[key] = value
    result = await db.execute(
        update(ConsentFinding)
        .where(
            ConsentFinding.id == finding_id,
            ConsentFinding.status == "pending",
            ConsentFinding.scan_id.in_(_org_scoped_scan_ids(org_id)),
        )
        .values(**values)
        .returning(ConsentFinding)
    )
    finding = result.scalar_one_or_none()
    await db.flush()
    return finding


async def create_approval(
    db: AsyncSession,
    *,
    finding_id: uuid.UUID,
    reviewer_user_id: uuid.UUID,
    decision: str,
    reason: str | None,
    edited_payload: dict | None = None,
) -> Approval:
    row = Approval(
        finding_id=finding_id,
        reviewer_user_id=reviewer_user_id,
        decision=decision,
        reason=reason,
        edited_payload=edited_payload,
    )
    db.add(row)
    await db.flush()
    return row
