"""Everything method 3.0.0 reads, rebuilt from what a scan already stored: no model call, no browser, no website.

The text analysis (analysis/pipeline.py) leaves its work in the database: the evidence rows, the frontier, and one row per unit of analysis (`ent_analysis_chunks`) holding the facts
each passage yielded (with their verified quotes), the model's settling of ambiguous pages, and its few interpretations. This module puts the same context back together, so the
96 parameters are worked out from exactly the facts and evidence the 51-parameter method used, and can be worked out again (after a review, or for a scan analysed earlier)
without reading anything twice.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import date
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

from app.enterprise.analysis import consolidate as co
from app.enterprise.analysis import sources as sr
from app.enterprise.analysis.extract import normalise
from app.enterprise.analysis.repo import ChunkRow, EvRow, FrontierRow, SqlRepo

#: The address of a notice, as a link points at it.
NOTICE_HREF = re.compile(r"privacy|data-?protection|datenschutz|/legal/privacy", re.I)
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
_VISIBLE_STRIP = re.compile(r"<(script|style|template|noscript)\b[^>]*>.*?</\1\s*>|<!--.*?-->", re.I | re.S)


class _Anchors(HTMLParser):
    """The links of a page: (href, text). Tolerant of broken HTML; never raises on content."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href = dict(attrs).get("href") or ""
            self._text = []

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            self.links.append((self._href, " ".join("".join(self._text).split())[:200]))
            self._href = None


def anchors(html: str, base_url: str) -> list[tuple[str, str]]:
    p = _Anchors()
    try:
        p.feed(html or "")
        p.close()
    except Exception:                       # noqa: BLE001 -- a page the parser cannot read has no links we can rely on
        return []
    return [(urljoin(base_url, h), t) for h, t in p.links if h and not h.startswith(("javascript:", "mailto:", "tel:", "#"))]


@dataclass
class PageHtml:
    """What method 3.0.0 needs from a page's HTML, kept small: whether it links to the notice, the e-mail addresses it shows, and (for policy pages) its links."""
    url: str
    notice_link: bool
    emails: set[str]
    links: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class M3Context:
    scan_id: str
    root_url: str
    as_of: date
    ctx: co.Ctx
    html: dict[str, PageHtml]
    tls: dict | None
    frontier_rows: list[dict]
    scan: dict
    judgments: dict[str, dict]
    banner_text: str = ""                     # the homepage's control text and shadow-DOM text, where a consent banner draws itself
    five_page: dict[str, dict] = field(default_factory=dict)       # page url -> the 5-page first-page inspection record kept with it

    @property
    def loaded(self) -> sr.Loaded:
        return self.ctx.loaded


def _facts(loaded: sr.Loaded, chunks: dict[tuple, ChunkRow]) -> tuple[list[dict], dict[str, int]]:
    """The kept facts and what was not read, in the same way the text analysis assembles them (analysis/pipeline.py, step 4)."""
    from app.enterprise.analysis.pipeline import _plan

    facts: list[dict] = []
    unread: dict[str, int] = {}
    seen: set[tuple] = set()
    for w in _plan(loaded):
        c = chunks.get(w.row.key)
        cat = "forms" if w.src.kind == "form_text" else w.src.category
        if c is None or c.status != "done":
            unread[cat] = unread.get(cat, 0) + 1
            continue
        for f in (c.result or {}).get("facts", []):
            k = (f["topic"], f["quote"], w.src.evidence_id)
            if k in seen:
                continue
            seen.add(k)
            base = {**f, "evidence_id": w.src.evidence_id, "url": w.src.url, "page_number": w.src.page_number, "kind": w.src.kind}
            facts.append({**base, "id": len(facts), "category": cat if cat != "forms" else w.src.category})
            page = loaded.pages.get(w.src.url)
            for e in (page.also if page is not None and cat != "forms" else []):
                if normalise(f["quote"]) in normalise(e.text):
                    facts.append({**base, "id": len(facts), "category": e.kind, "embedded_in": cat})
    return facts, unread


def assemble(evidence: list[EvRow], frontier: list[FrontierRow], chunks: list[ChunkRow], *, scan_id: str, root_url: str, as_of: date,
             frontier_rows: list[dict] | None = None, scan: dict | None = None) -> M3Context:
    """The context from rows already loaded (pure: tested without a database)."""
    loaded = sr.build(evidence, frontier)
    by_key = {c.key: c for c in chunks}
    for c in chunks:                                  # the model's settling of ambiguous pages, exactly as the analysis applied it
        if c.level == "category" and c.status == "done" and c.result and c.result.get("confidence") != "low":
            sr.apply_category(loaded, c.source_url, c.result["category"], f"the page was ambiguous; the model read it as {c.result['category']} ({c.result['confidence']} confidence)")
    unresolved = sorted(p.url for p in loaded.pages.values() if p.unresolved)
    facts, unread = _facts(loaded, by_key)
    judgments = {c.source_url.removeprefix("param:"): (c.result or {}) for c in chunks if c.level == "mapping" and c.source_url.startswith("param:") and c.status == "done"}
    ctx = co.Ctx(loaded, facts, unread, as_of, judgments=judgments, unresolved=unresolved)
    policy_urls = {s.url for s in loaded.texts if s.category in (co.PRIVACY, co.GRIEVANCE, co.COOKIE, co.TERMS)}
    html: dict[str, PageHtml] = {}
    tls = None
    five: dict[str, dict] = {}
    for r in evidence:
        if r.kind == "header" and r.detection_method == "five_page_inspection":
            body = sr._json(r.content)
            if body.get("page_url"):
                five[body["page_url"]] = body.get("five_page_inspection") or {}
    for r in evidence:
        if r.kind == "page_html":
            # what a visitor can see: scripts, styles, templates and structured data are not "published on the page", so an address only they carry does not count
            content = _VISIBLE_STRIP.sub(" ", r.content or "")
            links = anchors(content, r.source_url) if r.source_url in policy_urls else []
            html[r.source_url] = PageHtml(r.source_url, bool(re.search(r"<a\b[^>]*href=\"[^\"]*(privacy|data-?protection)[^\"]*\"", content, re.I)),
                                          {e.lower() for e in EMAIL.findall(content) if not e.lower().endswith((".png", ".jpg", ".webp", ".svg", ".gif"))}, links)
        elif r.kind == "header" and r.detection_method == "tls_probe":
            tls = sr._json(r.content)
    home = _home_url(root_url, [s.url for s in loaded.texts] + list(five))
    banner = (five.get(home) or {}).get("control_text") or ""
    home_text = next((s.text for s in loaded.texts if s.url == home and s.kind == "page_text" and not s.virtual), "")
    if "[shadow DOM]" in home_text:
        banner += "\n" + home_text[home_text.index("[shadow DOM]"):][:20000]
    return M3Context(scan_id, root_url, as_of, ctx, html, tls, frontier_rows or [], scan or {}, judgments, banner, five)


def _home_url(root_url: str, urls: list[str]) -> str:
    """The stored address of the entered homepage (with or without its trailing slash)."""
    want = root_url.rstrip("/")
    return next((u for u in urls if u.rstrip("/") == want), root_url)


async def load(scan_id: uuid.UUID, *, as_of: date | None = None) -> M3Context:
    """The context of one stored scan. The caller has set the organisation scope."""
    from sqlalchemy import text

    from app.db.session import async_session_factory
    repo = SqlRepo()
    evidence = await repo.load_evidence(scan_id)
    frontier = await repo.load_frontier(scan_id)
    chunks = await repo.load_chunks(scan_id)
    async with async_session_factory() as db:
        scan = (await db.execute(text("select website_url, status, analysis_status, stop_reason, started_at, completed_at, analysis_started_at, analysis_completed_at, options "
                                      "from ent_scans where id = :s"), {"s": scan_id})).mappings().first()
        rows = (await db.execute(text("select original_url, final_url, status, depth, discovery_source, status_reason, http_status, page_category, page_title, priority "
                                      "from ent_frontier where scan_id = :s order by depth, original_url"), {"s": scan_id})).mappings().all()
    if scan is None:
        raise LookupError(f"Enterprise scan {scan_id} not found")
    return assemble(evidence, frontier, chunks, scan_id=str(scan_id), root_url=scan["website_url"], as_of=as_of or date.today(),
                    frontier_rows=[dict(r) for r in rows], scan=dict(scan))


def same_host(url: str, root_url: str) -> bool:
    return (urlparse(url).hostname or "").lower() == (urlparse(root_url).hostname or "").lower()
