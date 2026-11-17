"""Turning stored evidence rows into what the analysis reads: text sources (web pages and document pages), form sources, and the browser observations.

A web page is one source (its latest stored text); a document is one source per page, ordered. A page that carries forms also yields a `form_text` source: the forms written out as plain text
(fields, labels, the wording near them, anything pre-ticked), which the model reads like any other passage and whose quotes are checked against exactly that text. Identical forms repeated
on many pages (a newsletter box in every footer) are read once: the others are `covered_by` the first.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from urllib.parse import urlparse

from app.enterprise.analysis import embedded as emb
from app.enterprise.analysis import pages as pg
from app.enterprise.analysis.repo import EvRow, FrontierRow

Ev = tuple[str, dict]


@dataclass
class Src:
    evidence_id: str
    url: str
    kind: str                       # page_text | document_text | form_text
    page_number: int
    text: str
    frontier_id: str | None = None
    category: str = pg.OTHER
    virtual: bool = False           # a policy found inside another page: read as part of that page, counted for its own category, never sent to the model a second time


@dataclass
class Page:
    url: str
    category: str
    strength: str
    reasons: tuple[str, ...]
    title: str = ""
    headings: list[str] = field(default_factory=list)
    candidates: tuple[str, ...] = ()
    frontier_ids: set[str] = field(default_factory=set)
    is_document: bool = False
    unresolved: bool = False        # ambiguous and the model could not settle it
    also: list = field(default_factory=list)       # policies embedded in this page as sections (analysis.embedded.Embedded), each in addition to the page's own category


@dataclass
class Loaded:
    texts: list[Src] = field(default_factory=list)
    forms: list[Src] = field(default_factory=list)
    form_covers: dict[str, list[str]] = field(default_factory=dict)      # representative form url -> urls of identical forms it stands for
    pages: dict[str, Page] = field(default_factory=dict)
    form_data: list[Ev] = field(default_factory=list)
    networks: list[Ev] = field(default_factory=list)
    cookies: list[Ev] = field(default_factory=list)
    headers: list[Ev] = field(default_factory=list)
    consent: dict[str, Ev] = field(default_factory=dict)
    frontier: list[FrontierRow] = field(default_factory=list)

    def of(self, *categories: str) -> list[Src]:
        return [s for s in self.texts if s.category in categories]


def form_text(content: dict) -> str:
    """The forms of one page as plain text. Field values are never read (the crawler never typed anything); this is names, labels and wording."""
    out: list[str] = []
    for f in content.get("forms", []):
        out.append(f"FORM {f.get('index', 0) + 1} (method {str(f.get('method', 'get')).upper()}, sends to {f.get('action_host') or 'this site'}):")
        for x in f.get("fields", []):
            bits = [x.get("label") or x.get("placeholder") or x.get("name") or x.get("id") or x.get("type", "field")]
            bits.append(f"[{x.get('type', 'text')}{', required' if x.get('required') else ''}{', PRE-TICKED' if x.get('checked') else ''}]")
            out.append("  Field: " + " ".join(str(b) for b in bits if b))
        if f.get("label_text"):
            out.append(f"  Wording near the form: {f['label_text']}")
        if f.get("text"):
            out.append(f"  Form text: {f['text']}")
        out.append(f"  Link to a privacy notice in the form: {'yes' if f.get('privacy_link') else 'no'}")
    return "\n".join(out)


def form_signature(content: dict) -> str:
    parts = []
    for f in content.get("forms", []):
        parts.append(json.dumps([sorted((x.get("type", ""), x.get("name", "")) for x in f.get("fields", [])), " ".join((f.get("text") or "").split()), f.get("label_text") or ""], sort_keys=True))
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def build(evidence: list[EvRow], frontier: list[FrontierRow]) -> Loaded:
    """Group the stored evidence. Classification here is the deterministic part only; ambiguous pages are settled by the pipeline."""
    out = Loaded(frontier=frontier)
    hints: dict[str, tuple[str, ...]] = {}
    for fr in frontier:
        if fr.slug_kinds:
            hints[fr.final_url or fr.url] = fr.slug_kinds
            hints.setdefault(fr.url, fr.slug_kinds)
    text_rows: dict[tuple[str, int], EvRow] = {}
    html: dict[str, str] = {}
    form_rows: dict[str, EvRow] = {}
    for r in evidence:                                          # rows arrive oldest first: a later row of the same place replaces an earlier one
        if r.kind in ("page_text", "document_text"):
            text_rows[(r.source_url, r.page_number)] = r
        elif r.kind == "page_html":
            html[r.source_url] = r.content
        elif r.kind == "form":
            form_rows[r.source_url] = r
            out.form_data.append((r.id, _json(r.content)))
        elif r.kind == "network":
            out.networks.append((r.id, _json(r.content)))
        elif r.kind == "cookie":
            out.cookies.append((r.id, _json(r.content)))
        elif r.kind == "header" and r.detection_method != "tls_probe":
            out.headers.append((r.id, _json(r.content)))
        elif r.kind == "consent":
            out.consent[r.detection_method.removeprefix("consent_test:")] = (r.id, _json(r.content))
    for (url, number), r in sorted(text_rows.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        if not r.content or not r.content.strip():
            continue
        page = out.pages.get(url)
        if page is None:
            title, heads = pg.title_and_headings(html.get(url, "")) if r.kind == "page_text" else ("", [])
            hint = hints.get(url, ())
            facts = pg.PageFacts(url, title, heads, r.content if r.kind == "page_text" else r.content, hint)
            c = pg.classify_page(facts)
            page = out.pages[url] = Page(url, c.category, c.strength, c.reasons, title, heads, c.candidates, is_document=r.kind == "document_text", unresolved=c.ambiguous)
            if r.kind == "page_text" and c.category not in (pg.ARTICLE, pg.PRODUCT):
                _embedded(page, html.get(url), r.content)
                for e in page.also:
                    out.texts.append(Src(r.id, url, r.kind, number, e.text, r.frontier_id, e.kind, virtual=True))
        if r.frontier_id:
            page.frontier_ids.add(r.frontier_id)
        out.texts.append(Src(r.id, url, r.kind, number, r.content, r.frontier_id, page.category))
    seen: dict[str, str] = {}
    for url, r in sorted(form_rows.items()):
        data = _json(r.content)
        text = form_text(data)
        if not text:
            continue
        sig = form_signature(data)
        page = out.pages.get(url)
        if page is None:
            page = out.pages[url] = Page(url, pg.OTHER, "medium", ("a page with forms and no stored text",))
        if r.frontier_id:
            page.frontier_ids.add(r.frontier_id)
        if sig in seen:
            out.form_covers.setdefault(seen[sig], []).append(url)
            continue
        seen[sig] = url
        out.forms.append(Src(r.id, url, "form_text", 0, text, r.frontier_id, page.category))
    return out


def _embedded(page: Page, html: str | None, text: str) -> None:
    """Find the policies this page holds as sections. A page with no category of its own that holds some (a "Legal" hub) takes the first as its category."""
    try:
        found = emb.detect(html, text, page.category)
    except Exception:                                   # a page the parser cannot read is not a reason to lose the page
        return
    if not found:
        return
    if page.category == pg.OTHER and not page.unresolved:
        first, found = found[0], found[1:]
        page.category, page.strength = first.kind, "medium"
        page.reasons = page.reasons + (f"no policy title, but its heading \"{first.heading}\" names a {first.kind.replace('_', ' ')} with {len(first.text):,} characters under it",)
    page.also = found
    if found:
        page.reasons = page.reasons + (f"it also holds {', '.join(e.kind.replace('_', ' ') for e in found)} as sections",)


def apply_category(loaded: Loaded, url: str, category: str, reason: str) -> None:
    """Settle an ambiguous page (what the model said), keeping the reason."""
    page = loaded.pages.get(url)
    if page is None:
        return
    page.category, page.unresolved, page.strength = category, False, "medium"
    page.reasons = page.reasons + (reason,)
    for s in loaded.texts:
        if s.url == url:
            s.category = category


def _json(s: str) -> dict:
    try:
        v = json.loads(s)
        return v if isinstance(v, dict) else {}
    except (ValueError, TypeError):
        return {}


def host_of(url: str) -> str:
    return (urlparse(url).hostname or "").lower()
