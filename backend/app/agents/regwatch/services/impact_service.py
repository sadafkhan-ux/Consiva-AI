"""What in THIS organisation a regulatory change may touch (spec §4 step 7, §12).

This is where Agent 5 earns its place beside the other four. A regulatory change about
consent means something concrete when you already know which websites this
organisation scans, what data its systems hold, how it answers access requests and how
it handles incidents. Without that, an alert is a link to a PDF.

TWO RULES GOVERN EVERY LINK MADE HERE
-------------------------------------
1. BY REFERENCE, NEVER BY COPY. An impact row names another agent's row by
   (target_kind, target_id). Nothing is duplicated, so nothing can drift out of step
   with the agent that owns it.

2. NOTHING HERE ESTABLISHES AN OBLIGATION. A link says "this change is about consent,
   and you have four websites with consent findings, so look at those". It does not
   say the change applies to them. Every row carries a confidence, and no rule in this
   module may write CONFIRMED -- that remains a position a person takes.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.regwatch.rules import relevance as relevance_rules
from app.agents.regwatch.schemas import watch
from app.db.models import (
    ConsentFinding,
    ConsentScan,
    DsrSourceAuthorization,
    IncidentCase,
    RegWatchFinding,
    RegWatchImpact,
    RopaRecordRow,
    Website,
)
from app.db.repositories import regwatch_repository as repo
from app.services import audit_service

logger = logging.getLogger(__name__)

# Which topic points at which agent. A topic with no entry maps to nothing, which is
# the honest outcome -- an empty impact list with a recorded gap, not a guess.
_TOPIC_TARGETS: dict[str, tuple[str, ...]] = {
    "consent": (watch.TARGET_CONSENT_WEBSITE, watch.TARGET_CONSENT_FINDING),
    "rights": (watch.TARGET_DSR_CONFIG, watch.TARGET_ROPA_RECORD),
    "breach": (watch.TARGET_INCIDENT,),
    "retention": (watch.TARGET_DSR_CONFIG, watch.TARGET_ROPA_RECORD),
    "transfer": (watch.TARGET_ROPA_RECORD,),
    "children": (watch.TARGET_ROPA_RECORD, watch.TARGET_CONSENT_WEBSITE),
    "security": (watch.TARGET_ROPA_RECORD, watch.TARGET_INCIDENT),
}


async def map_impact(
    db: AsyncSession,
    finding: RegWatchFinding,
    *,
    topics: tuple[str, ...],
    actor_user_id: uuid.UUID | None = None,
) -> tuple[list[RegWatchImpact], list[str]]:
    """Link a finding to what it may touch. Returns (rows, gaps).

    `gaps` says what could NOT be established -- a topic with nothing configured
    behind it, or no topic at all. An empty impact list with no gaps recorded would
    read as "nothing is affected", which is a conclusion this cannot reach.
    """
    org_id = finding.org_id
    rows: list[RegWatchImpact] = []
    gaps: list[str] = []

    if not topics:
        gaps.append(
            "The change did not match any topic this platform tracks, so nothing "
            "could be linked to it automatically. What it affects must be established "
            "by hand."
        )

    wanted: set[str] = set()
    for topic in topics:
        targets = _TOPIC_TARGETS.get(topic)
        if not targets:
            gaps.append(f"topic {topic!r} has no mapping to anything this platform holds")
            continue
        wanted.update(targets)

    for kind in sorted(wanted):
        found = await _targets_for(db, org_id, kind)
        if not found:
            gaps.append(
                f"the change touches {kind.replace('_', ' ')}, but this organisation "
                f"has none recorded in Consiva"
            )
            continue
        for target_id, label in found:
            rows.append(RegWatchImpact(
                org_id=org_id, finding_id=finding.id,
                target_kind=kind, target_id=target_id, target_label=label,
                # POSSIBLE, always. This row says "look here", not "this is affected".
                confidence=watch.POSSIBLE,
                derived_from=watch.DERIVED_RULE,
                rationale=(
                    f"The change text touches {', '.join(topics)}, which this platform "
                    f"tracks as {kind.replace('_', ' ')}. Whether this particular item "
                    "is actually affected is for a reviewer to decide."
                ),
            ))

    await repo.replace_impacts(db, finding.id, org_id, rows)
    await audit_service.record(
        db, org_id=org_id, actor_user_id=actor_user_id,
        action=watch.AUDIT_IMPACT_MAPPED,
        entity_type=watch.AUDIT_ENTITY, entity_id=finding.id,
        after={
            "topics": list(topics),
            "linked": len(rows),
            "kinds": sorted({r.target_kind for r in rows}),
            "confidence": watch.POSSIBLE,
            "gaps": gaps,
        },
    )
    return rows, gaps


# How many of each kind to link. A change that touches "ROPA records" on an
# organisation with 600 of them should not produce 600 impact rows -- that is a wall,
# not a finding. The count is reported in the label so the number is not lost.
_SAMPLE_LIMIT = 10


async def _targets_for(
    db: AsyncSession, org_id: uuid.UUID, kind: str
) -> list[tuple[uuid.UUID | None, str]]:
    """The other agents' rows this kind refers to, org-scoped, capped."""
    if kind == watch.TARGET_CONSENT_WEBSITE:
        result = await db.execute(
            select(Website.id, Website.domain)
            .where(Website.org_id == org_id)
            .order_by(Website.domain)
            .limit(_SAMPLE_LIMIT)
        )
        return [(row.id, row.domain) for row in result]

    if kind == watch.TARGET_CONSENT_FINDING:
        # Only findings still awaiting a decision: a change to consent law is a reason
        # to revisit what is open, not to reopen what was settled years ago.
        result = await db.execute(
            select(ConsentFinding.id, ConsentFinding.finding_text, ConsentScan.url)
            .join(ConsentScan, ConsentScan.id == ConsentFinding.scan_id)
            .where(ConsentScan.org_id == org_id, ConsentFinding.status == "pending")
            .order_by(ConsentFinding.created_at.desc())
            .limit(_SAMPLE_LIMIT)
        )
        return [
            (row.id, f"{(row.finding_text or 'consent finding')[:60]} ({row.url})")
            for row in result
        ]

    if kind == watch.TARGET_ROPA_RECORD:
        # Superseded rows are history: a later regulation does not touch a version
        # of a processing activity that has already been replaced.
        total = await db.execute(
            select(func.count()).select_from(RopaRecordRow).where(
                RopaRecordRow.org_id == org_id, RopaRecordRow.status != "superseded"
            )
        )
        count = int(total.scalar_one())
        if not count:
            return []
        result = await db.execute(
            select(RopaRecordRow.id, RopaRecordRow.processing_activity, RopaRecordRow.status)
            .where(RopaRecordRow.org_id == org_id, RopaRecordRow.status != "superseded")
            .order_by(RopaRecordRow.created_at.desc())
            .limit(_SAMPLE_LIMIT)
        )
        rows = [
            (row.id, f"{row.processing_activity or 'processing activity'} [{row.status}]")
            for row in result
        ]
        if count > _SAMPLE_LIMIT:
            # The count travels with the sample, so "10 records" is never mistaken for
            # the whole picture.
            rows.append((None, f"...and {count - _SAMPLE_LIMIT} further ROPA record(s)"))
        return rows

    if kind == watch.TARGET_DSR_CONFIG:
        result = await db.execute(
            select(DsrSourceAuthorization.id, DsrSourceAuthorization.searchable_tables)
            .where(DsrSourceAuthorization.org_id == org_id)
            .limit(_SAMPLE_LIMIT)
        )
        return [
            (row.id, f"DSR source authorisation ({len(row.searchable_tables or [])} searchable tables)")
            for row in result
        ]

    if kind == watch.TARGET_INCIDENT:
        result = await db.execute(
            select(IncidentCase.id, IncidentCase.reference, IncidentCase.title)
            .where(
                IncidentCase.org_id == org_id,
                IncidentCase.status.not_in(("closed", "rejected", "cancelled")),
            )
            .order_by(IncidentCase.created_at.desc())
            .limit(_SAMPLE_LIMIT)
        )
        return [(row.id, f"{row.reference}: {row.title[:60]}") for row in result]

    return []


def summarise_impact(rows: list[RegWatchImpact], gaps: list[str]) -> str:
    """One deterministic sentence. No model in this path.

    Says what was linked AND what could not be, in the same breath, because a reader
    who sees only the first half will read an empty list as "nothing affected".
    """
    if not rows and not gaps:
        return "No impact mapping was attempted."
    if not rows:
        return (
            "Nothing in this organisation could be linked to this change automatically. "
            + " ".join(gaps)
        )
    by_kind: dict[str, int] = {}
    for row in rows:
        by_kind[row.target_kind] = by_kind.get(row.target_kind, 0) + 1
    parts = ", ".join(
        f"{count} {kind.replace('_', ' ')}{'s' if count != 1 else ''}"
        for kind, count in sorted(by_kind.items())
    )
    sentence = (
        f"May touch {parts}. Each is a place to look, held at "
        f"{watch.POSSIBLE} confidence -- not a finding that it is affected."
    )
    if gaps:
        sentence += " Not established: " + " ".join(gaps)
    return sentence


def suggest_topics(result: relevance_rules.RelevanceResult) -> tuple[str, ...]:
    """The topics impact mapping should aim at, taken from the relevance assessment
    rather than recomputed, so the two cannot disagree about what the change is about."""
    return result.topics
