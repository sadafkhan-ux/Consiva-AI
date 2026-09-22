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

from app.agents.regwatch.errors import (
    ApprovalRequiredError,
    InvalidWatchTransitionError,
    WatchNotReadyError,
)
from app.agents.regwatch.rules import relevance as relevance_rules
from app.agents.regwatch.schemas import watch
from app.db.models import (
    ConsentFinding,
    ConsentScan,
    DsrSourceAuthorization,
    IncidentCase,
    Policy,
    RegWatchFinding,
    RegWatchImpact,
    RopaDataSource,
    RopaRecordRow,
    Website,
)
from app.db.repositories import regwatch_repository as repo
from app.services import audit_service

logger = logging.getLogger(__name__)

# Which topic points at which agent. A topic with no entry maps to nothing, which is
# the honest outcome -- an empty impact list with a recorded gap, not a guess.
_TOPIC_TARGETS: dict[str, tuple[str, ...]] = {
    "consent": (watch.TARGET_CONSENT_WEBSITE, watch.TARGET_CONSENT_FINDING,
                watch.TARGET_POLICY),
    "rights": (watch.TARGET_DSR_CONFIG, watch.TARGET_ROPA_RECORD, watch.TARGET_POLICY),
    "breach": (watch.TARGET_INCIDENT, watch.TARGET_ROPA_SOURCE),
    "retention": (watch.TARGET_DSR_CONFIG, watch.TARGET_ROPA_RECORD),
    "transfer": (watch.TARGET_ROPA_RECORD, watch.TARGET_ROPA_SOURCE),
    "children": (watch.TARGET_ROPA_RECORD, watch.TARGET_CONSENT_WEBSITE),
    "security": (watch.TARGET_ROPA_RECORD, watch.TARGET_INCIDENT,
                 watch.TARGET_ROPA_SOURCE),
}

# WHY `control` IS NOT IN THAT TABLE
# ----------------------------------
# `TARGET_CONTROL` is in the vocabulary and in the database's CHECK constraint, and
# nothing maps to it, deliberately. This platform has no control register -- no table
# of controls, no owners, no implementation status. Pointing a regulatory change at
# "your controls" when there is no such record would be inventing the target, which is
# the one thing every rule in this module is arranged to avoid. The value stays in the
# vocabulary because a reviewer can still file a manual impact under it (see
# `add_manual_impact`), and because a control register is a plausible thing to build
# later; it is not mapped automatically because there is nothing to map to.

# The ROPA payload fields that carry real evidence for a topic, and what finding one
# actually means. This is the difference between "the change is about transfers and
# you have ROPA records" and "the change is about transfers and THIS record names a
# processor in the US".
_ROPA_EVIDENCE: dict[str, tuple[str, ...]] = {
    "transfer": ("processors", "recipients", "storage_locations", "transfer_information"),
    "retention": ("retention",),
    "security": ("security_control_status",),
    "consent": ("consent_or_processing_context",),
    "children": ("data_subjects",),
}

# `rights` is deliberately absent above. Measured against live data it matched EVERY
# record, because every ROPA record has data elements an access request would reach.
# Upgrading all of them to PROBABLE would not be precision, it would be the topic rule
# with a larger number attached -- and a confidence that never discriminates teaches a
# reviewer to ignore the confidence. The topic rule already links these at POSSIBLE,
# which is the honest strength of "all your records hold data".

# ROPA fields are often filled in with a placeholder rather than left empty, and a
# placeholder is a GAP, not evidence. "This record has a retention period of Unknown"
# says the organisation does not know its retention -- which is worth a reviewer's
# attention, but it is not evidence that a retention change touches this record, and
# stamping PROBABLE on it would be precisely the false precision this agent exists to
# avoid. Measured live: four of five records carried "Unknown" here.
_PLACEHOLDERS = frozenset({
    "unknown", "not determined", "undetermined", "n/a", "na", "none", "null",
    "tbd", "to be determined", "not set", "not specified", "-", "",
})


def _is_placeholder(value: object) -> bool:
    return str(value or "").strip().lower() in _PLACEHOLDERS

# Countries that make a transfer cross-border for an India-based fiduciary. Kept
# small and explicit rather than "anything that is not IN": a list nobody can read is
# a list nobody can correct, and an unrecognised location produces no claim at all.
_OFFSHORE_MARKERS = ("us", "usa", "united states", "eu", "uk", "singapore", "ireland",
                     "germany", "netherlands", "australia", "canada")


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

    # ── Pass 1: the topic rules. Broad, and honest about being broad. ──────────
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
                # POSSIBLE. This row says "look here", not "this is affected".
                confidence=watch.POSSIBLE,
                derived_from=watch.DERIVED_RULE,
                rationale=(
                    f"The change text touches {', '.join(topics)}, which this platform "
                    f"tracks as {kind.replace('_', ' ')}. Whether this particular item "
                    "is actually affected is for a reviewer to decide."
                ),
            ))

    # ── Pass 2: ROPA metadata. Narrow, and evidenced. ──────────────────────────
    #
    # A topic rule can only say "you have ROPA records". The records themselves say
    # far more: which name a processor abroad, which carry a retention period, which
    # record their security status. Where a record's own payload speaks to the topic,
    # the link is upgraded -- PROBABLE rather than POSSIBLE, and the evidence is
    # quoted in the rationale so a reviewer can check it rather than take it on trust.
    #
    # PROBABLE, not CONFIRMED: the evidence establishes that this record is the kind
    # of thing the change is about, never that an obligation applies to it.
    evidenced = await _ropa_evidence_links(db, finding, topics)
    rows = _merge_links(rows, evidenced)

    await repo.replace_impacts(db, finding.id, org_id, rows)
    await audit_service.record(
        db, org_id=org_id, actor_user_id=actor_user_id,
        action=watch.AUDIT_IMPACT_MAPPED,
        entity_type=watch.AUDIT_ENTITY, entity_id=finding.id,
        after={
            "topics": list(topics),
            "linked": len(rows),
            "kinds": sorted({r.target_kind for r in rows}),
            # Was a single hardcoded POSSIBLE, which stopped being true the moment
            # ROPA-evidenced links could reach PROBABLE. Recorded as it actually is,
            # split by how each link was arrived at, so the log never asserts a
            # confidence no row holds.
            "confidences": sorted({r.confidence for r in rows}),
            "derivations": sorted({r.derived_from for r in rows}),
            "evidenced": sum(1 for r in rows if r.derived_from == watch.DERIVED_ROPA),
            "gaps": gaps,
        },
    )
    return rows, gaps


# How many of each kind to link. A change that touches "ROPA records" on an
# organisation with 600 of them should not produce 600 impact rows -- that is a wall,
# not a finding. The count is reported in the label so the number is not lost.
_SAMPLE_LIMIT = 10

# How many ROPA records to read payloads for when looking for evidence. Higher than
# the display cap because this pass READS many and LINKS few -- most records will say
# nothing about the topic, and the ones that do are the point.
_EVIDENCE_SCAN_LIMIT = 200


async def _ropa_evidence_links(
    db: AsyncSession, finding: RegWatchFinding, topics: tuple[str, ...]
) -> list[RegWatchImpact]:
    """Impact links a ROPA record's OWN payload supports, with the evidence quoted.

    Reads only the fields listed in `_ROPA_EVIDENCE` for the topics that matched, so
    a change about retention never goes rummaging through processor locations. A
    record whose payload says nothing about the topic produces no link here at all --
    it may still appear from the topic rules, at the lower confidence they warrant.
    """
    wanted = [t for t in topics if t in _ROPA_EVIDENCE]
    if not wanted:
        return []

    result = await db.execute(
        select(RopaRecordRow.id, RopaRecordRow.processing_activity, RopaRecordRow.payload)
        .where(RopaRecordRow.org_id == finding.org_id, RopaRecordRow.status != "superseded")
        .order_by(RopaRecordRow.created_at.desc())
        .limit(_EVIDENCE_SCAN_LIMIT)
    )

    links: list[RegWatchImpact] = []
    for row in result:
        payload = row.payload or {}
        for topic in wanted:
            evidence = _evidence_for(topic, payload)
            if not evidence:
                continue
            links.append(RegWatchImpact(
                org_id=finding.org_id, finding_id=finding.id,
                target_kind=watch.TARGET_ROPA_RECORD,
                target_id=row.id,
                target_label=row.processing_activity or "processing activity",
                confidence=watch.PROBABLE,
                derived_from=watch.DERIVED_ROPA,
                rationale=(
                    f"This record's own ROPA entry speaks to {topic}: {evidence}. That "
                    "makes it the kind of processing the change is about. It is not a "
                    "finding that any obligation applies to it -- that is a reviewer's "
                    "call."
                ),
            ))
            break  # one link per record; the first matching topic carries the evidence
    return links


def _evidence_for(topic: str, payload: dict) -> str | None:
    """The concrete thing in this payload that speaks to the topic, as readable text.

    None whenever nothing does -- silence rather than a vague link, because a link
    whose rationale says "this record exists" is the topic rule again wearing a
    higher confidence.
    """
    if topic == "transfer":
        offshore = []
        for processor in payload.get("processors") or []:
            location = str((processor or {}).get("location") or "").strip()
            if location and location.lower() in _OFFSHORE_MARKERS:
                offshore.append(f"{processor.get('name') or 'a processor'} in {location}")
        for place in payload.get("storage_locations") or []:
            if str(place).lower() in _OFFSHORE_MARKERS:
                offshore.append(f"storage in {place}")
        return "; ".join(offshore[:3]) if offshore else None

    if topic == "retention":
        retention = payload.get("retention")
        if _is_placeholder(retention):
            return None
        return f"a stated retention period of {str(retention).strip()}"

    if topic == "security":
        status = payload.get("security_control_status")
        if _is_placeholder(status):
            return None
        return f"a recorded security control status of {str(status).strip()!r}"

    if topic == "consent":
        context = payload.get("consent_or_processing_context")
        if _is_placeholder(context):
            return None
        return f"a recorded processing context of {str(context).strip()!r}"

    if topic == "children":
        subjects = [str(s) for s in (payload.get("data_subjects") or [])]
        minors = [s for s in subjects if any(
            w in s.lower() for w in ("child", "minor", "student", "pupil")
        )]
        return f"data subjects recorded as {', '.join(minors)}" if minors else None

    return None


def _merge_links(
    broad: list[RegWatchImpact], evidenced: list[RegWatchImpact]
) -> list[RegWatchImpact]:
    """Keep the better link when both passes name the same thing.

    A record reached by a topic rule AND by its own payload should appear once, at the
    evidenced confidence. Two rows for one record -- one saying "possible", one saying
    "probable" -- would make the list longer and the reader less sure.
    """
    by_target = {(link.target_kind, link.target_id): link for link in broad}
    for link in evidenced:
        by_target[(link.target_kind, link.target_id)] = link
    return list(by_target.values())


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

    if kind == watch.TARGET_ROPA_SOURCE:
        # The systems Agent 2 discovers from. A change about cross-border transfer or
        # security safeguards lands on the systems holding the data, not only on the
        # records describing it.
        result = await db.execute(
            select(RopaDataSource.id, RopaDataSource.name, RopaDataSource.source_type)
            .where(RopaDataSource.org_id == org_id, RopaDataSource.enabled.is_(True))
            .order_by(RopaDataSource.name)
            .limit(_SAMPLE_LIMIT)
        )
        return [(row.id, f"{row.name} ({row.source_type})") for row in result]

    if kind == watch.TARGET_POLICY:
        # Privacy and cookie policies Agent 1 found on scanned sites. `policies` has
        # no org_id of its own -- it hangs off the scan -- so the scope comes through
        # the join, exactly as consent findings do. Getting that wrong would leak
        # another tenant's policy URLs.
        result = await db.execute(
            select(Policy.id, Policy.policy_type, Policy.url)
            .join(ConsentScan, ConsentScan.id == Policy.scan_id)
            .where(ConsentScan.org_id == org_id)
            .order_by(Policy.policy_type)
            .limit(_SAMPLE_LIMIT)
        )
        return [
            (row.id, f"{row.policy_type or 'policy'} at {row.url}") for row in result
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


async def add_manual_impact(
    db: AsyncSession,
    finding: RegWatchFinding,
    *,
    target_kind: str,
    target_label: str,
    rationale: str,
    reviewer_user_id: uuid.UUID,
    target_id: uuid.UUID | None = None,
    confidence: str = watch.CONFIRMED,
) -> RegWatchImpact:
    """A reviewer adding a link the rules could not find.

    The rules only see what this platform holds. A compliance lead knows things it
    does not -- a contract with an offshore vendor that never became a ROPA entry, a
    control that lives in somebody's runbook, a policy kept outside the scanner's
    reach. Without this the impact map is capped at the platform's own knowledge and
    silently presents that cap as the whole picture.

    This is the ONE place CONFIRMED is legitimate. Everywhere else in Agent 5 it is
    refused, because a rule or a model asserting that a change definitely touches
    something is exactly the false certainty the agent exists to prevent. Here a
    named person is asserting it, with a reason, and that is a different kind of
    claim -- which is why `derived_from` records that a human put it there.
    """
    if reviewer_user_id is None:
        raise ApprovalRequiredError(
            "a manual impact link is an assertion by a person; no reviewer was supplied"
        )
    if target_kind not in watch.IMPACT_TARGET_KINDS:
        raise InvalidWatchTransitionError(
            f"{target_kind!r} is not an impact target; expected one of "
            f"{sorted(watch.IMPACT_TARGET_KINDS)}"
        )
    if confidence not in watch.CONFIDENCE_LEVELS:
        raise InvalidWatchTransitionError(
            f"{confidence!r} is not a confidence; expected {list(watch.CONFIDENCE_LEVELS)}"
        )
    cleaned = (rationale or "").strip()
    if len(cleaned) < _MIN_MANUAL_RATIONALE:
        raise WatchNotReadyError(
            f"a manual impact link needs a rationale of at least "
            f"{_MIN_MANUAL_RATIONALE} characters. It carries more weight than anything "
            "the rules produce -- it can assert CONFIRMED -- so the reason it exists "
            "has to be readable by whoever reviews this next."
        )
    label = (target_label or "").strip()
    if not label:
        raise WatchNotReadyError("a manual impact link needs a label naming what it points at")

    row = RegWatchImpact(
        org_id=finding.org_id, finding_id=finding.id,
        target_kind=target_kind, target_id=target_id, target_label=label,
        confidence=confidence,
        derived_from=watch.DERIVED_MANUAL,
        rationale=cleaned,
    )
    await repo.add_impact(db, row)
    await audit_service.record(
        db, org_id=finding.org_id, actor_user_id=reviewer_user_id,
        action=watch.AUDIT_IMPACT_MAPPED,
        entity_type=watch.AUDIT_ENTITY, entity_id=finding.id,
        after={
            "impact_id": str(row.id),
            "target_kind": target_kind,
            "target_label": label,
            "confidence": confidence,
            "derived_from": watch.DERIVED_MANUAL,
            "rationale": cleaned,
            # The distinction that matters on a later read: a person said this, the
            # platform did not derive it.
            "asserted_by_a_person": True,
        },
    )
    return row


# A manual link can assert CONFIRMED, which no rule may. That weight is the reason
# the rationale floor is higher than the one on a review decision.
_MIN_MANUAL_RATIONALE = 20


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
