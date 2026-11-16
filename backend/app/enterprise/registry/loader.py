"""The DPDP registry: load a version from its seed file into the database, and read a version back.

The seed (seed_dpdp.json) holds the provisions the existing scanner's parameters relate to, each with a phrase copied from the Act or Rules. It is NOT every section and rule: the
full registry needs the gazetted text loaded and a named legal sign-off. A loaded version is always DRAFT; only `approve` (a named person, a date) makes it approved, and an approved
version is frozen by the database itself, so an assessment can always be traced to the exact text it used.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.enterprise.models import EntRegistryProvision, EntRegistryVersion

SEED_FILE = Path(__file__).with_name("seed_dpdp.json")
INSTRUMENTS = ("Act", "Rules")
RELATIONS = ("source of the obligation", "related context", "good practice")


class RegistryError(ValueError):
    pass


def read_seed(path: Path = SEED_FILE) -> dict:
    """The seed, validated: every provision names its instrument, exact reference, a phrase and a commencement date, and no provision appears twice."""
    doc = json.loads(path.read_text(encoding="utf-8"))
    seen: set[tuple[str, str]] = set()
    for p in doc["provisions"]:
        if p["instrument"] not in INSTRUMENTS:
            raise RegistryError(f"unknown instrument {p['instrument']!r}")
        if p["relation"] not in RELATIONS:
            raise RegistryError(f"unknown relation {p['relation']!r} for {p['ref']}")
        if not p["ref"] or not p["phrase"].strip() or not p["summary"].strip():
            raise RegistryError(f"{p['instrument']} {p.get('ref')}: reference, summary and phrase are all required")
        date.fromisoformat(p["commences"])
        key = (p["instrument"], p["ref"])
        if key in seen:
            raise RegistryError(f"{key} appears twice")
        seen.add(key)
    return doc


def version_label(doc: dict) -> str:
    return f"dpdp-{doc['source_version']}"


async def load_seed(db: AsyncSession, doc: dict | None = None) -> EntRegistryVersion:
    """Create the version from the seed as a DRAFT, or return it if it already exists (loading twice changes nothing)."""
    doc = doc or read_seed()
    label = version_label(doc)
    existing = await db.scalar(select(EntRegistryVersion).where(EntRegistryVersion.version == label))
    if existing is not None:
        return existing
    version = EntRegistryVersion(version=label, source=doc["source"], note=doc.get("coverage_note"))
    db.add(version)
    await db.flush()
    for p in doc["provisions"]:
        db.add(EntRegistryProvision(
            version_id=version.id, instrument=p["instrument"], ref=p["ref"], summary=p["summary"], relation=p["relation"],
            phrase=p["phrase"], commences=date.fromisoformat(p["commences"])))
    await db.flush()
    return version


async def approve(db: AsyncSession, version_id, approved_by: str) -> EntRegistryVersion:
    """Approval is a named person's act. It freezes the version."""
    if not approved_by or not approved_by.strip():
        raise RegistryError("approval needs the name of the person who approved it")
    version = await db.get(EntRegistryVersion, version_id)
    if version is None:
        raise LookupError("no such registry version")
    if version.status == "approved":
        raise RegistryError("this version is already approved")
    version.status, version.approved_by, version.approved_at = "approved", approved_by.strip(), datetime.now(timezone.utc)
    await db.flush()
    return version


async def provisions(db: AsyncSession, version_id) -> list[EntRegistryProvision]:
    rows = await db.scalars(select(EntRegistryProvision).where(EntRegistryProvision.version_id == version_id)
                            .order_by(EntRegistryProvision.instrument, EntRegistryProvision.ref))
    return list(rows)
