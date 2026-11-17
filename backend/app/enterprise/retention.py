"""Clear the temporary data of finished Enterprise scans, and keep everything that is history, evidence or a result.

CLEARED, after a scan has been analysed and the grace period (ENTERPRISE_TEMP_RETENTION_HOURS, default 24; 0 = as soon as the scan finishes) has passed:
  * the extracted text and HTML of pages that no assessment cites: every page that was not read as a privacy, cookie, terms, contact, grievance or about page (blog posts, product and
    service pages, listings). They were read to discover links and to be classified, nothing more;
  * the bulky results of analysed passages (their facts and raw model output). A passage's counts and whether it was reused stay; what it yielded goes to the reuse cache first.

KEPT, always: the scan and its URL inventory; assessments, findings, scores and score history; every stored report; the audit trail; the browser observations (cookies, requests, forms, headers,
consent tests); the text and HTML of the pages that were read as policies (their quotes are the evidence the findings cite, and the report lists them); the policy knowledge base; the cache of reusable
results (until it is unused for ENTERPRISE_FACT_CACHE_DAYS). Reports are never touched here, and nothing outside the Enterprise tables is touched: not the 5-page scanner's tables, not the other agents'.

A scan is cleared once, and never if it was not analysed. A scan from before pages were classified and recorded has no category on its pages; for it the pages kept are chosen by their address (privacy, cookie,
terms, policy, grievance, DPO, legal, contact, consent, about), which keeps more than needed and never less.
"""

from __future__ import annotations

import json
import logging
import re
import uuid

from sqlalchemy import text

from app.db.session import async_session_factory, set_org_scope
from app.enterprise.analysis import pages as pg
from app.enterprise.crawl import scope
from app.enterprise.config import get_enterprise_settings

logger = logging.getLogger(__name__)

KEEP_CATEGORIES = list(pg.ANALYSED)
TEMP_KINDS = ("page_text", "page_html", "document_text")
FINISHED = ("completed", "failed", "cancelled")
ANALYSED_STATES = ("completed", "partial")

_ABOUT = re.compile(r"/(about|about-us|company|who-we-are)(/|$|\?)", re.I)
_SCAN = text("select org_id, status, analysis_status, text_cleared_at, coalesce(analysis_completed_at, completed_at) as finished_at from ent_scans where id = :s")
_CLASSIFIED = text("select count(*) from ent_frontier where scan_id = :s and page_category is not null")

_HARVEST = text("""
    insert into ent_fact_cache (org_id, text_hash, extractor_key, result, provider, model)
    select :org, c.text_hash, c.extractor_key, c.result, c.provider, c.model
    from ent_analysis_chunks c
    where c.scan_id = :s and c.level = 'page' and c.status = 'done' and c.text_hash is not null and c.extractor_key is not null
      and c.result is not null and coalesce(c.result->>'compacted', 'false') <> 'true' and coalesce(c.result->>'reused', 'false') <> 'true'
    on conflict (org_id, text_hash, extractor_key) do nothing
    returning 1""")

_COMPACT = text("""
    update ent_analysis_chunks
       set result = jsonb_build_object('compacted', true, 'reused', coalesce(result->>'reused', 'false') = 'true',
                                       'facts', coalesce(jsonb_array_length(result->'facts'), 0)),
           updated_at = now()
     where scan_id = :s and level = 'page' and status = 'done' and result is not null and coalesce(result->>'compacted', 'false') <> 'true'
    returning 1""")

_CLEAR = text("""
    with keep as (
        select original_url as u from ent_frontier where scan_id = :s and page_category = any(cast(:cats as text[]))
        union select final_url from ent_frontier where scan_id = :s and page_category = any(cast(:cats as text[])) and final_url is not null
    ), gone as (
        delete from ent_evidence e
         where e.scan_id = :s and e.kind = any(cast(:kinds as text[])) and e.source_url not in (select u from keep) and not (e.source_url = any(cast(:extra as text[])))
        returning e.byte_size)
    select count(*) as rows, coalesce(sum(byte_size), 0) as bytes from gone""")


async def purge_scan(scan_id: uuid.UUID, *, force: bool = False) -> dict:
    """Clear one scan's temporary data. Returns what was done, or {"skipped": reason}. `force` skips only the grace period, never the safety checks."""
    async with async_session_factory() as db:
        scan = (await db.execute(_SCAN, {"s": scan_id})).mappings().first()
        if scan is None:
            return {"skipped": "scan not found"}
        set_org_scope(scan["org_id"])
        if scan["text_cleared_at"] is not None:
            return {"skipped": "already cleared"}
        if scan["status"] not in FINISHED or scan["analysis_status"] not in ANALYSED_STATES:
            return {"skipped": "the scan is not finished and analysed"}
        if not force:
            hours = get_enterprise_settings().temp_retention_hours
            late = (await db.scalar(text("select coalesce(cast(:f as timestamptz), now()) + make_interval(hours => :h) <= now()"), {"f": scan["finished_at"], "h": hours}))
            if not late:
                return {"skipped": "within the retention period"}
        classified = bool(await db.scalar(_CLASSIFIED, {"s": scan_id}))
        extra: list[str] = []
        if not classified:                       # a scan from before pages were classified: keep by address, generously
            urls = [r[0] for r in (await db.execute(text("select distinct source_url from ent_evidence where scan_id = :s and kind = any(cast(:kinds as text[]))"), {"s": scan_id, "kinds": list(TEMP_KINDS)})).all()]
            extra = [u for u in urls if scope.legal_priority(u) == 0 or _ABOUT.search(u)]
        added = len((await db.execute(_HARVEST, {"org": scan["org_id"], "s": scan_id})).all())
        compacted = len((await db.execute(_COMPACT, {"s": scan_id})).all())
        cleared = (await db.execute(_CLEAR, {"s": scan_id, "cats": KEEP_CATEGORIES, "kinds": list(TEMP_KINDS), "extra": extra})).mappings().one()
        summary = {"evidence_rows_cleared": int(cleared["rows"]), "evidence_bytes_cleared": int(cleared["bytes"]), "passages_compacted": compacted, "reusable_results_cached": added,
                   "pages_kept_by": "recorded page category" if classified else "address patterns (this scan predates page classification)"}
        await db.execute(text("update ent_scans set text_cleared_at = now(), purge_summary = cast(:p as jsonb) where id = :s"), {"p": json.dumps(summary), "s": scan_id})
        await db.execute(text("insert into ent_audit_events (scan_id, actor, event, detail) values (:s, 'system', 'temp_data_cleared', cast(:p as jsonb))"), {"s": scan_id, "p": json.dumps(summary)})
        await db.commit()
    logger.info("enterprise scan %s: temporary data cleared %s", scan_id, summary)
    return summary


async def purge_expired(org_id: uuid.UUID, *, limit: int = 50) -> dict:
    """Clear every finished scan of an organisation whose grace period has passed, and remove reusable results unused for too long."""
    set_org_scope(org_id)
    settings = get_enterprise_settings()
    out = {"scans_cleared": 0, "evidence_rows_cleared": 0, "evidence_bytes_cleared": 0, "cache_entries_expired": 0, "skipped": 0}
    async with async_session_factory() as db:
        ids = [r[0] for r in (await db.execute(text(
            "select id from ent_scans where org_id = :o and text_cleared_at is null and status = any(cast(:fin as text[])) and analysis_status = any(cast(:an as text[])) "
            "and coalesce(analysis_completed_at, completed_at) + make_interval(hours => :h) <= now() order by coalesce(analysis_completed_at, completed_at) limit :n"),
            {"o": org_id, "fin": list(FINISHED), "an": list(ANALYSED_STATES), "h": settings.temp_retention_hours, "n": limit})).all()]
    for sid in ids:
        try:
            r = await purge_scan(sid)
        except Exception:
            logger.exception("enterprise scan %s: clearing temporary data failed", sid)
            out["skipped"] += 1
            continue
        if "skipped" in r:
            out["skipped"] += 1
            continue
        out["scans_cleared"] += 1
        out["evidence_rows_cleared"] += r["evidence_rows_cleared"]
        out["evidence_bytes_cleared"] += r["evidence_bytes_cleared"]
    async with async_session_factory() as db:
        res = await db.execute(text("delete from ent_fact_cache where org_id = :o and last_used_at < now() - make_interval(days => :d) returning 1"), {"o": org_id, "d": settings.fact_cache_days})
        out["cache_entries_expired"] = len(res.all())
        await db.commit()
    return out


async def after_scan(scan_id: uuid.UUID, org_id: uuid.UUID) -> None:
    """Called when a scan's analysis has finished. With a grace period of 0 this scan is cleared now; in every case the organisation's expired scans are cleared. Never raises: a clearing problem
    must not change the scan's result."""
    settings = get_enterprise_settings()
    if not settings.retention_enabled:
        return
    try:
        if settings.temp_retention_hours == 0:
            await purge_scan(scan_id)
        await purge_expired(org_id)
    except Exception:
        logger.exception("enterprise scan %s: retention sweep failed", scan_id)


async def held_now(org_id: uuid.UUID) -> dict:
    """What the Enterprise tables hold for this organisation right now: for the dashboard's retention panel. Figures are counts and byte sizes of stored rows."""
    set_org_scope(org_id)
    async with async_session_factory() as db:
        ev = (await db.execute(text(
            "select count(*) filter (where e.kind = any(cast(:t as text[]))) temp_rows, coalesce(sum(e.byte_size) filter (where e.kind = any(cast(:t as text[]))), 0) temp_bytes, "
            "count(*) rows_all, coalesce(sum(e.byte_size), 0) bytes_all from ent_evidence e join ent_scans s on s.id = e.scan_id where s.org_id = :o"), {"t": list(TEMP_KINDS), "o": org_id})).mappings().one()
        sc = (await db.execute(text(
            "select count(*) filter (where text_cleared_at is not null) cleared, count(*) filter (where text_cleared_at is null) held, coalesce(sum((purge_summary->>'evidence_bytes_cleared')::bigint), 0) bytes_cleared "
            "from ent_scans where org_id = :o"), {"o": org_id})).mappings().one()
        cache = (await db.execute(text("select count(*) n, coalesce(sum(use_count), 0) uses from ent_fact_cache where org_id = :o"), {"o": org_id})).mappings().one()
    s = get_enterprise_settings()
    return {
        "policy": {"enabled": s.retention_enabled, "grace_hours": s.temp_retention_hours, "fact_cache_days": s.fact_cache_days,
                   "clears": "extracted text and HTML of pages no assessment cites, and the bulky results of analysed passages",
                   "keeps": "scans and URL inventory, assessments, findings, scores, reports, audit trail, browser observations, the text of policy pages, the policy knowledge base"},
        "scans_cleared": sc["cleared"], "scans_not_yet_cleared": sc["held"], "bytes_cleared": int(sc["bytes_cleared"]),
        "temporary_evidence_rows_held": ev["temp_rows"], "temporary_evidence_bytes_held": int(ev["temp_bytes"]), "evidence_rows_total": ev["rows_all"], "evidence_bytes_total": int(ev["bytes_all"]),
        "reusable_results_cached": cache["n"], "reusable_results_used": int(cache["uses"]),
    }
