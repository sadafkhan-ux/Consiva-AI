"""Runs an Enterprise crawl, strictly one URL at a time, in the owner's sequence (19 Nov 2026):

  1. the homepage, inspected with the 5-page agent's own first-page sequence (crawl/inspect.py), then the consent tests on it (Accept, Reject, Reject carried to further pages, withdrawal,
     purpose by purpose, the buttons compared), one browser session at a time;
  2. URL discovery: the sitemap and every inspected page's links are normalised, de-duplicated and recorded in the frontier (the database);
  3. the privacy policies first (crawl priority 0), then the other legal pages (1), then every remaining URL (2), each with the same deep inspection as the homepage.

One URL is opened, inspected, its evidence stored, the commit made and the stored evidence checked in the database before the next URL is opened; the `concurrency` option is kept for the
API but never makes page inspection parallel. Everything that must survive a crash lives in the database (the frontier and the evidence), so `run` can be called again for the same scan and
carries on where it stopped: URLs a dead worker had claimed are taken back by `reclaim_stale`, URLs already done are never visited again, and a scan stopped by blocking can be resumed.

Blocking: until 10 pages have been inspected successfully, the first page the site's protection refuses (HTTP 401/403/429/451, a CAPTCHA or a challenge) stops the crawl at once; after that a
refusal stops it when it is website-wide (rate limiting or a challenge, or three refused pages in a row), and a single refused page is recorded as that page's outcome. The blocked URL and
the reason are recorded, and everything collected is kept.

The browser setup (host pinning against DNS rebinding, the navigation guard, locale and user agent) is the 5-page scanner's own, imported and unchanged, so the two products
cannot differ on what is safe to visit.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import socket
import time
import uuid
from urllib.parse import urlparse

from playwright.async_api import async_playwright
from sqlalchemy import text

from app.config import get_settings
from app.db.session import async_session_factory, set_org_scope
from app.enterprise import scans, store
from app.enterprise.config import get_enterprise_settings
from app.enterprise.consent.run import make_session_tools, run_consent_suite
from app.enterprise.crawl import discovery, frontier, limits, page_inspect, redirects, scope, slug_triage
from app.enterprise.crawl.guard import host_guard
from app.enterprise.crawl.inspect import NotFoundPage, _verdict_for_failure, deep_inspect
from app.enterprise.crawl.renderer import RenderFailed, _links
from app.enterprise.documents import extract as doc_extract
from app.enterprise.documents import fetch as doc_fetch
from app.enterprise.documents import readers
from app.enterprise.evidence import store as evidence
from app.enterprise.security.tls import probe_tls
from app.scanner.crawl_controls import DomainRateLimiter
from app.scanner.crawler import (
    _TRACKER_SETTLE_MS, _host_resolver_pin_args, _navigate_with_retry, _new_context_kwargs, _page_is_a_block_page, _page_says_it_was_not_found, _progressive_scroll,
    _wait_for_load_event,
)
from app.scanner.failures import RobotsUnreadable
from app.scanner.url_safety import assert_safe_url
from app.scanner.withdrawal import non_essential_requests
from app.monitoring import blocks, events as error_events

logger = logging.getLogger(__name__)

WORKER_ID = f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
#: How long a runner waits for pages another worker still holds before it checks again.
IDLE_POLL_SECONDS = 2.0
#: Pages the website's protection refused, per running scan: {scan_id: {"first": BlockVerdict, "url": str, "seed": bool, "count": int, "last": BlockVerdict, "last_url": str}}.
_BLOCKS: dict = {}
#: Until this many pages have been inspected successfully, the first refused page stops the crawl at once (owner's rule, 19 Nov 2026).
BLOCK_GRACE_PAGES = 10
#: After that, this many refused pages in a row mean the whole website is refusing the scanner.
BLOCKED_IN_A_ROW = 3


def _note_block(scan_id, claimed, verdict) -> None:
    b = _BLOCKS.setdefault(scan_id, {"first": verdict, "url": claimed.url, "seed": False, "count": 0})
    b["count"] += 1
    b["last"], b["last_url"] = verdict, claimed.url
    if claimed.depth == 0:
        b.update(first=verdict, url=claimed.url, seed=True)


def is_website_wide(verdict) -> bool:
    """A refusal that is about the scanner, not about one page: rate limiting (HTTP 429), or a bot challenge or CAPTCHA."""
    return verdict.kind == blocks.BOT_BLOCKED or "429" in (verdict.signal or "")


def should_stop_for_block(*, verdict, inspected: int, in_a_row: int, seed: bool) -> bool:
    """The blocking rule. The homepage refused, or any refusal before BLOCK_GRACE_PAGES pages were inspected, stops at once; after that a website-wide refusal, or BLOCKED_IN_A_ROW refusals
    in a row, stop; a single refused page is only that page's outcome."""
    if seed or inspected < BLOCK_GRACE_PAGES:
        return True
    return is_website_wide(verdict) or in_a_row >= BLOCKED_IN_A_ROW


def script_identity(url: str) -> str:
    """A script's identity: its URL without the query string or fragment (the 5-page rule, crawler._merge_tracker_pass), host lowercased: `gtm.js?id=1&cb=9` and `gtm.js?id=1&cb=10` are one script."""
    from urllib.parse import urlsplit as _s
    try:
        p = _s(url)
    except ValueError:
        return url.split("?", 1)[0].split("#", 1)[0]
    return f"{p.scheme.lower()}://{(p.netloc or '').lower()}{p.path}"


def cookie_lifetime(c: dict, now: float | None = None) -> str:
    """How long a cookie lives, in coarse classes (session, under a day, under 30 days, under a year, a year or more): enough to see a cookie's lifetime change between pages, never
    flipping because two pages were loaded a few seconds apart."""
    import time as _t
    expires = c.get("expires")
    if c.get("session") or expires in (None, -1):
        return "session"
    left = float(expires) - (now if now is not None else _t.time())
    day = 86_400
    return "under_1_day" if left < day else "under_30_days" if left < 30 * day else "under_1_year" if left < 365 * day else "1_year_or_more"


def cookie_signature(c: dict) -> str:
    """What makes a cookie's observation different from an earlier one of the same identity: its flags and its lifetime class (never its value, which can be a per-visit identifier)."""
    return "|".join(str(c.get(k)) for k in ("secure", "http_only", "same_site", "first_party", "lifetime"))


class Fingerprints:
    """Which cookies, tracking services, script sources and scripts this scan has already seen, so each page's evidence says what is NEW on it and what CHANGED (smart evidence reuse: a
    cookie seen on every page is one cookie, and a page that sets a new one, or the same one with other flags or another lifetime, says so). A fingerprint is the identity of the thing, never its
    value: a cookie's name, domain (no leading dot, lowercase) and path; a service's vendor or host; a script's host; a script's URL without its query string."""

    KINDS = ("cookie", "tracker", "script", "script_url")

    def __init__(self) -> None:
        self.seen: dict[str, set[str]] = {k: set() for k in self.KINDS}
        self.signatures: dict[str, str] = {}

    @staticmethod
    def of_page(cookies: list[dict], requests: list[dict], non_essential: list[dict]) -> dict[str, set[str]]:
        from urllib.parse import urlparse as _u
        scripts = [r["url"] for r in requests if r.get("type") == "script" and r.get("url")]
        return {
            "cookie": {c.get("identity") or f"{c.get('name')}|{(c.get('domain') or '').lstrip('.').lower()}|{c.get('path') or '/'}" for c in cookies},
            "tracker": {(t.get("vendor") or t.get("host") or "").lower() for t in non_essential if t.get("vendor") or t.get("host")},
            "script": {_u(u).netloc.lower() for u in scripts if _u(u).netloc},
            "script_url": {script_identity(u) for u in scripts},
        }

    def new_on(self, page: dict[str, set[str]]) -> dict[str, list[str]]:
        """What on this page was not seen before in the scan, and remember it."""
        out = {}
        for kind, items in page.items():
            fresh = sorted(items - self.seen.setdefault(kind, set()))
            self.seen[kind] |= items
            out[kind] = fresh[:100]
        return out

    def changed_on(self, cookies: list[dict]) -> list[str]:
        """The cookies on this page whose identity was seen before in the scan but with another signature (flags or lifetime class), and remember the latest signature."""
        changed = []
        for c in cookies:
            ident = c.get("identity")
            if not ident:
                continue
            sig = cookie_signature(c)
            before = self.signatures.get(ident)
            if before is not None and before != sig:
                changed.append(ident)
            self.signatures[ident] = sig
        return sorted(changed)[:100]

    async def load(self, scan_id) -> None:
        """Rebuild what was already seen from the stored evidence (a resumed scan), so a resumed page is not told everything is new."""
        async with async_session_factory() as db:
            rows = (await db.execute(text("select kind, content from ent_evidence where scan_id = :s and kind in ('cookie', 'network') order by captured_at"), {"s": scan_id})).all()
        for r in rows:
            try:
                doc = json.loads(r.content)
            except (TypeError, ValueError):
                continue
            if r.kind == "cookie":
                cookies = doc.get("cookies") or []
                self.seen["cookie"] |= self.of_page(cookies, [], [])["cookie"]
                for c in cookies:
                    if c.get("identity") and "lifetime" in c:
                        self.signatures[c["identity"]] = cookie_signature(c)
            else:
                self.seen["tracker"] |= self.of_page([], [], doc.get("non_essential") or [])["tracker"]
                self.seen["script"] |= set(doc.get("script_hosts") or [])
                self.seen["script_url"] |= set(doc.get("script_urls") or [])


def off_host_reason(url: str, final: str, stopped: list[dict], root_url: str) -> str:
    """Why a page that left the submitted host is excluded: where it went. A browser-guard stop (a script sent the page to another host) leaves the browser on its own error page, so the
    destination is the navigation the guard stopped, not the address the browser shows."""
    dest = stopped[-1]["url"] if stopped and not final.lower().startswith(("http://", "https://")) else (stopped[-1]["url"] if stopped and scope.same_site(final, root_url) else final)
    how = "navigates the browser to" if stopped else "redirected to"
    return (scope.exclusion_reason(dest, root_url, context=f"{url} {how} {dest}" + ("; the browser guard stopped it" if stopped else ""))
            or f"redirected_to_another_site: {url} {how} {dest}"[:1000])


async def _is_cancelled(scan_id: uuid.UUID, org_id: uuid.UUID) -> bool:
    async with async_session_factory() as db:
        scan = await scans.get_scan(db, scan_id, org_id)
        return scan is None or scan.status == "cancelled"


async def _process(claimed: frontier.Claimed, *, scan_id, root_url, rules, context, browser, settings, limiter, max_urls, max_depth, timeout_ms, prints: Fingerprints | None = None) -> str:
    """One page: robots check, the redirect chain read before navigation (a hop to another host is never followed), deep inspection in a fresh browser session whose top-level navigation
    is held to the submitted host (the 5-page agent's first-page sequence, then the Enterprise extras), keep evidence, record links, mark the row, then check in the database that the
    evidence is really stored. Returns the row's final status. Never raises: a page's failure is that page's record."""
    async with async_session_factory() as db:
        try:
            if not rules.allows(claimed.url):
                await frontier.mark_excluded(db, claimed.id, "robots_txt: the site does not allow the scanner to visit this page")
                await db.commit()
                return "excluded"
            off = scope.exclusion_reason(claimed.url, root_url, context="recorded before the exact-host rule")
            if off:                                    # a row recorded before the exact-host rule (a resumed older scan): never opened
                await frontier.mark_excluded(db, claimed.id, off)
                await db.commit()
                return "excluded"
            await limiter.acquire()
            if scope.classify(claimed.url, root_url) == scope.DOCUMENT:
                return await _process_document(db, claimed, scan_id=scan_id, root_url=root_url, context=context)
            # The redirect chain, read before the browser opens the page (crawl/redirects.py): a hop to a subdomain or another website is not followed.
            chain = await redirects.check(context.request, claimed.url, root_url)
            if chain.off_host:
                await frontier.mark_excluded(db, claimed.id, scope.exclusion_reason(chain.off_host, root_url, context=f"{claimed.url} redirects to {chain.off_host}"))
                await db.commit()
                return "excluded"
            # Each page gets a browser context of its own: its cookies and requests are then this page's alone, and it is always the first visit (no consent given).
            page_context = await browser.new_context(**_new_context_kwargs(settings))
            stopped: list[dict] = []
            await page_context.route("**/*", host_guard(root_url, stopped))
            try:
                page = await deep_inspect(page_context, claimed.url, root_url=root_url, settings=settings, is_home=claimed.depth == 0)
            except NotFoundPage:
                await frontier.mark_excluded(db, claimed.id, "soft_404: the page answered 200 but says it was not found")
                await db.commit()
                return "excluded"
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
                # The redirect was not visible before navigation (or changed), or the page sent the browser to another host by script and the browser guard stopped it (the browser
                # then shows its own error page). Nothing from it is stored; the reason names the original address and where it tried to go.
                await frontier.mark_excluded(db, claimed.id, off_host_reason(claimed.url, page.final_url, stopped, root_url))
                await db.commit()
                return "excluded"
            # A same-host redirect to a page already inspected under its own address: the same page, recorded as a duplicate destination instead of being stored twice.
            dest = await frontier.find_row(db, scan_id, page.final_url) if frontier.key_of(page.final_url) != frontier.key_of(claimed.url) else None
            if dest is not None and dest.id != claimed.id and dest.status == "done":
                await frontier.mark_excluded(db, claimed.id, f"duplicate_destination: redirects to {page.final_url}, which was inspected under its own address"[:1000])
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
            await _keep_observations(db, scan_id, claimed, page, root_url, prints=prints)
            await frontier.record_page(db, claimed.id, title=page.title, text_chars=len(page.text or ""), content_hash=text_hash, evidence_note="; ".join(notes))
            await frontier.mark_done(db, claimed.id, http_status=page.http_status, final_url=page.final_url, content_type=page.content_type)
            if dest is not None and dest.id != claimed.id and dest.status == "pending":
                # The destination is waiting under its own address: it was just inspected as this page's destination, so it is not opened a second time.
                await frontier.mark_excluded(db, dest.id, f"duplicate_destination: inspected as the destination of {claimed.url}"[:1000])
            await db.commit()
            # The page is complete only when its evidence is in the database: checked from the database itself before the next URL is opened.
            missing = await frontier.missing_evidence(db, claimed.id)
            if missing:
                new = await frontier.demote_unverified(db, claimed.id, missing)
                await db.commit()
                logger.warning("enterprise scan %s: %s was inspected but its %s evidence is not stored; recorded as %s", scan_id, claimed.url, ", ".join(missing), new)
                return new
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
    """The consent tests on the homepage, right after the homepage was inspected and before any other page (the owner's sequence): Accept, Reject, Reject carried to further pages,
    withdrawal, purpose by purpose and the buttons compared, ONE browser session at a time. The further pages are the first same-site pages the homepage links to (legal pages first).
    Each result is kept as evidence of kind 'consent'. Not repeated on a resumed scan that already has them. A failure here loses these observations, never the crawl."""
    try:
        async with async_session_factory() as db:
            done = await db.scalar(text("select count(*) from ent_evidence where scan_id = :s and kind = 'consent'"), {"s": scan_id})
            if done:
                return
            rows = (await db.execute(text(
                r"select original_url from ent_frontier where scan_id = :s and depth = 1 and original_url !~* '\.(pdf|docx?|odt|rtf|xlsx?|pptx?)($|\?)' "
                "order by priority, discovered_at limit 12"), {"s": scan_id})).all()
        root_key = root_url.rstrip("/")
        further = [r.original_url for r in rows if r.original_url.rstrip("/") != root_key][:3]
        make_context, goto = make_session_tools(browser, settings, host_guard(root_url), _new_context_kwargs)
        results = await run_consent_suite(make_context, goto, root_url, further, max_parallel=1)
        async with async_session_factory() as db:
            for name, result in sorted(results.items()):
                await evidence.store(db, scan_id, kind="consent", source_url=root_url, content=json.dumps({"test": name, **result}, sort_keys=True, ensure_ascii=False),
                                     detection_method=f"consent_test:{name}", content_type="application/json")
            await db.commit()
    except Exception:
        logger.warning("enterprise scan %s: the consent tests could not be kept", scan_id, exc_info=True)


async def _keep_observations(db, scan_id, claimed: frontier.Claimed, page, root_url: str, *, prints: Fingerprints | None = None) -> None:
    """What the browser saw on this page, kept as evidence rows: cookies, requests, forms, the response's headers with the inspection record, and what the 5-page agent's first-page sequence
    observed (and, for the homepage, its screenshot). Each row names its page and the consent state, which is always `pre_consent` here (a fresh context that has given no consent).
    Each cookie and network row also lists what on this page was NEW in the scan (by fingerprint), so repeated cookies and trackers are recognised as the same ones. One bad row never
    loses the others."""
    state = "pre_consent"
    cookies = page_inspect.normalize_cookies(page.cookies, root_url, state)
    now = time.time()
    for c in cookies:
        c["lifetime"] = cookie_lifetime(c, now)
    non_essential = non_essential_requests([(r["url"], r["type"]) for r in page.requests], urlparse(root_url).netloc)
    seen_here = Fingerprints.of_page(cookies, page.requests, non_essential)
    new = prints.new_on(seen_here) if prints is not None else None
    changed = prints.changed_on(cookies) if prints is not None else None
    rows = [
        # Every cookie on the page is kept with the page (the page's own record); `new_in_scan` and `changed_in_scan` say which identities are new to the scan or seen before with other
        # flags or another lifetime, and every other one is one already recorded on an earlier page (the report shows each identity once, with every page it was seen on).
        ("cookie", {"page_url": page.final_url, "consent_state": state, "cookies": cookies,
                    **({"new_in_scan": new["cookie"], "changed_in_scan": changed} if new is not None else {})}, "browser_cookies"),
        # `non_essential` lists the analytics and advertising services this page contacted before any choice, classified by the same catalogue the 5-page scanner uses.
        ("network", {"page_url": page.final_url, **page_inspect.summarise_requests(page.requests, root_url, state), "non_essential": non_essential,
                     "script_hosts": sorted(seen_here["script"])[:200], "script_urls": sorted(seen_here["script_url"])[:300],
                     **({"new_in_scan": {"trackers": new["tracker"], "script_hosts": new["script"], "script_urls": new["script_url"]}} if new is not None else {})}, "browser_requests"),
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
    five = dict(getattr(page, "five_page", None) or {})
    shot = five.pop("screenshot", None)
    if five:
        rows.append(("header", {"page_url": page.final_url, "five_page_inspection": five}, "five_page_inspection"))
    if shot:
        rows.append(("screenshot", {"page_url": page.final_url, **shot}, "homepage_screenshot"))
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
    except doc_fetch.DocumentOffHost as exc:
        await frontier.mark_excluded(db, claimed.id, str(exc))
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


#: Audit events that mark a finished phase, so a resumed scan does not repeat it.
DISCOVERY_DONE = "url_discovery_completed"
PRIVACY_REVIEWED = "privacy_policy_reviewed"


async def _audit_has(scan_id, event: str) -> bool:
    async with async_session_factory() as db:
        return bool(await db.scalar(text("select count(*) from ent_audit_events where scan_id = :s and event = :e"), {"s": scan_id, "e": event}))


async def _audit(scan_id, event: str, detail: dict) -> None:
    async with async_session_factory() as db:
        await store.audit(db, scan_id, "system", event, detail)
        await db.commit()


async def _read_links(context, url: str, *, settings, is_home: bool) -> dict:
    """Phase 1's visit of one page: the 5-page agent's navigation (bounded, classified retries, a block page or a "not found" page recognised), the load event and the settle window, the
    progressive scroll (footer and lazy-loaded links appear), then the rendered page's links. Nothing is kept as evidence: the deep inspection of phase 3 is the page's record.
    Returns {"outcome": "loaded" | "blocked" | "not_found" | "failed", "final_url", "links", "verdict", "error"}."""
    page = await context.new_page()
    page.on("dialog", lambda d: asyncio.ensure_future(d.dismiss()))
    requests: list[str] = []
    page.on("request", lambda r: requests.append(r.url) if len(requests) < 5000 else None)
    try:
        window = settings.scanner_nav_retry_seconds_seed if is_home else settings.scanner_nav_retry_seconds_page
        response, outcome, exc, attempts = await _navigate_with_retry(page, url, settings.scanner_timeout_seconds * 1000, window_s=window)
        if outcome != "success":
            status = getattr(exc, "status", None)
            verdict = _verdict_for_failure(status, exc)
            msg = (str(exc).splitlines()[0][:300] if exc else outcome) + f" (after {attempts} attempt(s))"
            return {"outcome": "blocked" if verdict else "failed", "verdict": verdict, "error": msg, "final_url": None, "links": []}
        await _wait_for_load_event(page, url)
        await page.wait_for_timeout(_TRACKER_SETTLE_MS)
        if await _page_is_a_block_page(page):
            body = await page.evaluate("document.body ? document.body.innerText : ''")
            verdict = blocks.detect_block(403, {}, await page.title(), body)
            return {"outcome": "blocked", "verdict": verdict, "error": "the page is a bot-protection message", "final_url": page.url, "links": []}
        if await _page_says_it_was_not_found(page):
            return {"outcome": "not_found", "verdict": None, "error": None, "final_url": page.url, "links": []}
        await _progressive_scroll(page, request_count=lambda: len(requests))
        html = await page.content()
        return {"outcome": "loaded", "verdict": None, "error": None, "final_url": page.url, "links": _links(html, page.url)}
    except Exception as exc:                                   # noqa: BLE001 -- one page's failure is that page's outcome
        return {"outcome": "failed", "verdict": None, "error": f"{type(exc).__name__}: {exc}"[:300], "final_url": None, "links": []}
    finally:
        try:
            await page.close()
        except Exception:                                      # noqa: BLE001
            pass


async def _discover_inventory(*, scan_id, org_id, root_url, rules, browser, settings, request_context, limiter, guard, max_urls: int, max_depth: int) -> dict:
    """PHASE 1 (owner's sequence, 19 Nov 2026): discover the whole URL inventory before any page is deeply inspected. Starting from the submitted homepage and the sitemap (already recorded
    by `discovery.seed`), every pending page on the submitted host is visited ONCE, one at a time, only to read its links (`_read_links`); each link is normalised, de-duplicated and recorded
    in the frontier (the database), with subdomains and other websites recorded as excluded with their reason. Redirects are resolved: a hop to another host excludes the URL (never followed),
    and a same-host redirect to another recorded address marks a duplicate destination. The same blocking rule as the deep inspection applies. When every reachable page has been read the
    phase is marked done (audit `url_discovery_completed`); a scan interrupted here repeats the discovery visits on resume (the frontier keeps what was recorded).

    Returns {"visited", "loaded", "failed", "links_added", "off_host", "block": bool, "stopped": None | "cancelled" | <limit>, "seed_off_host": str | None}."""
    from collections import deque
    out: dict = {"visited": 0, "loaded": 0, "failed": 0, "links_added": 0, "off_host": 0, "block": False, "stopped": None, "seed_off_host": None}
    async with async_session_factory() as db:
        rows = (await db.execute(text("select original_url, depth from ent_frontier where scan_id = :s and status = 'pending' order by depth, discovered_at"), {"s": scan_id})).all()
    queue = deque((r.original_url, r.depth) for r in rows)
    visited: set[str] = set()
    in_a_row = 0
    ctx = await browser.new_context(**_new_context_kwargs(settings))
    stopped: list[dict] = []
    await ctx.route("**/*", host_guard(root_url, stopped))
    try:
        while queue:
            url, depth = queue.popleft()
            key = frontier.key_of(url)
            if key in visited or scope.classify(url, root_url) != scope.PAGE:
                continue                                        # documents have no links to follow; they are fetched in phase 3
            visited.add(key)
            if await _is_cancelled(scan_id, org_id):
                out["stopped"] = "cancelled"
                break
            verdict = guard.check()
            while verdict == limits.PAUSE:
                await asyncio.sleep(limits.POLL_SECONDS)
                verdict = guard.check()
            if verdict is not None:
                out["stopped"] = verdict
                break
            async with async_session_factory() as db:
                row = await frontier.find_row(db, scan_id, url)
                if row is None or row.status != "pending":
                    continue
                if not rules.allows(url):
                    await frontier.mark_excluded(db, row.id, "robots_txt: the site does not allow the scanner to visit this page")
                    await db.commit()
                    continue
            await limiter.acquire()
            chain = await redirects.check(request_context, url, root_url)
            if chain.off_host:
                async with async_session_factory() as db:
                    await frontier.mark_excluded(db, row.id, scope.exclusion_reason(chain.off_host, root_url, context=f"{url} redirects to {chain.off_host}"))
                    await db.commit()
                out["off_host"] += 1
                if depth == 0:
                    out["seed_off_host"] = chain.off_host
                    break
                continue
            out["visited"] += 1
            stopped.clear()
            res = await _read_links(ctx, url, settings=settings, is_home=depth == 0)
            claimed = frontier.Claimed(id=row.id, url=url, depth=depth, attempts=0)
            if res["outcome"] == "blocked":
                _note_block(scan_id, claimed, res["verdict"])
                in_a_row += 1
                if should_stop_for_block(verdict=res["verdict"], inspected=out["loaded"], in_a_row=in_a_row, seed=depth == 0):
                    out["block"] = True
                    break
                continue                                        # this URL stays pending: phase 3 tries it again and records its own outcome
            if res["outcome"] == "failed":
                out["failed"] += 1                              # stays pending: phase 3 retries it with its own bounded attempts and records the reason
                continue
            async with async_session_factory() as db:
                if res["outcome"] == "not_found":
                    await frontier.mark_excluded(db, row.id, "soft_404: the page answered 200 but says it was not found")
                    await db.commit()
                    continue
                in_a_row = 0
                out["loaded"] += 1
                final = res["final_url"] or url
                if not scope.same_site(final, root_url):
                    await frontier.mark_excluded(db, row.id, off_host_reason(url, final, stopped, root_url))
                    await db.commit()
                    out["off_host"] += 1
                    if depth == 0:
                        out["seed_off_host"] = final
                        break
                    continue
                if frontier.key_of(final) != key:
                    dest = await frontier.find_row(db, scan_id, final)
                    if depth == 0:
                        # The homepage redirects within the host (/ -> /en/): it stays the homepage; its destination is not inspected a second time.
                        if dest is None:
                            await frontier.add_url(db, scan_id, final, depth=1, source="redirect", max_urls=max_urls, max_depth=max_depth,
                                                   exclude_reason=f"duplicate_destination: the homepage {url} redirects here; inspected as the homepage"[:1000])
                        elif dest.status == "pending":
                            await frontier.mark_excluded(db, dest.id, f"duplicate_destination: the homepage {url} redirects here; inspected as the homepage"[:1000])
                    elif dest is not None:
                        await frontier.mark_excluded(db, row.id, f"duplicate_destination: redirects to {final}, which is recorded and inspected under its own address"[:1000])
                    else:
                        # Inspect the page at the address it really is: the destination is recorded and the redirecting address is excluded with the reason.
                        result = await frontier.add_url(db, scan_id, final, depth=depth, source="redirect", max_urls=max_urls, max_depth=max_depth)
                        if result == frontier.ADDED:
                            await frontier.mark_excluded(db, row.id, f"redirect: redirects to {final}, which is inspected under that address"[:1000])
                            queue.appendleft((final, depth))
                added = await discovery.add_links(db, scan_id, root_url, res["links"], parent_depth=depth, max_urls=max_urls, max_depth=max_depth)
                await frontier.add_not_recorded(db, scan_id, added["limit"])
                await db.commit()
            out["links_added"] += added["added"]
            out["off_host"] += added["off_host"]
            queue.extend((u, depth + 1) for u in added["added_urls"])
    finally:
        await ctx.close()
    if not out["block"] and out["stopped"] is None and out["seed_off_host"] is None:
        async with async_session_factory() as db:
            c = await frontier.counts(db, scan_id)
        await _audit(scan_id, DISCOVERY_DONE, {**{k: v for k, v in out.items() if k not in ("block", "stopped", "seed_off_host")}, "inventory": c})
    logger.info("enterprise scan %s: URL discovery %s", scan_id, out)
    return out


class _InterimRepo:
    """The text analysis' repository for the privacy-first review: it reads the scan's evidence and records every unit of work it does (so the final analysis reuses those readings
    instead of asking the model again), but it does NOT change the scan's analysis status, its saved assessments or the pages' analysis status: those belong to the final analysis, after
    every URL has been inspected."""

    def __init__(self, inner) -> None:
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    async def set_scan_status(self, *a, **k) -> None:
        return None

    async def save_assessments(self, *a, **k) -> int:
        return 0

    async def set_frontier_status(self, *a, **k) -> None:
        return None

    async def set_frontier_tracking(self, *a, **k) -> None:
        return None


async def _privacy_review(scan_id, org_id) -> dict:
    """PHASE 2 (owner's sequence, 19 Nov 2026): once every privacy policy page has been inspected, and before any other page is, their text is read against the DPDP parameters: the facts
    are extracted with exact quotes from the stored evidence (each with its evidence id, address and page number), and the parameters they bear on are compared, by the same text analysis
    the final assessment uses. Its readings are kept (the final analysis reuses them, so nothing is read by the model twice); its interim result is recorded in the audit trail
    (`privacy_policy_reviewed`). The final score is NOT computed here: only after every URL is inspected. A failure here is recorded and the crawl goes on."""
    async with async_session_factory() as db:
        rows = (await db.execute(text("select original_url, final_url from ent_frontier where scan_id = :s and priority = 0 and status = 'done' order by discovered_at"), {"s": scan_id})).all()
    pages = [r.final_url or r.original_url for r in rows]
    detail: dict = {"privacy_pages": pages[:200]}
    if not pages:
        detail["note"] = "no privacy policy page was found among the discovered URLs; the final analysis searches every inspected page"
        await _audit(scan_id, PRIVACY_REVIEWED, detail)
        return detail
    try:
        from app.enterprise.analysis.pipeline import run_text_analysis
        from app.enterprise.analysis.repo import SqlRepo
    except ImportError:
        detail["note"] = "the text analysis is not installed"
        await _audit(scan_id, PRIVACY_REVIEWED, detail)
        return detail
    try:
        outcome = await run_text_analysis(scan_id, org_id, repo=_InterimRepo(SqlRepo()))
        detail.update({
            "status": outcome.status, "error": outcome.error, "facts_kept": outcome.facts_kept, "facts_discarded": outcome.facts_discarded, "chunks_done": outcome.chunks_done,
            "chunks_failed": outcome.chunks_failed, "chunks_shared_in_scan": getattr(outcome, "chunks_shared_in_scan", 0),
            "assessments": [{"parameter_id": a.parameter_id, "state": a.state, "evidence_refs": list(a.evidence_refs)[:20], "confidence": a.confidence, "note": (a.note or "")[:500]}
                            for a in outcome.assessments],
            "interim": "privacy policy pages only; the final assessment is made after every URL is inspected",
        })
    except Exception as exc:                                   # noqa: BLE001
        logger.exception("enterprise scan %s: the privacy policy review failed", scan_id)
        detail["error"] = f"{type(exc).__name__}: {exc}"[:300]
    await _audit(scan_id, PRIVACY_REVIEWED, detail)
    return detail


async def _privacy_pages_left(scan_id) -> int:
    async with async_session_factory() as db:
        return int(await db.scalar(text("select count(*) from ent_frontier where scan_id = :s and priority = 0 and status in ('pending', 'in_progress')"), {"s": scan_id}) or 0)


async def run(scan_id: uuid.UUID, org_id: uuid.UUID, *, worker_id: str = WORKER_ID, timeout_ms: int = 30_000) -> str:
    """Crawl one scan to the end (or until it is cancelled), one URL at a time in the owner's sequence. Returns the scan's final status. Safe to call again after a crash or a resume."""
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
    rps = float(options.get("per_domain_rps", 2.0))
    # `concurrency` stays in the scan options for the API, but page inspection is strictly sequential: one URL is opened, inspected and stored before the next (owner's rule, 19 Nov 2026).
    settings = get_settings()
    limiter = DomainRateLimiter(rps)
    guard = limits.LimitGuard(memory_budget_mb=int(options.get("memory_budget_mb", 0)), max_runtime_seconds=int(options.get("max_runtime_seconds", 0)))
    limit_hit: str | None = None
    block_stop = None                                  # set when the website's protection refused the scanner and the crawl was stopped for it
    seed_off_host: str | None = None                   # the submitted homepage redirects to another host: nothing on the submitted host can be scanned
    _BLOCKS.pop(scan_id, None)                         # a resumed scan starts its blocking count afresh; the pages it refused before were put back by the resume
    prints = Fingerprints()
    await prints.load(scan_id)
    in_a_row = 0

    try:
        safe_ips = await assert_safe_url(root_url)
        launch_args = _host_resolver_pin_args(urlparse(root_url).hostname, safe_ips)
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=settings.scanner_headless, args=launch_args)
            try:
                context = await browser.new_context(**_new_context_kwargs(settings))
                await context.route("**/*", host_guard(root_url))
                rules = await discovery.read_robots(context, root_url)

                async def one(c: frontier.Claimed) -> str:
                    return await _process(c, scan_id=scan_id, root_url=root_url, rules=rules, context=context, browser=browser, settings=settings, limiter=limiter,
                                          max_urls=recorded, max_depth=max_depth, timeout_ms=timeout_ms, prints=prints)

                await _keep_tls(scan_id, root_url)
                async with async_session_factory() as db:
                    taken_back = await frontier.reclaim_stale(db, scan_id)
                    if taken_back:
                        logger.info("enterprise scan %s: took back %d unfinished pages (resume)", scan_id, taken_back)
                    if sum((await frontier.counts(db, scan_id)).values()) == 0:
                        seeded = await discovery.seed(db, scan_id, context, root_url, rules, max_urls=recorded, max_depth=max_depth)
                        await frontier.add_not_recorded(db, scan_id, seeded.get("not_recorded", 0))
                    await db.commit()

                # PHASE 1. The whole URL inventory, discovered and recorded before any page is deeply inspected.
                if not await _audit_has(scan_id, DISCOVERY_DONE):
                    found = await _discover_inventory(scan_id=scan_id, org_id=org_id, root_url=root_url, rules=rules, browser=browser, settings=settings,
                                                      request_context=context.request, limiter=limiter, guard=guard, max_urls=recorded, max_depth=max_depth)
                    if found["stopped"] == "cancelled":
                        return "cancelled"
                    if found["block"]:
                        b = _BLOCKS.get(scan_id) or {}
                        block_stop = {**b, "first": b.get("last") or b.get("first"), "url": b.get("last_url") or b.get("url")}
                    elif found["stopped"]:
                        limit_hit = found["stopped"]
                    seed_off_host = found["seed_off_host"]
                    in_a_row = 0
                if seed_off_host is None:
                    async with async_session_factory() as db:
                        seed_row = (await db.execute(text("select status, status_reason from ent_frontier where scan_id = :s and depth = 0 order by discovered_at limit 1"), {"s": scan_id})).first()
                    if seed_row is not None and seed_row.status == "excluded" and (seed_row.status_reason or "").startswith(("excluded_subdomain", "excluded_external_domain")):
                        seed_off_host = seed_row.status_reason
                go = block_stop is None and limit_hit is None and seed_off_host is None

                # The model reads the complete inventory's addresses for policies a pattern does not recognise (they are then read with the privacy policies).
                if go:
                    await slug_triage.triage_safe(scan_id)

                # PHASE 2 begins with the homepage, deeply inspected with the 5-page agent's first-page sequence; then the consent tests on it, before any other page is opened.
                if go:
                    async with async_session_factory() as db:
                        home = await frontier.claim_seed(db, scan_id, worker_id)
                        await db.commit()
                    if home is not None:
                        await one(home)
                        b = _BLOCKS.get(scan_id)
                        if b and b.get("seed"):
                            block_stop = b                     # the homepage itself was refused: nothing else can be assessed
                    async with async_session_factory() as db:
                        home_done = await db.scalar(text("select count(*) from ent_frontier where scan_id = :s and depth = 0 and status = 'done'"), {"s": scan_id})
                    if block_stop is None and home_done:
                        await _keep_consent(scan_id, root_url, browser, settings)
                privacy_reviewed = await _audit_has(scan_id, PRIVACY_REVIEWED)

                # PHASE 2 then 3: the privacy policies (priority 0), their review against the DPDP parameters, then the other legal pages (1), then every remaining URL (2): ONE at a time.
                while go and block_stop is None:
                    if await _is_cancelled(scan_id, org_id):
                        return "cancelled"
                    verdict = guard.check()
                    if verdict == limits.PAUSE:                    # over the memory budget: wait for memory to settle before opening the next page
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
                    if not privacy_reviewed and await _privacy_pages_left(scan_id) == 0:
                        await _privacy_review(scan_id, org_id)          # every privacy policy page is inspected: review them before any other page is opened
                        privacy_reviewed = True
                    async with async_session_factory() as db:
                        await frontier.reclaim_stale(db, scan_id)
                        batch = await frontier.claim_next(db, scan_id, worker_id, limit=1)
                        await db.commit()
                    if not batch:
                        async with async_session_factory() as db:
                            if await frontier.is_finished(db, scan_id):
                                break
                        await asyncio.sleep(IDLE_POLL_SECONDS)         # another worker holds the rest
                        continue
                    before = (_BLOCKS.get(scan_id) or {}).get("count", 0)
                    outcome = await one(batch[0])
                    b = _BLOCKS.get(scan_id)
                    if b and b["count"] > before:                    # this page was refused by the site's protection
                        in_a_row += 1
                        async with async_session_factory() as db:
                            inspected = (await frontier.counts(db, scan_id))["done"]
                        if should_stop_for_block(verdict=b["last"], inspected=inspected, in_a_row=in_a_row, seed=b["seed"]):
                            block_stop = {**b, "first": b["last"], "url": b["last_url"]}     # report the refusal that stopped the crawl
                            break
                    elif outcome == "done":
                        in_a_row = 0
            finally:
                await browser.close()
    except RobotsUnreadable as exc:
        return await _finish(scan_id, org_id, "failed", error=f"robots.txt could not be read, so the site's restrictions are unknown: {exc}")
    except Exception as exc:
        logger.exception("enterprise scan %s failed", scan_id)
        return await _finish(scan_id, org_id, "failed", error=f"{type(exc).__name__}: {exc}"[:500])

    if seed_off_host is not None:
        async with async_session_factory() as db:
            await frontier.mark_unreached(db, scan_id, "the submitted address redirects to another host, so nothing on the submitted host could be scanned")
            await db.commit()
        return await _finish(scan_id, org_id, "failed", error=(
            f"The submitted address {root_url} redirects to another host ({seed_off_host}). The Enterprise scan only scans the exact host that was submitted; "
            "submit the address the site redirects to in order to scan it.")[:500])

    async with async_session_factory() as db:
        c = await frontier.counts(db, scan_id)
    block = _BLOCKS.pop(scan_id, None)
    if block_stop is not None or (block and c["done"] == 0):
        b = block_stop or block
        v = b["first"]
        async with async_session_factory() as db:
            await frontier.mark_unreached(db, scan_id, f"the crawl stopped because the website blocked the scanner at {b['url']} ({v.kind}: {v.signal})")
            await db.commit()
        await error_events.record_block(agent="enterprise", website_url=b["url"] or root_url, verdict=v, module="enterprise.crawl", scan_id=scan_id, wait=True)
        explanation = (f"The {c['done']} page(s) inspected before that are analysed and reported, labelled blocked and incomplete; resume the scan to inspect the rest."
                       if c["done"] else blocks.EXPLANATION)
        return await _finish(scan_id, org_id, "failed", error=f"{v.kind} at {b['url']}: {blocks.USER_MESSAGE} {v.signal[:1].upper() + v.signal[1:]}. {explanation}"[:500],
                             stop_reason=v.kind.lower())
    if block:                                              # some pages were refused but the scan went on: say so, once
        await error_events.record_block(agent="enterprise", website_url=block["url"] or root_url, verdict=block["first"], module="enterprise.crawl", scan_id=scan_id, wait=True)
    if c["done"] == 0:
        why = f"the {limit_hit.replace('_', ' ')} was reached before any page was inspected" if limit_hit else "every URL failed or was excluded"
        return await _finish(scan_id, org_id, "failed", error=f"no page could be scanned ({why})")
    if limit_hit:
        async with async_session_factory() as db:
            await frontier.mark_unreached(db, scan_id, f"the crawl stopped at the {limit_hit.replace('_', ' ')}; resume the scan to inspect it")
            await db.commit()
    return await _finish(scan_id, org_id, "completed", stop_reason=limit_hit)


async def _finish(scan_id: uuid.UUID, org_id: uuid.UUID, to: str, *, error: str | None = None, stop_reason: str | None = None) -> str:
    async with async_session_factory() as db:
        scan = await scans.get_scan(db, scan_id, org_id)
        if scan is not None and scan.status == "running":      # a scan cancelled meanwhile stays cancelled
            await scans.transition(db, scan_id, org_id, to, error=error, stop_reason=stop_reason)
        await db.commit()
        return scan.status if scan is not None else to

