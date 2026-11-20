"""Run method 3.0.0 for a scan and keep the result. Called after the analysis phase and after every human review, once the 51-parameter results are committed.

A failure here is logged, audited and recorded as an error event; it never changes the scan's status, its 51-parameter score or its findings.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.enterprise import store
from app.enterprise.method3 import context
from app.enterprise.method3.assess import assess
from app.enterprise.method3.engine import score
from app.enterprise.models import EntMethod3Result

logger = logging.getLogger(__name__)


async def calculate(scan_id: uuid.UUID, *, as_of: date | None = None) -> dict:
    """The method 3.0.0 result of a scan, worked out from what it stored. Pure of side effects (nothing is written). The caller has set the organisation scope."""
    from app.db.session import async_session_factory
    as_of = as_of or date.today()
    m = await context.load(scan_id, as_of=as_of)
    async with async_session_factory() as db:
        v2 = await store.load_current(db, scan_id)
    out = score(assess(m, v2), as_of=as_of)
    out["scan"] = {"scan_id": str(scan_id), "website_url": m.root_url, "pages_inspected": len({u for _, n in m.loaded.networks for u in [n.get("page_url")] if u})}
    return out


async def save(db: AsyncSession, scan_id: uuid.UUID, result: dict) -> uuid.UUID:
    row = EntMethod3Result(scan_id=scan_id, version=result["version"], catalogue_fingerprint=result["catalogue_fingerprint"], as_of=date.fromisoformat(result["as_of"]),
                           score=result["score"], marks_earned=result["marks_earned"], applicable_max=result["applicable_max"], basis=result["basis"], result=result)
    db.add(row)
    await db.flush()
    return row.id


async def latest(db: AsyncSession, scan_id: uuid.UUID) -> EntMethod3Result | None:
    return await db.scalar(select(EntMethod3Result).where(EntMethod3Result.scan_id == scan_id).order_by(EntMethod3Result.created_at.desc()).limit(1))


async def run_safe(scan_id: uuid.UUID, org_id: uuid.UUID, *, as_of: date | None = None, actor: str = "system") -> dict | None:
    """Calculate, store and audit. Returns the result, or None when it could not be calculated (logged, audited and recorded, never raised)."""
    from app.db.session import async_session_factory, set_org_scope
    set_org_scope(org_id)
    try:
        result = await calculate(scan_id, as_of=as_of)
        async with async_session_factory() as db:
            rid = await save(db, scan_id, result)
            await store.audit(db, scan_id, actor, "method3_scored", {"method3_result_id": str(rid), "score": result["score"], "version": result["version"],
                                                                     "counts": result["counts"], "applicable_max": result["applicable_max"]})
            await db.commit()
        logger.info("enterprise scan %s: method 3.0.0 score %s (%s of %s applicable marks)", scan_id, result["score"], result["marks_earned"], result["applicable_max"])
        return result
    except Exception as exc:
        logger.exception("enterprise scan %s: method 3.0.0 could not be calculated", scan_id)
        try:
            from app.monitoring import events as error_events
            await error_events.record_error(agent="enterprise", module="enterprise.method3", exc=exc, service="worker", scan_id=scan_id)
        except Exception:                                    # noqa: BLE001
            logger.exception("enterprise scan %s: the method 3.0.0 failure could not be recorded", scan_id)
        try:
            async with async_session_factory() as db:
                await store.audit(db, scan_id, actor, "method3_failed", {"error": f"{type(exc).__name__}: {str(exc)[:300]}"})
                await db.commit()
        except Exception:                                    # noqa: BLE001
            logger.exception("enterprise scan %s: the method 3.0.0 failure could not be audited", scan_id)
        return None
