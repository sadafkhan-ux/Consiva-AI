"""Runs an Enterprise crawl: claim URLs from the frontier, render them, keep the evidence, record the links, repeat until nothing is left.

Everything that must survive a crash lives in the database (the frontier and the evidence), so `run` can be called again for the same scan and carries on where it stopped:
URLs a dead worker had claimed are taken back by `reclaim_stale`, and URLs already done are never visited again. Several workers may run the same scan; claiming is row-locked.

The browser setup (host pinning against DNS rebinding, the navigation guard, locale and user agent) is the 5-page scanner's own, imported and unchanged, so the two products
cannot differ on what is safe to visit.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import socket
import uuid
from urllib.parse import urlparse

from playwright.async_api import async_playwright
from sqlalchemy import text

from app.config import get_settings
from app.db.session import async_session_factory, set_org_scope
from app.enterprise import scans
from app.enterprise.config import get_enterprise_settings
from app.enterprise.consent.run import make_session_tools, run_consent_suite
from app.enterprise.crawl import discovery, frontier, limits, page_inspect, scope, slug_triage
from app.enterprise.crawl.renderer import RenderFailed, render
from app.enterprise.documents import extract as doc_extract
from app.enterprise.documents import fetch as doc_fetch
from app.enterprise.documents import readers
from app.enterprise.evidence import store as evidence
from app.enterprise.security.tls import probe_tls
from app.scanner.crawl_controls import DomainRateLimiter
from app.scanner.crawler import _guard_navigation, _host_resolver_pin_args, _new_context_kwargs
from app.scanner.failures import RobotsUnreadable
from app.scanner.url_safety import assert_safe_url
from app.scanner.withdrawal import non_essential_requests
from app.monitoring import blocks, events as error_events

logger = logging.getLogger(__name__)

WORKER_ID = f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
#: How long a runner waits for pages another worker still holds before it checks again.
IDLE_POLL_SECONDS = 2.0
#: Pages the website's protection refused, per running scan: {scan_id: {"first": BlockVerdict, "url": str, "seed": bool, "count": int}}. Read by the crawl loop to stop at once instead of retrying.
_BLOCKS: dict = {}
#: Blocked pages (at least this many, and at least half of the pages tried) end the scan as blocked: the rest of the site would be refused too, and what was read is not enough to assess.
BLOCKED_STOP_MIN = 3


def _note_block(scan_id, claimed, verdict) -> None:
    b = _BLOCKS.setdefault(scan_id, {"first": verdict, "url": claimed.url, "seed": False, "count": 0})
    b["count"] += 1
    if claimed.depth == 0:
        b.update(first=verdict, url=claimed.url, seed=True)


async def _is_cancelled(scan_id: uuid.UUID, org_id: uuid.UUID) -> bool:
    async with async_session_factory() as db:
        scan = await scans.get_scan(db, scan_id, org_id)
        return scan is None or scan.status == "cancelled"


async def _process(claimed: frontier.Claimed, *, scan_id, root_url, rules, context, browser, settings, limiter, max_urls, max_depth, timeout_ms) -> str:
    """One page: robots check, render, keep evidence, record links, mark the row. Returns the row's final status. Never raises: a page's failure is that page's record."""
    async with async_session_factory() as db:
        try:
            if not rules.allows(claimed.url):
                await frontier.mark_excluded(db, claimed.id, "robots_txt: the site does not allow the scanner to visit this page")
                await db.commit()
                return "excluded"
            await limiter.acquire()
            if scope.classify(claimed.url, root_url) == scope.DOCUMENT:
                return await _process_document(db, claimed, scan_id=scan_id, root_url=root_url, context=context)
            # Each page gets a browser context of its own: its cookies and requests are then this page's alone, and it is always the first visit (no consent given).
            page_context = await browser.new_context(**_new_context_kwargs(settings))
            await page_context.route("**/*", _guard_navigation)
            try:
                page = await render(page_context, claimed.url, timeout_ms=timeout_ms, site_url=root_url)
            except RenderFailed as exc:
                if exc.block:
                    _note_block(scan_id, claimed, exc.block)
                    new = await frontier.mark_failed(db, claimed.id, f"BLOCKED ({exc.block.kind}): {exc.block.signal}", retryable=False, http_status=exc.http_status)
                    await db.commit()
                    return new
                new = await frontier.mark_failed(db, claimed.id, str(exc), retryable=exc.retryable, http_status=exc.http_status)
                await db.commit()
                return new
            finally:
                await page_context.close()
            if not scope.same_site(page.final_url, root_url):
                await frontier.mark_excluded(db, claimed.id, "redirected_to_another_site")
                await db.commit()
                return "excluded"
            # A challenge or CAPTCHA page that was served with a normal status is not the site's content: it must not be stored, classified or analysed as if it were.
            verdict = blocks.detect_block(page.http_status, page.headers, page.title, page.text)
            if verdict:
                _note_block(scan_id, claimed, verdict)
                new = await frontier.mark_failed(db, claimed.id, f"BLOCKED ({verdict.kind}): {verdict.signal}", retryable=False, http_status=page.http_status)
                await db.commit()
                return new
            if page_inspect.is_soft_404(page.title, page.text, page.http_status):
                await frontier.mark_excluded(db, claimed.id, "soft_404: the page answered 200 but reads as not found")
                await db.commit()
                return "excluded"
            text_hash = hashlib.sha256(page.text.encode("utf-8")).hexdigest() if page.text else None
            added = await discovery.add_links(db, scan_id, root_url, page.links, parent_depth=claimed.depth, max_urls=max_urls, max_depth=max_depth)
            await frontier.add_not_recorded(db, scan_id, added["limit"])
            # No page is dropped as a "duplicate": two addresses that serve the same text are both kept, each read under its own address, and the analysis reuses what it already read (identical evidence is
            # stored once, and an identical passage is not sent to the model twice). A page that merely looks like another one is never discarded.
            notes = _inspection_notes(page, root_url)
            for kind, content in (("page_text", page.text), ("page_html", page.html)):
                if content:
                    notes += await _keep_page_evidence(db, scan_id, claimed, page, kind, content)
            await _keep_observations(db, scan_id, claimed, page, root_url)
            await frontier.record_page(db, claimed.id, title=page.title, text_chars=len(page.text or ""), content_hash=text_hash, evidence_note="; ".join(notes))
            await frontier.mark_done(db, claimed.id, http_status=page.http_status, final_url=page.final_url, content_type=page.content_type)
            await db.commit()
            if claimed.depth == 0:                         # the entry page's links are now recorded: ask the model which addresses look like policies
                await slug_triage.triage_safe(scan_id)
            return "done"
        except Exception as exc:                       # a bug or a database error must not take the whole crawl down with it
            await db.rollback()
            logger.exception("enterprise scan %s: page %s failed unexpectedly", scan_id, claimed.url)
            async with async_session_factory() as db2:
                await frontier.mark_failed(db2, claimed.id, f"{type(exc).__name__}: {exc}"[:300], retryable=False)
                await db2.commit()
            return "failed"


def _inspection_notes(page, root_url: str) -> list[str]:
    """Anything that limited what was read from this page, in words a reader can act on. Each note is a fact the renderer recorded; none is a guess."""
    notes: list[str] = []
    scroll, opened = page.scroll or {}, page.expanded or {}
    if scroll.get("stopped") in ("max_steps", "deadline"):
        notes.append(f"scrolling stopped at the {scroll['stopped'].replace('_', ' ')} limit, so content further down may not have loaded")
    if opened.get("candidates", 0) > opened.get("opened", 0):
        notes.append(f"{opened['candidates'] - opened['opened']} of {opened['candidates']} collapsed sections were not opened")
    if any(len(b) >= page_inspect.SHADOW_TEXT_LIMIT for b in page.dom.get("shadow", [])) or any(len(f.get("text", "")) >= page_inspect.SHADOW_TEXT_LIMIT for f in page.dom.get("frame_text", [])):
        notes.append(f"embedded (shadow DOM or iframe) text was cut at {page_inspect.SHADOW_TEXT_LIMIT:,} characters")
    read = {f.get("src") for f in page.dom.get("frame_text", [])}
    foreign = [i.get("src") for i in page.dom.get("iframes", []) if i.get("src") and i.get("src") not in read and not scope.same_site(i["src"], root_url)]
    if foreign:
        notes.append(f"{len(foreign)} iframe(s) from other sites were not read (for example {foreign[0][:80]})")
    return notes


async def _keep_page_evidence(db, scan_id, claimed, page, kind: str, content: str) -> list[str]:
    """Keep the page's text or HTML. A page over the evidence size limit used to be dropped with a log line. Its text is now kept in parts (each part its own numbered piece of the same page, which
    the analysis reads one after another), and what could not be kept is returned as a note, so the page is never silently empty."""
    try:
        await evidence.store(db, scan_id, kind=kind, source_url=page.final_url, content=content, detection_method="dom_render", frontier_id=claimed.id, content_type=page.content_type)
        return []
    except evidence.EvidenceTooLarge as exc:
        logger.warning("enterprise scan %s: %s of %s is over the evidence limit (%s)", scan_id, kind, page.final_url, exc)
    if kind == "page_html":
        return [f"the page HTML ({len(content.encode('utf-8')):,} bytes) was over the evidence limit and was not kept; the title and headings come from the text instead"]
    parts = evidence.split_for_storage(content)
    kept = 0
    for number, part in enumerate(parts, start=1):
        try:
            await evidence.store(db, scan_id, kind=kind, source_url=page.final_url, content=part, detection_method="dom_render", page_number=number, frontier_id=claimed.id, content_type=page.content_type)
            kept += 1
        except evidence.EvidenceTooLarge:
            logger.warning("enterprise scan %s: part %d of %s is still over the evidence limit", scan_id, number, page.final_url)
    return [f"the page text was over the evidence limit and was kept in {kept} of {len(parts)} parts" + ("" if kept == len(parts) else "; the rest was not kept")]


async def _keep_tls(scan_id, root_url: str) -> None:
    """Probe the entry site's TLS once per scan and keep what the server said. A failed probe is only a missing observation, never a failed scan."""
    host = urlparse(root_url).hostname
    if not host or urlparse(root_url).scheme != "https":
        return
    try:
        result = await probe_tls(host)
        async with async_session_factory() as db:
            await evidence.store(db, scan_id, kind="header", source_url=root_url, content=json.dumps(result, sort_keys=True), detection_method="tls_probe", content_type="application/json")
            await db.commit()
    except Exception:
        logger.warning("enterprise scan %s: TLS probe of %s could not be kept", scan_id, host, exc_info=True)


async def _keep_consent(scan_id, root_url: str, browser, settings) -> None:
    """Run the consent tests once the crawl has finished and keep each result as evidence of kind 'consent'. A failure here loses these observations, never the crawl."""
    try:
        async with async_session_factory() as db:
            rows = (await db.execute(text(
                "select original_url from ent_frontier where scan_id = :s and status = 'done' and content_type like 'text/html%' order by depth, discovered_at limit 6"), {"s": scan_id})).all()
        root_key = root_url.rstrip("/")
        further = [r.original_url for r in rows if r.original_url.rstrip("/") != root_key][:3]
        make_context, goto = make_session_tools(browser, settings, _guard_navigation, _new_context_kwargs)
        results = await run_consent_suite(make_context, goto, root_url, further)
        async with async_session_factory() as db:
            for name, result in sorted(results.items()):
                await evidence.store(db, scan_id, kind="consent", source_url=root_url, content=json.dumps({"test": name, **result}, sort_keys=True, ensure_ascii=False),
                                     detection_method=f"consent_test:{name}", content_type="application/json")
            await db.commit()
    except Exception:
        logger.warning("enterprise scan %s: the consent tests could not be kept", scan_id, exc_info=True)


async def _keep_observations(db, scan_id, claimed: frontier.Claimed, page, root_url: str) -> None:
    """What the browser saw on this page, kept as evidence rows: cookies, requests, forms and the response's headers with the inspection record. Each row names its page and the
    consent state, which is always `pre_consent` here (a fresh context that has given no consent). One bad row never loses the others."""
    state = "pre_consent"
    rows = [
        ("cookie", {"page_url": page.final_url, "consent_state": state, "cookies": page_inspect.normalize_cookies(page.cookies, root_url, state)}, "browser_cookies"),
        # `non_essential` lists the analytics and advertising services this page contacted before any choice, classified by the same catalogue the 5-page scanner uses.
        ("network", {"page_url": page.final_url, **page_inspect.summarise_requests(page.requests, root_url, state),
                     "non_essential": non_essential_requests([(r["url"], r["type"]) for r in page.requests], urlparse(root_url).netloc)}, "browser_requests"),
        ("header", {
            "page_url": page.final_url, "requested_url": claimed.url, "http_status": page.http_status, "redirect_chain": page.redirect_chain,
            "canonical": page.dom.get("canonical"), "alternates": page.dom.get("alternates", []), "robots_meta": page.dom.get("robots"), "lang": page.dom.get("lang"),
            "security": page_inspect.security_headers(page.headers, page.final_url),
            "inspection": {"scroll": page.scroll, "sections_opened": page.expanded, "shadow_blocks": len(page.dom.get("shadow", [])), "iframes": page.dom.get("iframes", [])},
        }, "response_headers"),
    ]
    forms = page_inspect.normalize_forms(page.dom.get("forms", []), page.final_url)
    if forms:
        rows.append(("form", {"page_url": page.final_url, "forms": forms}, "dom_forms"))
    for kind, body, method in rows:
        try:
            await evidence.store(db, scan_id, kind=kind, source_url=page.final_url, content=json.dumps(body, sort_keys=True, ensure_ascii=False), detection_method=method,
                                 frontier_id=claimed.id, content_type="application/json")
        except evidence.EvidenceTooLarge as exc:
            logger.warning("enterprise scan %s: %s of %s not kept (%s)", scan_id, kind, page.final_url, exc)


async def _process_document(db, claimed: frontier.Claimed, *, scan_id, root_url, context) -> str:
    """A linked PDF or DOCX: fetch it, read it page by page (OCR for scanned pages where available), keep each page as evidence with its page number and method, and record the
    document's extraction_status and extraction_method. Unreadable, encrypted and unsupported documents are done-with-a-status, not silently empty."""
    max_bytes = get_enterprise_settings().max_document_mb * 1024 * 1024
    try:
        data, content_type, final_url = await doc_fetch.fetch(context, claimed.url, root_url, max_bytes=max_bytes)
    except doc_fetch.DocumentTooLarge as exc:
        await frontier.mark_excluded(db, claimed.id, f"document_too_large: {exc} is over the {get_enterprise_settings().max_document_mb} MB limit")
        await doc_extract.set_extraction(db, claimed.id, "too_large", None)
        await db.commit()
        return "excluded"
    except doc_fetch.DocumentFetchFailed as exc:
        new = await frontier.mark_failed(db, claimed.id, str(exc), retryable=exc.retryable, http_status=exc.http_status)
        await db.commit()
        return new
    # PDF parsing and OCR block, so they run in a worker thread. The outcome is an Extraction: its status is recorded as extraction_status (never as analysed).
    result = await asyncio.to_thread(doc_extract.extract, data, final_url, content_type)
    for page in result.pages:
        try:
            await evidence.store(db, scan_id, kind="document_text", source_url=final_url, content=page.text, detection_method=page.method,
                                 page_number=page.number, frontier_id=claimed.id, content_type=content_type)
        except evidence.EvidenceTooLarge as exc:
            logger.warning("enterprise scan %s: page %s of %s not kept (%s)", scan_id, page.number, final_url, exc)
    note = f"{result.page_count} page(s) kept ({result.status})" + (f"; {result.reason}" if result.reason else "")
    # The fetch succeeded, so the row is done; how much of the document could be read is its extraction_status, and the reason is in status_reason.
    await frontier.mark_done(db, claimed.id, http_status=200, final_url=final_url, content_type=content_type, note=note[:1000])
    await doc_extract.set_extraction(db, claimed.id, result.status, result.method)
    await db.commit()
    return "done"


async def run(scan_id: uuid.UUID, org_id: uuid.UUID, *, worker_id: str = WORKER_ID, timeout_ms: int = 30_000) -> str:
    """Crawl one scan to the end (or until it is cancelled). Returns the scan's final status. Safe to call again after a crash."""
    set_org_scope(org_id)
    async with async_session_factory() as db:
        scan = await scans.get_scan(db, scan_id, org_id)
        if scan is None:
            raise LookupError(f"Enterprise scan {scan_id} not found")
        if scan.status in ("completed", "failed", "cancelled"):
            return scan.status
        if scan.status == "queued":
            await scans.transition(db, scan_id, org_id, "running")
        root_url, options = scan.website_url, dict(scan.options or {})
        await db.commit()

    max_urls, max_depth = int(options.get("max_urls", 0)), int(options.get("max_depth", 50))
    recorded = frontier.record_cap(max_urls)          # URLs known may far exceed URLs read; the limit is on pages read
    concurrency, rps = int(options.get("concurrency", 4)), float(options.get("per_domain_rps", 2.0))
    settings = get_settings()
    limiter = DomainRateLimiter(rps)
    guard = limits.LimitGuard(memory_budget_mb=int(options.get("memory_budget_mb", 0)), max_runtime_seconds=int(options.get("max_runtime_seconds", 0)))
    limit_hit: str | None = None
    block_stop = None                                  # set when the website's protection refused the scanner and the crawl was stopped for it

    try:
        safe_ips = await assert_safe_url(root_url)
        launch_args = _host_resolver_pin_args(urlparse(root_url).hostname, safe_ips)
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=settings.scanner_headless, args=launch_args)
            try:
                context = await browser.new_context(**_new_context_kwargs(settings))
                await context.route("**/*", _guard_navigation)
                rules = await discovery.read_robots(context, root_url)

                await _keep_tls(scan_id, root_url)
                async with async_session_factory() as db:
                    taken_back = await frontier.reclaim_stale(db, scan_id)
                    if taken_back:
                        logger.info("enterprise scan %s: took back %d unfinished pages (resume)", scan_id, taken_back)
                    if sum((await frontier.counts(db, scan_id)).values()) == 0:
                        seeded = await discovery.seed(db, scan_id, context, root_url, rules, max_urls=recorded, max_depth=max_depth)
                        await frontier.add_not_recorded(db, scan_id, seeded.get("not_recorded", 0))
                    await db.commit()
                await slug_triage.triage_safe(scan_id)                   # the sitemap's addresses, if there were any

                while True:
                    if await _is_cancelled(scan_id, org_id):
                        return "cancelled"
                    verdict = guard.check()
                    if verdict == limits.PAUSE:                    # over the memory budget: let the pages in flight finish and memory settle before starting more
                        await asyncio.sleep(limits.POLL_SECONDS)
                        continue
                    if verdict is not None:                        # memory_limit or time_limit: stop here, say why, leave the rest pending (the scan can be resumed)
                        limit_hit = verdict
                        break
                    if max_urls:
                        async with async_session_factory() as db:
                            c = await frontier.counts(db, scan_id)
                        if c["done"] + c["failed"] + c["in_progress"] >= max_urls:     # the page limit: everything else found stays pending, and says so
                            limit_hit = "url_limit"
                            break
                    async with async_session_factory() as db:
                        await frontier.reclaim_stale(db, scan_id)
                        batch = await frontier.claim_next(db, scan_id, worker_id, limit=concurrency)
                        await db.commit()
                    if not batch:
                        async with async_session_factory() as db:
                            if await frontier.is_finished(db, scan_id):
                                break
                        await asyncio.sleep(IDLE_POLL_SECONDS)         # another worker holds the rest
                        continue
                    await asyncio.gather(*(
                        _process(c, scan_id=scan_id, root_url=root_url, rules=rules, context=context, browser=browser, settings=settings, limiter=limiter,
                                 max_urls=recorded, max_depth=max_depth, timeout_ms=timeout_ms) for c in batch))
                    b = _BLOCKS.get(scan_id)
                    if b and (b["seed"] or b["count"] >= BLOCKED_STOP_MIN):
                        async with async_session_factory() as db:
                            c = await frontier.counts(db, scan_id)
                        if b["seed"] or b["count"] * 2 >= (c["done"] + c["failed"]):
                            block_stop = b                          # stop now: more attempts only add load on a site that is refusing us
                            break
                # The crawl is finished (a cancelled scan returned above): now the consent tests, in fresh sessions of their own. A crawl that hit a limit does not start more work.
                if block_stop is None and limit_hit not in (limits.MEMORY_LIMIT, limits.TIME_LIMIT):          # a page limit is a normal end; a memory or time limit means stop starting work
                    await _keep_consent(scan_id, root_url, browser, settings)
            finally:
                await browser.close()
    except RobotsUnreadable as exc:
        return await _finish(scan_id, org_id, "failed", error=f"robots.txt could not be read, so the site's restrictions are unknown: {exc}")
    except Exception as exc:
        logger.exception("enterprise scan %s failed", scan_id)
        return await _finish(scan_id, org_id, "failed", error=f"{type(exc).__name__}: {exc}"[:500])

    async with async_session_factory() as db:
        c = await frontier.counts(db, scan_id)
    block = _BLOCKS.pop(scan_id, None)
    if block_stop is not None or (block and c["done"] == 0):
        b = block_stop or block
        v = b["first"]
        await error_events.record_block(agent="enterprise", website_url=b["url"] or root_url, verdict=v, module="enterprise.crawl", scan_id=scan_id, wait=True)
        return await _finish(scan_id, org_id, "failed", error=f"{v.kind}: {blocks.USER_MESSAGE} {v.signal[:1].upper() + v.signal[1:]}. {blocks.EXPLANATION}"[:500], stop_reason=v.kind.lower())
    if block:                                              # some pages were refused but the scan went on: say so, once
        await error_events.record_block(agent="enterprise", website_url=block["url"] or root_url, verdict=block["first"], module="enterprise.crawl", scan_id=scan_id, wait=True)
    if c["done"] == 0:
        return await _finish(scan_id, org_id, "failed", error="no page could be scanned (every URL failed or was excluded)")
    stop = limit_hit
    return await _finish(scan_id, org_id, "completed", stop_reason=stop)


async def _finish(scan_id: uuid.UUID, org_id: uuid.UUID, to: str, *, error: str | None = None, stop_reason: str | None = None) -> str:
    async with async_session_factory() as db:
        scan = await scans.get_scan(db, scan_id, org_id)
        if scan is not None and scan.status == "running":      # a scan cancelled meanwhile stays cancelled
            await scans.transition(db, scan_id, org_id, to, error=error, stop_reason=stop_reason)
        await db.commit()
        return scan.status if scan is not None else to

