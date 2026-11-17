"""Dashboard: the scan history of BOTH scanning agents, and summary counts. Prefix /api/v1/enterprise/dashboard (router.py).

It reads only the scan history that is kept for ever: for each scan the agent, the website address, when it started and finished, and whether it completed or failed (with the reason). Nothing here depends
on extracted text, findings, evidence or reports, so it keeps working after the local scan data has been cleared (app/cleanup). Everything is scoped to the caller's organisation.

How a scan is classed (the same words are used on the screen):
  blocked    the website's protection (a firewall, bot protection, access control) refused the scanner: "Website blocked our scanner." Not a failure of the application and not a result about the website.
  completed  five-page: the scan completed. Enterprise: the crawl finished and the analysis completed (one that stopped at a limit is completed and says so)
  failed     the scan failed or was cancelled, or an Enterprise scan finished its crawl but its analysis never completed
  pending    queued or still running
"""

from __future__ import annotations

import os
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.integration_auth import get_caller
from app.core.security import CurrentUser
from app.db.session import get_db
from app.enterprise import errors
from app.enterprise.cache import TTLCache
from app.enterprise.config import get_enterprise_settings

router = APIRouter(prefix="/dashboard", tags=["Dashboard"])
_cache = TTLCache(ttl_seconds=15.0)

#: What the five-page scanner writes when a site refuses it (its own wording, stored as text): the stored failure is recognised, the scanner is not changed. The Enterprise agent writes BOT_BLOCKED / ACCESS_BLOCKED.
BLOCK_PATTERN = r"(refusing the scanner|closed the connection to the scanner|bot protection|answered with HTTP (401|403|429|451)|kept answering with HTTP 429)"

_HISTORY = """
    select * from (
        select 'five_page' as agent, s.id::text as scan_id, s.url as website_url, s.created_at, s.started_at, s.completed_at as finished_at,
               case when s.status = 'failed' and s.error ~* :block_pattern then 'blocked'
                    when s.status = 'completed' then 'completed' when s.status in ('failed', 'cancelled') then 'failed' else 'pending' end as outcome,
               case when s.status = 'failed' and s.error ~* :block_pattern then 'Website blocked our scanner.' else s.status end as detail, coalesce(s.error, '') as reason, not exists (select 1 from website_pages w where w.scan_id = s.id) as data_cleared
        from consent_scans s where s.org_id = :o
        union all
        select 'enterprise', s.id::text, s.website_url, s.created_at, s.started_at, coalesce(s.analysis_completed_at, s.completed_at),
               case when s.status = 'failed' and (s.error like 'BOT_BLOCKED%' or s.error like 'ACCESS_BLOCKED%') then 'blocked'
                    when s.status = 'failed' or s.status = 'cancelled' or s.analysis_status = 'failed' then 'failed'
                    when s.status = 'completed' and s.analysis_status in ('completed', 'partial') then 'completed'
                    when s.status in ('queued', 'running') then 'pending'
                    when s.status = 'completed' and s.analysis_status in ('not_started', 'running') and s.created_at > now() - interval '1 day' then 'pending'
                    else 'failed' end,
               case when s.status = 'failed' and (s.error like 'BOT_BLOCKED%' or s.error like 'ACCESS_BLOCKED%') then 'Website blocked our scanner.'
                    when s.status = 'cancelled' then 'cancelled' when s.status = 'failed' then 'crawl failed' when s.analysis_status = 'failed' then 'analysis failed'
                    when s.status = 'completed' and s.analysis_status = 'partial' then 'completed, limited'
                    when s.status = 'completed' and s.analysis_status = 'completed' then 'completed'
                    when s.status in ('queued', 'running') then s.status
                    when s.analysis_status in ('not_started', 'running') and s.created_at > now() - interval '1 day' then 'analysing'
                    else 'analysis did not complete' end,
               case when s.status = 'cancelled' then coalesce(s.error, 'the scan was cancelled')
                    when s.status = 'failed' or s.analysis_status = 'failed' then coalesce(nullif(s.analysis_error, ''), nullif(s.error, ''), 'the scan failed')
                    when s.status = 'completed' and s.analysis_status in ('not_started', 'running') and s.created_at <= now() - interval '1 day' then 'the analysis did not complete'
                    else '' end,
               s.text_cleared_at is not null
        from ent_scans s where s.org_id = :o
    ) h"""


def _item(r) -> dict:
    return {"agent": r["agent"], "scan_id": r["scan_id"], "website_url": r["website_url"], "outcome": r["outcome"], "detail": r["detail"], "reason": r["reason"] or None,
            "started_at": r["started_at"] or r["created_at"], "finished_at": r["finished_at"], "data_cleared": bool(r["data_cleared"]),
            "blocked": r["outcome"] == "blocked", "blocked_at": r["finished_at"] if r["outcome"] == "blocked" else None}


@router.get("/summary", summary="Scan history of both agents, and summary counts")
async def summary(outcome: Literal["all", "completed", "failed", "pending", "blocked"] = "all", agent: Literal["all", "five_page", "enterprise"] = "all", q: str | None = Query(default=None, max_length=200),
                  offset: int = Query(0, ge=0), limit: int = Query(25, ge=1, le=100), refresh: bool = False, user: CurrentUser = Depends(get_caller), db: AsyncSession = Depends(get_db)) -> dict:
    if not get_enterprise_settings().enabled:
        raise errors.EnterpriseDisabled("Not found")
    org = uuid.UUID(user.org_id)
    key = (str(org), outcome, agent, (q or "").strip().lower(), offset, limit)
    if not refresh:
        hit = _cache.get(key)
        if hit is not None:
            return {**hit, "cached": True}
    rows = (await db.execute(text(_HISTORY + " order by coalesce(h.started_at, h.created_at) desc"), {"o": org, "block_pattern": BLOCK_PATTERN})).mappings().all()
    k = {"scans_total": len(rows), "scans_completed": 0, "scans_failed": 0, "scans_pending": 0, "scans_blocked": 0, "scans_five_page": 0, "scans_enterprise": 0}
    for r in rows:
        k[f"scans_{r['outcome']}"] += 1
        k["scans_five_page" if r["agent"] == "five_page" else "scans_enterprise"] += 1
    items = [_item(r) for r in rows]
    shown = [i for i in items if (outcome == "all" or i["outcome"] == outcome) and (agent == "all" or i["agent"] == agent) and (not q or q.strip().lower() in i["website_url"].lower())]
    out = {"kpis": k, "last_scan": items[0] if items else None, "history": {"items": shown[offset:offset + limit], "total": len(shown), "offset": offset, "limit": limit},
           "definitions": {"completed": "the scan completed (an Enterprise scan that stopped at a limit is completed and says so)",
                           "failed": "the scan failed (for a reason other than the website blocking us) or was cancelled, or its analysis never completed",
                           "pending": "queued or still running",
                           "blocked": "the website's protection refused the scanner, so the scan could not be completed and no score or report was made"}, "cached": False}
    _cache.put(key, out)
    return out


@router.get("/retention", summary="What is kept for finished scans and what is cleared")
async def retention_status(user: CurrentUser = Depends(get_caller)) -> dict:
    if not get_enterprise_settings().enabled:
        raise errors.EnterpriseDisabled("Not found")
    grace = int(os.environ.get("CLEANUP_GRACE_MINUTES", "15"))
    return {
        "grace_minutes": grace,
        "keeps": "for every scan of both agents: its id, the website address, when it started and finished, whether it completed or failed, and the reason it failed",
        "clears": "everything else a scan produced: extracted page text and HTML, evidence, passages, findings, assessments, scores, embeddings, intermediate analysis state and the stored reports of both agents",
        "when": f"{grace} minutes after a scan has completed, failed or been cancelled, by the cleanup service",
    }
