"""The coverage declaration of an Enterprise scan: what was found, read, analysed, left out and failed, in actual counts.

Crawling and analysis are separate statuses. A scan is `complete` only when the crawl has finished with nothing pending, the analysis has completed, and no analysis task failed or is
outstanding. In every other case it says exactly which part is outstanding (`partial`, `running`, `queued`, `failed`, `cancelled`). Nothing here is estimated: every figure is a count of
rows in the database, and a figure that cannot be known yet is reported as `null`, never as 0.
"""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.enterprise.scoring.catalogue import PARAMETERS

_DOCUMENT = r"'\.(pdf|docx?|odt|rtf|xlsx?|pptx?)(\?|#|$)'"

_URLS = text(f"""
    select
        count(*)                                                                                      as discovered,
        count(*) filter (where status in ('pending', 'in_progress', 'done', 'failed'))                as eligible,
        count(*) filter (where status = 'done')                                                       as crawled,
        count(*) filter (where status in ('pending', 'in_progress'))                                  as pending,
        count(*) filter (where status = 'excluded')                                                   as excluded,
        count(*) filter (where status = 'failed')                                                     as failed,
        count(*) filter (where status = 'failed' and http_status in (401, 403))                       as inaccessible,
        count(*) filter (where status = 'failed' and last_error like 'BLOCKED%')                      as blocked,
        count(*) filter (where status = 'done' and final_url is not null
                         and regexp_replace(lower(final_url), '/+$', '') <> regexp_replace(lower(original_url), '/+$', ''))  as redirected,
        coalesce(sum(greatest(coalesce(array_length(discovery_sources, 1), 1) - 1, 0)), 0)            as duplicates_merged,
        count(*) filter (where status = 'excluded' and (status_reason like 'unsupported%' or status_reason like 'document_unsupported%')) as unsupported,
        count(*) filter (where analysis_status = 'done')                                              as analysed,
        count(*) filter (where evidence_note is not null)                                             as pages_with_limits,
        count(*) filter (where analysis_note is not null and analysis_status <> 'not_applicable')     as pages_not_fully_analysed,
        count(*) filter (where analysis_status = 'not_applicable' and status = 'done')                as pages_not_sent_to_model,
        count(*) filter (where analysis_status = 'failed')                                            as analysis_failed,
        count(*) filter (where status = 'done' and analysis_status = 'not_started'
                         and coalesce(extraction_status, '') not in ('unreadable', 'encrypted', 'unsupported', 'too_large'))  as awaiting_analysis,
        count(*) filter (where lower(normalized_url) ~ {_DOCUMENT})                                   as documents_discovered,
        count(*) filter (where lower(normalized_url) ~ {_DOCUMENT} and extraction_status in ('extracted', 'ocr_extracted'))   as documents_extracted,
        count(*) filter (where lower(normalized_url) ~ {_DOCUMENT} and extraction_status = 'partly_extracted')               as documents_partly_extracted,
        count(*) filter (where lower(normalized_url) ~ {_DOCUMENT} and extraction_status in ('unreadable', 'encrypted', 'unsupported', 'too_large')) as documents_unreadable,
        count(*) filter (where lower(normalized_url) ~ {_DOCUMENT} and status = 'done' and extraction_status is null)         as documents_extraction_unrecorded
    from ent_frontier where scan_id = :scan_id
""")

_EXCLUDED = text("select split_part(status_reason, ':', 1) as reason, count(*) as n from ent_frontier where scan_id = :scan_id and status = 'excluded' group by 1 order by n desc")
_EVIDENCE = text("select count(*) as total, count(distinct source_url) as sources from ent_evidence where scan_id = :scan_id")
_CHUNKS = text("""
    select count(*) filter (where status = 'done')    as done,
           count(*) filter (where status = 'failed')  as failed,
           count(*) filter (where status = 'pending') as pending,
           count(distinct evidence_id) filter (where status = 'done' and level = 'page') as evidence_analysed
    from ent_analysis_chunks where scan_id = :scan_id
""")
_SCAN = text("select status, analysis_status, stop_reason, urls_not_recorded from ent_scans where id = :scan_id")
_ASSESS = text("select state, count(*) as n from ent_assessments where scan_id = :scan_id and status = 'current' group by state")
_LATEST_SCORE = text("select result from ent_score_results where scan_id = :scan_id order by created_at desc limit 1")


LIMIT_STOPS = ("url_limit", "memory_limit", "time_limit")
BLOCK_STOPS = ("bot_blocked", "access_blocked")


def completion_state(crawl: str, analysis: str, *, pending_urls: int, failed_chunks: int, pending_chunks: int, awaiting_analysis: int, stop_reason: str | None = None) -> str:
    """One word for where the scan stands. `complete` needs everything finished; anything else names what is missing."""
    if crawl in ("failed", "cancelled"):
        return crawl
    if crawl == "queued":
        return "queued"
    if crawl == "running":
        return "crawling"
    if pending_urls > 0 or stop_reason in LIMIT_STOPS:
        return "partial"                      # the crawl stopped (a limit, a failure) with URLs still waiting: they are listed as pending, and the scan is not complete
    if analysis == "not_started":
        return "crawled"                      # the crawl is finished; the analysis has not begun
    if analysis == "running":
        return "analysing"
    if analysis == "failed":
        return "failed"
    if analysis == "partial" or failed_chunks or pending_chunks or awaiting_analysis:
        return "partial"
    return "complete"


async def coverage_declaration(db: AsyncSession, scan_id: uuid.UUID) -> dict:
    u = (await db.execute(_URLS, {"scan_id": scan_id})).mappings().one()
    ev = (await db.execute(_EVIDENCE, {"scan_id": scan_id})).mappings().one()
    ch = (await db.execute(_CHUNKS, {"scan_id": scan_id})).mappings().one()
    scan = (await db.execute(_SCAN, {"scan_id": scan_id})).mappings().one()
    states = {r["state"]: r["n"] for r in (await db.execute(_ASSESS, {"scan_id": scan_id})).mappings().all()}
    excluded = {r["reason"] or "unspecified": r["n"] for r in (await db.execute(_EXCLUDED, {"scan_id": scan_id})).mappings().all()}
    assessed = sum(states.get(k, 0) for k in ("met", "partly_met", "not_met"))
    started = scan["analysis_status"] != "not_started"
    latest = (await db.execute(_LATEST_SCORE, {"scan_id": scan_id})).scalar()
    lines = (latest or {}).get("lines") or []
    scored = {"pass": sum(1 for x in lines if x.get("state") == "met"), "partially_pass": sum(1 for x in lines if x.get("state") == "partly_met"),
              "fail": sum(1 for x in lines if x.get("state") == "not_met"),
              "fail_not_verified": sum(1 for x in lines if x.get("state") == "not_met" and x.get("on_evidence") is False)}
    return {
        "crawl_status": scan["status"], "analysis_status": scan["analysis_status"], "stop_reason": scan["stop_reason"],
        # True when the crawl was stopped because the website blocked the scanner: whatever was analysed covers only the pages inspected before that, never the whole site.
        "blocked": scan["status"] == "failed" and (scan["stop_reason"] or "") in BLOCK_STOPS,
        "completion": completion_state(scan["status"], scan["analysis_status"], pending_urls=u["pending"], failed_chunks=ch["failed"], pending_chunks=ch["pending"], awaiting_analysis=u["awaiting_analysis"], stop_reason=scan["stop_reason"]),
        "urls": {
            "discovered": u["discovered"], "eligible": u["eligible"], "crawled": u["crawled"], "analysed": u["analysed"], "pending": u["pending"], "excluded": u["excluded"],
            "failed": u["failed"], "redirected": u["redirected"], "inaccessible": u["inaccessible"], "unsupported": u["unsupported"], "duplicates_merged": int(u["duplicates_merged"]),
            "awaiting_analysis": u["awaiting_analysis"], "analysis_failed": u["analysis_failed"], "excluded_by_reason": excluded,
            # Main-domain coverage (owner's exact-host rule, 19 Nov 2026): pages the site's protection refused, and URLs on another host recorded and not scanned.
            "blocked": u["blocked"], "excluded_subdomain": excluded.get("excluded_subdomain", 0), "excluded_external_domain": excluded.get("excluded_external_domain", 0),
            # URLs found but dropped at the record cap (link occurrences, so a URL linked from many pages counts more than once), and pages whose read or analysis was limited in some way.
            "not_recorded": int(scan["urls_not_recorded"] or 0), "pages_with_limits": u["pages_with_limits"], "pages_not_fully_analysed": u["pages_not_fully_analysed"], "pages_not_sent_to_model": u["pages_not_sent_to_model"],
        },
        "documents": {
            "discovered": u["documents_discovered"], "extracted": u["documents_extracted"], "partly_extracted": u["documents_partly_extracted"], "unreadable": u["documents_unreadable"],
            "extraction_not_recorded": u["documents_extraction_unrecorded"],
        },
        "evidence": {"records": ev["total"], "sources": ev["sources"], "analysed": ch["evidence_analysed"] if started else None},
        "analysis_tasks": {"done": ch["done"], "failed": ch["failed"], "pending": ch["pending"]},
        "parameters": {
            "total": len(PARAMETERS), "assessed": assessed, "not_assessed": states.get("not_assessed", 0), "not_applicable": states.get("not_applicable", 0),
            "awaiting_human_review": states.get("human_review", 0), "needs_records": states.get("needs_records", 0), "recorded": sum(states.values()),
            # The scored result (method version 2): every parameter is Pass, Partially Pass or Fail; "fail_not_verified" counts the Fails that rest on "could not be verified".
            **scored,
        },
    }
