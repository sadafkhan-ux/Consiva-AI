"""Reads a scan's stored observations and hands them to the assessors."""

from __future__ import annotations

import json
import uuid
from datetime import date

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.enterprise.assess.classify import build_cookie_classifier
from app.enterprise.assess.consent import assess_consent
from app.enterprise.assess.technical import Ev, assess_technical
from app.enterprise.scoring.engine import Assessment

_SQL = text(
    "select id, kind, detection_method, content from ent_evidence where scan_id = :scan_id and kind in ('header', 'cookie', 'network', 'consent') order by captured_at, id")


async def load_observations(db: AsyncSession, scan_id: uuid.UUID) -> dict:
    """The scan's per-page response, cookie and request records, its TLS probe, and its consent tests by name (for each of those, the latest if it was recorded more than once)."""
    out: dict = {"pages": [], "cookies": [], "networks": [], "tls": None, "consent": {}}
    for row in (await db.execute(_SQL, {"scan_id": scan_id})).all():
        item: Ev = (str(row.id), json.loads(row.content))
        if row.kind == "consent":
            out["consent"][row.detection_method.removeprefix("consent_test:")] = item
        elif row.kind == "header" and row.detection_method == "tls_probe":
            out["tls"] = item
        elif row.kind == "header" and row.detection_method == "five_page_inspection":
            continue                                    # the 5-page sequence's own observations of a page, not that page's response record
        elif row.kind == "header":
            out["pages"].append(item)
        elif row.kind == "cookie":
            out["cookies"].append(item)
        else:
            out["networks"].append(item)
    return out


async def assess_scan_technical(db: AsyncSession, scan_id: uuid.UUID, as_of: date) -> list[Assessment]:
    o = await load_observations(db, scan_id)
    return assess_technical(pages=o["pages"], cookies=o["cookies"], networks=o["networks"], tls=o["tls"], as_of=as_of)


async def assess_scan_consent(db: AsyncSession, scan_id: uuid.UUID) -> list[Assessment]:
    from app.lookup import repository as lookup_repository          # the cookie catalogue, loaded from the database

    o = await load_observations(db, scan_id)
    classify = build_cookie_classifier(await lookup_repository.load_all(db))
    return assess_consent(networks=o["networks"], cookies=o["cookies"], tests=o["consent"], classify=classify)
