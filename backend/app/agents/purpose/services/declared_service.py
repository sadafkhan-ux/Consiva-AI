"""What the organisation DECLARED the data is for.

The declared purpose is not this agent's to compute -- it belongs to whoever recorded
it. This module reads it from wherever it exists and says plainly when it does not.

ON THIS BRANCH IT USUALLY DOES NOT EXIST

A record of processing is produced by the ROPA agent, and `app/agents/ropa/` carries no
code on this branch -- the `ropa_records` table exists in the database, but nothing here
writes to it. So `for_scan()` will commonly return an empty mapping, and that is a real
state the rest of the agent is built to handle, not an error to work around.

This matters for how findings read. With no declared purpose available, a comparison
cannot produce `mismatch` -- only `undetermined`, or the one finding that needs no
declaration at all: processing observed in a state where no consent had been granted.
The agent stays useful and stops short of claiming a disagreement it cannot see.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class DeclaredPurpose:
    purpose: str
    source: str          # 'ropa_record' | 'policy' | 'manual'
    evidence_ref: str
    confidence: float


async def _ropa_records_available(db: AsyncSession) -> bool:
    """Whether a record-of-processing table exists AND holds anything.

    Checked at runtime rather than assumed from an import, because the table's presence
    and the agent's presence are independent here: the database is ahead of this
    branch's code, so `ropa_records` exists with rows while no ROPA code does.
    """
    exists = await db.scalar(text("select to_regclass('public.ropa_records') is not null"))
    if not exists:
        return False
    return bool(await db.scalar(text("select exists (select 1 from ropa_records)")))


async def for_org(db: AsyncSession, org_id: uuid.UUID) -> dict[str, DeclaredPurpose]:
    """Declared purposes for an organisation, keyed by a lowercased subject label.

    Read with raw SQL rather than an ORM model on purpose: `ropa_records` has no mapped
    class on this branch, and adding one would create a second, drifting definition of a
    table another agent owns. A read-only query against a table this agent does not own
    is the smaller commitment.
    """
    if not await _ropa_records_available(db):
        return {}

    rows = (await db.execute(
        text(
            "select id, processing_activity, payload "
            "from ropa_records "
            "where org_id = :org and status <> 'superseded'"
        ),
        {"org": str(org_id)},
    )).mappings().all()

    declared: dict[str, DeclaredPurpose] = {}
    for row in rows:
        activity = row["processing_activity"]
        payload = row["payload"] or {}
        purpose = payload.get("purpose") or activity
        if not purpose:
            continue

        # A ROPA record describes a processing activity spanning tables. The subjects it
        # can be matched against are whatever the payload names; nothing is inferred
        # beyond what is written there.
        subjects = _subjects_in(payload)
        for subject in subjects or [activity]:
            key = str(subject).strip().lower()
            if key and key not in declared:
                declared[key] = DeclaredPurpose(
                    purpose=str(purpose),
                    source="ropa_record",
                    evidence_ref=str(row["id"]),
                    confidence=float(payload.get("confidence") or 0.8),
                )
    return declared


def _subjects_in(payload: dict) -> list[str]:
    """Table or system names a ROPA payload refers to.

    Tolerant of shape because the payload is another agent's contract and this agent
    only reads it -- an unexpected structure yields no subjects rather than an
    exception, which keeps a malformed record from failing an entire assessment run.
    """
    subjects: list[str] = []
    for key in ("tables", "systems", "sources", "data_elements"):
        value = payload.get(key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    subjects.append(item)
                elif isinstance(item, dict):
                    name = item.get("table_name") or item.get("name") or item.get("source_name")
                    if name:
                        subjects.append(str(name))
    return subjects


def availability_note(declared: dict[str, DeclaredPurpose]) -> str | None:
    """A sentence explaining an empty declaration set, for the run record.

    Stated in the run rather than left implicit: a reviewer looking at a run that
    produced only `undetermined` assessments needs to know whether that means the data
    was fine or that half the comparison was missing.
    """
    if declared:
        return None
    return (
        "No declared purposes were available: no record-of-processing data was found "
        "for this organisation. Comparisons that need a declared purpose are reported "
        "as undetermined rather than assumed aligned."
    )
