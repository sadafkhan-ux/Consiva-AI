"""The crawl frontier: the URL queue of an Enterprise scan, kept in the database so a scan survives a crash and resumes where it stopped.

One row per distinct normalized URL per scan (ent_frontier). A worker CLAIMS a batch (pending -> in_progress, stamped with who and when); finishing it moves the row to done,
failed or excluded. A row still in_progress after the lease is a crashed worker's page: `reclaim_stale` puts it back to pending (or fails it, once it has used its attempts),
and that one call is what makes a scan resumable. Claiming uses FOR UPDATE SKIP LOCKED, so several workers on one scan never take the same URL.

Discovered is not scanned: a URL the crawl never reached stays pending, and `counts` says so, so a report can state coverage without promising it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.enterprise.config import get_enterprise_settings
from app.enterprise.models import EntFrontier
from app.enterprise.crawl.scope import legal_priority
from app.scanner.url_normalize import normalize_url


ADDED, DUPLICATE, DEPTH_LIMIT, URL_LIMIT, EXCLUDED = "added", "duplicate", "depth_limit", "url_limit", "excluded"


def key_of(url: str) -> str:
    """The frontier's identity for a URL: the 5-page normaliser's key (fragment, case, default port, tracking parameters, trailing slash), EXCEPT that the host is kept exactly as it is.
    The 5-page normaliser folds `www.` away because it scans the whole registered domain; the Enterprise scan treats `www.example.com` as another host (crawl/host.py), so
    `https://www.example.com/x` and `https://example.com/x` must stay two rows: one excluded, one scanned."""
    key = normalize_url(url)
    try:
        original, normalised = urlsplit(url.strip()), urlsplit(key)
        host = (original.hostname or "").lower()
        if host and normalised.hostname and normalised.hostname != host:
            netloc = host + (f":{normalised.port}" if normalised.port else "")
            return urlunsplit(normalised._replace(netloc=netloc))
    except ValueError:
        pass
    return key


async def find_row(db: AsyncSession, scan_id: uuid.UUID, url: str):
    """The frontier row (id, status, original_url) for this URL's key in this scan, or None."""
    return (await db.execute(select(EntFrontier.id, EntFrontier.status, EntFrontier.original_url).where(
        EntFrontier.scan_id == scan_id, EntFrontier.normalized_url == key_of(url)))).first()


def record_cap(max_urls: int) -> int:
    """How many URLs a scan may RECORD, given how many it may crawl. The crawl limit (`max_urls`) is on pages read, not on URLs known: every URL found is recorded with a status, so the inventory
    is complete and the pages that matter most (priority 0) are read first. Recording is bounded only so a site with endless generated links cannot fill the database: 20 times the crawl limit,
    and never fewer than 10,000. 0 (no crawl limit) records without a cap."""
    return 0 if max_urls == 0 else max(max_urls * 20, 10_000)


@dataclass(frozen=True)
class Claimed:
    id: uuid.UUID
    url: str
    depth: int
    attempts: int


async def add_url(
    db: AsyncSession, scan_id: uuid.UUID, url: str, *, depth: int, source: str,
    max_urls: int | None = None, max_depth: int | None = None, exclude_reason: str | None = None,
) -> str:
    """Record a discovered URL. Returns ADDED, DUPLICATE (already known: the new source is merged in), DEPTH_LIMIT or EXCLUDED (both recorded as excluded, with the reason) or URL_LIMIT (not recorded).

    The limits default to the settings; a caller passes the scan's own (already clamped) values to override them. `exclude_reason` records the URL as excluded for that reason
    (out of scope, a document not yet read, a query variant) instead of queueing it, so the coverage report can say what was found and not scanned and why.
    """
    settings = get_enterprise_settings()
    max_urls = settings.max_urls if max_urls is None else max_urls
    max_depth = settings.max_depth if max_depth is None else max_depth
    key = key_of(url)

    known =await db.scalar(select(EntFrontier.id).where(EntFrontier.scan_id == scan_id, EntFrontier.normalized_url == key))
    if known is not None:
        # the same page named by another link or sitemap: keep every source, change nothing else
        await db.execute(
            update(EntFrontier)
            .where(EntFrontier.id == known, ~EntFrontier.discovery_sources.contains([source]))
            .values(discovery_sources=func.array_append(EntFrontier.discovery_sources, source))
        )
        return DUPLICATE

    if max_urls and (await db.scalar(select(func.count()).select_from(EntFrontier).where(EntFrontier.scan_id == scan_id)) or 0) >= max_urls:
        return URL_LIMIT

    if exclude_reason is None and depth > max_depth:
        exclude_reason, depth_limited = "depth_limit", True
    else:
        depth_limited = False
    excluded = exclude_reason is not None
    stmt = (
        pg_insert(EntFrontier)
        .values(
            scan_id=scan_id, normalized_url=key, original_url=url, depth=depth, discovery_source=source, discovery_sources=[source], priority=legal_priority(url),
            status="excluded" if excluded else "pending", status_reason=exclude_reason,
        )
        .on_conflict_do_nothing(constraint="ent_frontier_unique")
    )
    result = await db.execute(stmt)
    if result.rowcount == 0:        # lost a race with another worker that inserted the same URL
        return DUPLICATE
    return DEPTH_LIMIT if depth_limited else (EXCLUDED if excluded else ADDED)


_CLAIM_SQL = text("""
    update ent_frontier f
       set status = 'in_progress', claimed_by = :worker, claimed_at = now(), attempts = f.attempts + 1, updated_at = now()
     where f.id in (
            select id from ent_frontier
             where scan_id = :scan_id and status = 'pending'
             order by priority, depth, discovered_at
             limit :n
               for update skip locked)
 returning f.id, f.original_url, f.depth, f.attempts
""")


async def claim_next(db: AsyncSession, scan_id: uuid.UUID, worker: str, limit: int = 1) -> list[Claimed]:
    """Take up to `limit` pending URLs of one scan, shallowest first. Two workers never receive the same row."""
    rows = (await db.execute(_CLAIM_SQL, {"scan_id": scan_id, "worker": worker, "n": max(1, limit)})).all()
    return [Claimed(id=r.id, url=r.original_url, depth=r.depth, attempts=r.attempts) for r in rows]


_CLAIM_SEED_SQL = text("""
    update ent_frontier f
       set status = 'in_progress', claimed_by = :worker, claimed_at = now(), attempts = f.attempts + 1, updated_at = now()
     where f.id in (select id from ent_frontier where scan_id = :scan_id and status = 'pending' and depth = 0 order by discovered_at limit 1 for update skip locked)
 returning f.id, f.original_url, f.depth, f.attempts
""")


async def claim_seed(db: AsyncSession, scan_id: uuid.UUID, worker: str) -> Claimed | None:
    """The homepage (depth 0), if it is still waiting: it is inspected before any other page, whatever the priorities of the rest."""
    r = (await db.execute(_CLAIM_SEED_SQL, {"scan_id": scan_id, "worker": worker})).first()
    return Claimed(id=r.id, url=r.original_url, depth=r.depth, attempts=r.attempts) if r else None


#: The evidence a done web page must have before the next page is opened (a document has its `document_text` pages instead, which may legitimately be none).
REQUIRED_PAGE_EVIDENCE = ("cookie", "network", "header")


async def missing_evidence(db: AsyncSession, row_id: uuid.UUID) -> list[str]:
    """The required evidence kinds that are NOT stored for this frontier row (checked from the database itself, after the commit), or [] when everything is there."""
    have = {r.kind for r in (await db.execute(text("select distinct kind from ent_evidence where frontier_id = :f"), {"f": row_id})).all()}
    return [k for k in REQUIRED_PAGE_EVIDENCE if k not in have]


async def demote_unverified(db: AsyncSession, row_id: uuid.UUID, missing: list[str]) -> str:
    """A page marked done whose required evidence is not in the database: it is NOT done. Back to pending while attempts remain, else failed with the reason. Returns the new status."""
    attempts = await db.scalar(select(EntFrontier.attempts).where(EntFrontier.id == row_id)) or 0
    new = "pending" if attempts < get_enterprise_settings().max_attempts else "failed"
    await db.execute(update(EntFrontier).where(EntFrontier.id == row_id, EntFrontier.status == "done").values(
        status=new, last_error=f"evidence not stored: {', '.join(missing)}", claimed_by=None, claimed_at=None, updated_at=func.now()))
    return new


async def mark_unreached(db: AsyncSession, scan_id: uuid.UUID, reason: str) -> int:
    """Give every URL still waiting when the crawl stopped a recorded outcome: it stays pending (it can be resumed) and says why it was not inspected. Returns how many."""
    res = await db.execute(text("update ent_frontier set status_reason = :r, updated_at = now() where scan_id = :s and status = 'pending'"),
                           {"r": f"not inspected: {reason}"[:1000], "s": scan_id})
    return res.rowcount or 0


async def reopen_blocked(db: AsyncSession, scan_id: uuid.UUID) -> int:
    """For a resumed scan that had stopped because the site blocked the scanner: the pages that were refused go back to pending, with their attempts reset. Pages already done are kept."""
    res = await db.execute(text(
        "update ent_frontier set status = 'pending', attempts = 0, last_error = null, status_reason = null, claimed_by = null, claimed_at = null, updated_at = now() "
        "where scan_id = :s and status = 'failed' and last_error like 'BLOCKED%'"), {"s": scan_id})
    await db.execute(text("update ent_frontier set status_reason = null where scan_id = :s and status = 'pending' and status_reason like 'not inspected:%'"), {"s": scan_id})
    return res.rowcount or 0


async def reopen_unreached(db: AsyncSession, scan_id: uuid.UUID) -> int:
    """For a resumed scan that a time or memory limit had stopped: the URLs left pending lose their "not inspected" note (they will be inspected now). Pages already done are kept.
    Returns how many URLs are waiting."""
    res = await db.execute(text("update ent_frontier set status_reason = null, updated_at = now() where scan_id = :s and status = 'pending' and status_reason like 'not inspected:%'"),
                           {"s": scan_id})
    return res.rowcount or 0


async def mark_done(db: AsyncSession, row_id: uuid.UUID, *, http_status: int | None, final_url: str | None, content_type: str | None, note: str | None = None) -> None:
    """The page was read. `note` records anything left unread (for a document: the pages that had no text and need OCR)."""
    await db.execute(
        update(EntFrontier).where(EntFrontier.id == row_id, EntFrontier.status == "in_progress").values(
            status="done", http_status=http_status, final_url=final_url, content_type=content_type, status_reason=note,
            claimed_by=None, claimed_at=None, last_error=None, updated_at=func.now()))


async def record_page(db: AsyncSession, row_id: uuid.UUID, *, title: str | None, text_chars: int, content_hash: str | None, evidence_note: str | None) -> None:
    """What was extracted from the page: its title, how much text, the hash of that text, when, and anything that limited the read (a section not opened, a size limit, an iframe from another site)."""
    await db.execute(update(EntFrontier).where(EntFrontier.id == row_id).values(
        page_title=(title or "")[:500] or None, text_chars=text_chars, content_hash=content_hash, extracted_at=func.now(), evidence_note=(evidence_note or "")[:1500] or None))


async def add_not_recorded(db: AsyncSession, scan_id: uuid.UUID, count: int) -> None:
    """URLs that were found but could not be recorded because the scan reached its record cap. Kept as a number so the coverage declaration can say how many were dropped."""
    if count > 0:
        await db.execute(text("update ent_scans set urls_not_recorded = urls_not_recorded + :n where id = :s"), {"n": count, "s": scan_id})


async def mark_failed(db: AsyncSession, row_id: uuid.UUID, error: str, *, retryable: bool = True, http_status: int | None = None) -> str:
    """The page did not load. Retryable and attempts left -> back to pending; otherwise failed, with the reason. Returns the new status."""
    if not error:
        raise ValueError("a failure always says why")
    max_attempts = get_enterprise_settings().max_attempts
    attempts = await db.scalar(select(EntFrontier.attempts).where(EntFrontier.id == row_id))
    new = "pending" if (retryable and (attempts or 0) < max_attempts) else "failed"
    await db.execute(
        update(EntFrontier).where(EntFrontier.id == row_id, EntFrontier.status == "in_progress").values(
            status=new, last_error=error, http_status=http_status, claimed_by=None, claimed_at=None, updated_at=func.now()))
    return new


async def mark_excluded(db: AsyncSession, row_id: uuid.UUID, reason: str) -> None:
    """The URL was reached but is out of scope (robots.txt, another site, a page already scanned). It always says why."""
    if not reason:
        raise ValueError("an exclusion always says why")
    await db.execute(
        update(EntFrontier).where(EntFrontier.id == row_id, EntFrontier.status.in_(("pending", "in_progress"))).values(
            status="excluded", status_reason=reason, claimed_by=None, claimed_at=None, updated_at=func.now()))


_RECLAIM_SQL = text("""
    update ent_frontier
       set status = case when attempts >= :max_attempts then 'failed' else 'pending' end,
           last_error = 'worker lost: the claim expired before the page was finished',
           claimed_by = null, claimed_at = null, updated_at = now()
     where scan_id = :scan_id and status = 'in_progress'
       and claimed_at < now() - make_interval(secs => :lease)
""")


async def reclaim_stale(db: AsyncSession, scan_id: uuid.UUID, *, lease_seconds: int | None = None) -> int:
    """Take back URLs a worker claimed and never finished. Call it when a scan starts or resumes. Returns how many were taken back."""
    settings = get_enterprise_settings()
    result = await db.execute(_RECLAIM_SQL, {
        "scan_id": scan_id, "max_attempts": settings.max_attempts,
        "lease": settings.claim_lease_seconds if lease_seconds is None else lease_seconds,
    })
    return result.rowcount or 0


async def counts(db: AsyncSession, scan_id: uuid.UUID) -> dict[str, int]:
    """URLs per status. `pending + in_progress` is what remains; `done` is what was scanned. Missing statuses are 0."""
    out = {"pending": 0, "in_progress": 0, "done": 0, "failed": 0, "excluded": 0}
    rows = (await db.execute(select(EntFrontier.status, func.count()).where(EntFrontier.scan_id == scan_id).group_by(EntFrontier.status))).all()
    for status, n in rows:
        out[status] = n
    return out


async def is_finished(db: AsyncSession, scan_id: uuid.UUID) -> bool:
    """Nothing left to claim or wait for."""
    c = await counts(db, scan_id)
    return c["pending"] == 0 and c["in_progress"] == 0
