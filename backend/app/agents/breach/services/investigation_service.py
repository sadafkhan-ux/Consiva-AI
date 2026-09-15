"""Evidence, timeline, and working out what was affected (§11-§17, §45).

This is where Agent 2's knowledge earns its keep. When an incident names a database
Consiva has already discovered, the ROPA map says what lives in it -- which tables,
which columns, which data categories, whose data -- so the affected-data question can
be answered in seconds rather than by someone reading a schema.

THE RULE THAT GOVERNS THAT REUSE
--------------------------------
ROPA metadata says what a system CONTAINS. It does not say what an incident TOUCHED.
Those are different claims, and collapsing them would let an agent report "financial
data was breached" on the strength of the affected database merely having a payments
table. So every category derived this way is written at POSSIBLE confidence with
`derived_from='ropa_metadata'`, and only evidence from the incident itself, or a
person, can raise it. §15 puts it plainly: Agent 4 must still verify the actual
incident scope.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.breach.errors import EvidenceUnavailableError, InvalidIncidentError
from app.agents.breach.schemas import incident as vocab
from app.agents.ropa.rules import personal_data_rules
from app.db.models import (
    IncidentAffectedData,
    IncidentAffectedSystem,
    IncidentCase,
    IncidentEvidence,
    IncidentTimelineEntry,
)
from app.db.repositories import incident_repository, ropa_repository
from app.services import audit_service

logger = logging.getLogger(__name__)

# Keys whose values must never be stored in evidence detail, whatever a caller sends.
# Incident evidence routinely arrives as a raw log line, and a raw log line is exactly
# where a bearer token ends up.
_SECRET_KEYS = frozenset({
    "password", "passwd", "secret", "token", "api_key", "apikey", "authorization",
    "auth", "credential", "credentials", "private_key", "session", "cookie",
    "access_token", "refresh_token", "bearer",
})
_REDACTED = "[redacted]"


def redact(detail: dict) -> tuple[dict, bool]:
    """Strip anything that looks like a secret. Returns (clean, found_any).

    Applied on the way IN, not on the way out. Evidence is append-only, so a secret
    that reaches the table cannot be removed afterwards -- the only safe place to do
    this is before the insert.
    """
    if not isinstance(detail, dict):
        return {}, False
    clean: dict = {}
    found = False
    for key, value in detail.items():
        if str(key).lower() in _SECRET_KEYS:
            clean[key] = _REDACTED
            found = True
        elif isinstance(value, dict):
            nested, nested_found = redact(value)
            clean[key] = nested
            found = found or nested_found
        elif isinstance(value, list):
            items = []
            for item in value:
                if isinstance(item, dict):
                    nested, nested_found = redact(item)
                    items.append(nested)
                    found = found or nested_found
                else:
                    items.append(item)
            clean[key] = items
        else:
            clean[key] = value
    return clean, found


async def add_evidence(
    db: AsyncSession,
    case: IncidentCase,
    *,
    kind: str,
    source_system: str,
    summary: str,
    detail: dict | None = None,
    observed_at: datetime | None = None,
    supersedes_id: uuid.UUID | None = None,
    actor_user_id: uuid.UUID | None = None,
    correlation_id: str | None = None,
) -> IncidentEvidence:
    """File one piece of evidence. Append-only: this is the only way in, and there is
    no way to change it afterwards."""
    if kind not in vocab.EVIDENCE_KINDS:
        raise InvalidIncidentError(
            f"{kind!r} is not an evidence kind; expected one of {sorted(vocab.EVIDENCE_KINDS)}"
        )
    if not (summary and summary.strip()):
        raise InvalidIncidentError(
            "evidence needs a summary; a row nobody can read is not evidence"
        )

    clean, had_secrets = redact(detail or {})
    row = IncidentEvidence(
        org_id=case.org_id,
        incident_id=case.id,
        kind=kind,
        source_system=source_system.strip(),
        summary=summary.strip(),
        detail=clean,
        contains_secrets=had_secrets,
        is_derived=kind in vocab.DERIVED_EVIDENCE_KINDS,
        supersedes_id=supersedes_id,
        added_by_user_id=actor_user_id,
        correlation_id=correlation_id,
        observed_at=observed_at,
    )
    await incident_repository.add_evidence(db, case.org_id, row)
    await audit_service.record(
        db,
        org_id=case.org_id,
        actor_user_id=actor_user_id,
        action=vocab.AUDIT_EVIDENCE_ADDED,
        entity_type=vocab.AUDIT_ENTITY,
        entity_id=case.id,
        after={
            "evidence_id": str(row.id), "kind": kind, "source_system": source_system,
            # The summary, never the detail: the audit trail must not become a second
            # copy of a log line somebody redacted from the first.
            "summary": row.summary[:200],
            "secrets_redacted": had_secrets,
        },
    )
    if had_secrets:
        logger.info(
            "Redacted secret-shaped fields from evidence filed against incident %s",
            case.reference,
        )
    return row


async def add_timeline_entry(
    db: AsyncSession,
    case: IncidentCase,
    *,
    occurred_at: datetime,
    event: str,
    confidence: str = vocab.POSSIBLE,
    actor: str | None = None,
    source_system: str | None = None,
    evidence_id: uuid.UUID | None = None,
    actor_user_id: uuid.UUID | None = None,
) -> IncidentTimelineEntry:
    """Record one thing that happened in the world.

    An entry at better than POSSIBLE confidence must cite evidence. "We are fairly
    sure the database was read at 10:05" is a claim, and a claim with nothing behind
    it is a guess wearing a timestamp.
    """
    if confidence not in vocab.CONFIDENCE_LEVELS:
        raise InvalidIncidentError(f"{confidence!r} is not a confidence level")
    if confidence == vocab.CONFIRMED and actor_user_id is None:
        raise InvalidIncidentError(
            "only a named person may record a timeline entry as confirmed"
        )
    if vocab.at_least(confidence, vocab.PROBABLE) and evidence_id is None:
        raise EvidenceUnavailableError(
            f"a timeline entry asserted as {confidence} must cite the evidence it "
            "rests on"
        )

    row = IncidentTimelineEntry(
        org_id=case.org_id, incident_id=case.id, occurred_at=occurred_at,
        event=event.strip(), actor=actor, source_system=source_system,
        confidence=confidence, evidence_id=evidence_id, created_by_user_id=actor_user_id,
    )
    await incident_repository.add_timeline_entries(db, case.org_id, [row])
    await audit_service.record(
        db, org_id=case.org_id, actor_user_id=actor_user_id,
        action=vocab.AUDIT_TIMELINE_UPDATED, entity_type=vocab.AUDIT_ENTITY,
        entity_id=case.id,
        after={"occurred_at": occurred_at.isoformat(), "event": row.event[:200],
               "confidence": confidence},
    )
    return row


async def seed_timeline_from_evidence(
    db: AsyncSession, case: IncidentCase, *, actor_user_id: uuid.UUID | None = None
) -> list[IncidentTimelineEntry]:
    """Build timeline entries from evidence that carries its own timestamp.

    Deliberately modest: it transcribes what evidence says happened and when, at the
    confidence that evidence supports. It does not correlate, infer causation, or fill
    gaps -- an attacker's movements between two log lines are exactly the thing a
    machine should not be inventing.
    """
    evidence = await incident_repository.list_evidence(db, case.id, case.org_id)
    existing = {
        (e.occurred_at, e.event) for e in
        await incident_repository.list_timeline(db, case.id, case.org_id)
    }

    rows: list[IncidentTimelineEntry] = []
    for item in evidence:
        if item.observed_at is None or item.is_derived:
            # Derived evidence describes Consiva, not the world, and undated evidence
            # cannot be placed on a timeline at all.
            continue
        event = f"{item.kind.replace('_', ' ')}: {item.summary}"
        if (item.observed_at, event) in existing:
            continue
        rows.append(IncidentTimelineEntry(
            org_id=case.org_id, incident_id=case.id, occurred_at=item.observed_at,
            event=event, source_system=item.source_system,
            # Transcribing a log line is worth PROBABLE: something recorded it. Making
            # it CONFIRMED would be the machine asserting certainty it has not earned.
            confidence=vocab.PROBABLE, evidence_id=item.id,
            created_by_user_id=actor_user_id,
        ))

    if rows:
        await incident_repository.add_timeline_entries(db, case.org_id, rows)
        await audit_service.record(
            db, org_id=case.org_id, actor_user_id=actor_user_id,
            action=vocab.AUDIT_TIMELINE_UPDATED, entity_type=vocab.AUDIT_ENTITY,
            entity_id=case.id,
            after={"entries_added": len(rows), "from": "evidence timestamps"},
        )
    return rows


async def record_affected_system(
    db: AsyncSession,
    case: IncidentCase,
    *,
    system_name: str,
    system_kind: str,
    component: str | None = None,
    confidence: str = vocab.POSSIBLE,
    evidence_id: uuid.UUID | None = None,
    notes: str | None = None,
    actor_user_id: uuid.UUID | None = None,
) -> IncidentAffectedSystem:
    """Record that a system may be affected (§14).

    Links to the authorized-source registry where the named system happens to be one
    Consiva already knows -- that link is what makes the ROPA lookup below possible.
    A system Consiva has never connected to is still recorded; it just carries no link.
    """
    if system_kind not in vocab.SYSTEM_KINDS:
        raise InvalidIncidentError(f"{system_kind!r} is not a system kind")
    if confidence not in vocab.CONFIDENCE_LEVELS:
        raise InvalidIncidentError(f"{confidence!r} is not a confidence level")
    if confidence == vocab.CONFIRMED and actor_user_id is None:
        raise InvalidIncidentError("only a named person may confirm an affected system")

    data_source_id = None
    for source in await ropa_repository.list_data_sources(db, case.org_id):
        if source.name.lower() == system_name.strip().lower():
            data_source_id = source.id
            break

    row = IncidentAffectedSystem(
        org_id=case.org_id, incident_id=case.id, system_name=system_name.strip(),
        system_kind=system_kind, component=component, data_source_id=data_source_id,
        confidence=confidence, evidence_id=evidence_id, notes=notes,
    )
    await incident_repository.add_affected_system(db, row)
    await audit_service.record(
        db, org_id=case.org_id, actor_user_id=actor_user_id,
        action=vocab.AUDIT_SYSTEMS_IDENTIFIED, entity_type=vocab.AUDIT_ENTITY,
        entity_id=case.id,
        after={
            "system": row.system_name, "kind": system_kind, "confidence": confidence,
            "known_to_consiva": data_source_id is not None,
        },
    )
    return row


async def derive_affected_data(
    db: AsyncSession, case: IncidentCase, *, actor_user_id: uuid.UUID | None = None
) -> tuple[list[IncidentAffectedData], list[str]]:
    """Work out what data categories may be involved, using Agent 2's map (§15, §17).

    Returns (rows, notes). The notes say what could NOT be established -- a system
    Consiva has never discovered produces no categories, and that absence must be
    visible rather than read as "no personal data here".

    Every row lands at POSSIBLE with `derived_from='ropa_metadata'`, because the ROPA
    map says what a system CONTAINS and the incident is about what was TOUCHED.
    """
    systems = await incident_repository.list_affected_systems(db, case.id, case.org_id)
    if not systems:
        return [], ["no affected system has been identified, so no data map applies"]

    rows: list[IncidentAffectedData] = []
    notes: list[str] = []
    seen: set[tuple[str, str | None, str | None]] = set()

    for system in systems:
        if system.data_source_id is None:
            notes.append(
                f"{system.system_name}: not a source Consiva has discovered, so its "
                "contents are unknown; the data involved must be established by hand"
            )
            continue

        # Returns the snapshot dict itself, not a row.
        snapshot = await ropa_repository.get_current_baseline_snapshot(
            db, case.org_id, system.system_name
        )
        if not snapshot:
            notes.append(
                f"{system.system_name}: known to Consiva but never profiled by the "
                "ROPA agent, so its contents are unknown"
            )
            continue

        categorised = _categories_from_snapshot(snapshot)
        if not categorised:
            notes.append(f"{system.system_name}: ROPA baseline holds no column detail")
            continue

        for table, column, category in categorised:
            key = (category, table, column)
            if key in seen:
                continue
            seen.add(key)
            rows.append(IncidentAffectedData(
                org_id=case.org_id, incident_id=case.id, affected_system_id=system.id,
                data_category=category, table_name=table, column_name=column,
                confidence=vocab.POSSIBLE, derived_from="ropa_metadata",
            ))

    await incident_repository.replace_affected_data(db, case.id, case.org_id, rows)
    await audit_service.record(
        db, org_id=case.org_id, actor_user_id=actor_user_id,
        action=vocab.AUDIT_DATA_IDENTIFIED, entity_type=vocab.AUDIT_ENTITY,
        entity_id=case.id,
        after={
            "categories": sorted({r.data_category for r in rows}),
            "column_count": len(rows),
            "derived_from": "ropa_metadata",
            "confidence": vocab.POSSIBLE,
            "gaps": notes,
        },
    )
    return rows, notes


def _categories_from_snapshot(snapshot: dict) -> list[tuple[str, str, str]]:
    """Pull (table, column, data_category) out of a ROPA schema baseline.

    A baseline stores three things: the tables seen, a {"table.column": type} map, and
    a {"table.column": category} map Agent 2's classifier already produced. The stored
    classification is preferred -- it IS Agent 2's verdict, which is the metadata §17
    describes reusing, and re-deriving it would risk the two agents disagreeing about
    the same column.

    The classifier is only run for a column the baseline never classified, which
    happens when a baseline predates a classifier improvement. Non-personal columns
    are dropped either way.

    Tolerant of shape: a snapshot this cannot read yields nothing rather than raising.
    A parsing failure must not take an incident investigation down with it.
    """
    try:
        columns = snapshot.get("columns") or {}
        stored = snapshot.get("classifications") or {}
        if not isinstance(columns, dict) or not isinstance(stored, dict):
            return []

        found: list[tuple[str, str, str]] = []
        for qualified in columns:
            table, _, column = str(qualified).partition(".")
            if not table or not column:
                continue

            category = stored.get(qualified)
            if category is None:
                # Not classified when this baseline was taken -- ask Agent 2's rules
                # now rather than guessing or skipping.
                verdict = personal_data_rules.classify_column(column, table_name=table)
                category = verdict.category if verdict.is_personal_data else None
            if not category or not personal_data_rules.is_personal_category(category):
                continue
            found.append((table, column, category))
        return found
    except (AttributeError, TypeError):
        logger.debug("could not read a ROPA schema snapshot for affected-data mapping")
        return []


async def record_affected_subjects(
    db: AsyncSession,
    case: IncidentCase,
    *,
    subject_group: str,
    record_count: int | None,
    count_basis: str,
    basis_note: str | None = None,
    confidence: str = vocab.POSSIBLE,
    evidence_id: uuid.UUID | None = None,
    actor_user_id: uuid.UUID | None = None,
):
    """Record how many people may be affected (§16).

    `count_basis` is mandatory and meaningful. A figure with no stated basis is the
    thing that ends up in a regulator's inbox as though it were counted, so a count
    claimed as `counted` must say where the number came from.
    """
    if count_basis not in ("counted", "estimated", "unknown"):
        raise InvalidIncidentError(
            f"{count_basis!r} is not a count basis; expected counted, estimated or unknown"
        )
    if count_basis != "unknown" and record_count is None:
        raise InvalidIncidentError(
            f"a {count_basis} figure needs a number; use basis 'unknown' if there is none"
        )
    if count_basis == "counted" and not (basis_note and basis_note.strip()):
        raise InvalidIncidentError(
            "a counted figure must say how it was counted -- a bare number presented "
            "as exact is the claim most likely to be repeated to a regulator"
        )
    if confidence == vocab.CONFIRMED and actor_user_id is None:
        raise InvalidIncidentError("only a named person may confirm an impact figure")

    row = await incident_repository.upsert_affected_subjects(
        db, org_id=case.org_id, incident_id=case.id, subject_group=subject_group.strip(),
        record_count=record_count, count_basis=count_basis,
        basis_note=(basis_note or "").strip() or None, confidence=confidence,
        evidence_id=evidence_id,
    )
    await audit_service.record(
        db, org_id=case.org_id, actor_user_id=actor_user_id,
        action=vocab.AUDIT_SUBJECTS_ASSESSED, entity_type=vocab.AUDIT_ENTITY,
        entity_id=case.id,
        after={
            "subject_group": row.subject_group, "record_count": record_count,
            "count_basis": count_basis, "confidence": confidence,
        },
    )
    return row


def impact_total(subjects) -> tuple[int | None, str]:
    """Total affected individuals, and the weakest basis behind that total.

    A total is only as trustworthy as its softest input: one estimated group makes the
    whole figure an estimate, and one unknown group makes the total unknowable rather
    than merely approximate. Summing what is known and presenting it as the total is
    how "at least 400" becomes "400" somewhere between a spreadsheet and a press
    release.
    """
    if not subjects:
        return None, "unknown"
    if any(s.count_basis == "unknown" or s.record_count is None for s in subjects):
        return None, "unknown"
    total = sum(s.record_count for s in subjects)
    basis = "estimated" if any(s.count_basis == "estimated" for s in subjects) else "counted"
    return total, basis
