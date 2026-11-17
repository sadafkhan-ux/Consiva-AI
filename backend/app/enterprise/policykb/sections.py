"""Clean a policy page into text and sections, and cut it into document-aware chunks.

The cleaning uses a real HTML parser (never a regex over markup). It drops what is not the page's own content (scripts, styles, navigation, repeated site header and footer) and keeps what is:
headings, paragraphs, lists, tables (one row per line), links (mail, phone and web addresses) and the text inside expanded sections, which is already in the rendered DOM.

The chunker cuts at section boundaries first: a chunk holds whole paragraphs under one heading wherever it can, a long section is split paragraph by paragraph, and only a single paragraph longer than a
chunk is split by length. Every chunk keeps the heading it sits under. Nothing is dropped: the chunks, read in order, hold every paragraph of the cleaned text.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup, NavigableString, Tag

from app.enterprise.analysis.chunking import chunk_text

#: A chunk aims for this many characters (about 450 tokens); a section shorter than MIN_CHARS is joined with the one after it.
CHUNK_CHARS = 1800
MIN_CHARS = 300

_DROP = ("script", "style", "noscript", "template", "svg", "canvas", "iframe", "object", "embed", "head", "meta", "link")
_BOILERPLATE = ("nav", "header", "footer", "aside")
_BLOCKS = ("h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "tr", "blockquote", "pre", "summary", "dt", "dd", "figcaption")
_WS = re.compile(r"[ \t\r\f\v ]+")
_COOKIE_UI = re.compile(r"(cookie|consent|gdpr|onetrust|cc-window|cmp)[-_ ]?(banner|bar|notice|modal|popup|dialog|overlay|widget|manager)", re.I)


@dataclass
class Section:
    heading: str | None
    level: int
    paragraphs: list[str] = field(default_factory=list)


@dataclass
class CleanDoc:
    text: str
    sections: list[Section]
    method: str                      # "html" or "text"
    note: str | None = None          # anything the reader should know (the HTML was missing, the DOM text was much shorter than the page text)


@dataclass
class PolicyChunk:
    index: int
    heading: str | None
    content: str
    content_hash: str
    token_estimate: int


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalise_text(text: str) -> str:
    """The text a version's hash is taken over: whitespace collapsed, blank lines limited, so a change in spacing or line breaks alone is not a change in the policy."""
    lines = [_WS.sub(" ", ln).strip() for ln in (text or "").replace("\r", "").split("\n")]
    out: list[str] = []
    for ln in lines:
        if ln or (out and out[-1]):
            out.append(ln)
    return "\n".join(out).strip()


def _own_text(el: Tag) -> str:
    """The element's text with links kept as `text <address>` for mail, phone and web addresses, and without the text of lists nested inside it (those are blocks of their own)."""
    parts: list[str] = []

    def walk(node) -> None:
        for child in node.children:
            if isinstance(child, NavigableString):
                parts.append(str(child))
            elif isinstance(child, Tag):
                if child.name in ("ul", "ol", "table"):
                    continue
                if child.name == "br":
                    parts.append(" ")
                elif child.name == "a" and child.get("href"):
                    label = _WS.sub(" ", child.get_text(" ", strip=True))
                    href = str(child["href"]).strip()
                    parts.append(f"{label} <{href}>" if label and re.match(r"(https?://|mailto:|tel:)", href, re.I) and label != href else label or href)
                else:
                    walk(child)

    walk(el)
    return _WS.sub(" ", "".join(parts)).strip()


def _row_text(tr: Tag) -> str:
    cells = [_WS.sub(" ", c.get_text(" ", strip=True)) for c in tr.find_all(["td", "th"], recursive=False)]
    return " | ".join(c for c in cells if c)


def _root(soup: BeautifulSoup) -> Tag:
    body = soup.body or soup
    total = len(body.get_text(" ", strip=True))
    for sel in ("main", "article", '[role="main"]'):
        found = soup.select_one(sel)
        if found is not None and len(found.get_text(" ", strip=True)) >= max(500, total * 0.3):
            return found
    return body


def clean_html(html: str) -> CleanDoc:
    soup = BeautifulSoup(html or "", "html.parser")
    for el in soup.find_all(_DROP):
        el.decompose()
    root = _root(soup)
    if root is soup.body or root is soup:                         # a whole-body read: the site's own header, navigation and footer are not the policy
        for el in root.find_all(_BOILERPLATE):
            el.decompose()
    for el in root.find_all(attrs={"class": _COOKIE_UI}) + root.find_all(attrs={"id": _COOKIE_UI}):
        el.decompose()

    sections: list[Section] = [Section(None, 0)]
    last = ""
    for el in root.find_all(_BLOCKS):
        if el.name in ("li", "p", "figcaption", "blockquote", "dt", "dd", "pre") and el.find_parent(["td", "th"]) is not None:
            continue                                              # the table row carries it
        if el.name == "p" and el.find_parent("li") is not None:
            continue                                              # the list item carries it
        text = _row_text(el) if el.name == "tr" else _own_text(el)
        if el.name == "tr" and el.find_parent("table") is not None and el.find_parent("table").find_parent("table") is not None:
            continue
        if not text or text == last:
            continue
        last = text
        if el.name[0] == "h" and len(el.name) == 2:
            sections.append(Section(text, int(el.name[1])))
        elif el.name == "summary":
            sections.append(Section(text, 7))                     # a collapsible section's title opens a section of its own
        else:
            sections[-1].paragraphs.append(("• " + text) if el.name == "li" else text)
    sections = [s for s in sections if s.heading or s.paragraphs]
    return CleanDoc(_render(sections), sections, "html")


def clean_plain_text(text: str) -> CleanDoc:
    """The fallback when a page has no stored HTML: the rendered text, with heading-looking lines (short, no sentence end, followed by prose) starting sections."""
    from app.enterprise.analysis.pages import heading_lines

    heads = set(heading_lines(text))
    sections: list[Section] = [Section(None, 0)]
    for line in (ln.strip() for ln in (text or "").split("\n")):
        if not line:
            continue
        if line in heads:
            sections.append(Section(line, 2))
        else:
            sections[-1].paragraphs.append(line)
    sections = [s for s in sections if s.heading or s.paragraphs]
    return CleanDoc(_render(sections), sections, "text", "no stored HTML: sections were inferred from the text")


def _render(sections: list[Section]) -> str:
    blocks: list[str] = []
    for s in sections:
        if s.heading:
            blocks.append(s.heading)
        blocks.extend(s.paragraphs)
    return normalise_text("\n\n".join(blocks))


def clean_page(html: str | None, page_text: str | None) -> CleanDoc:
    """The page's cleaned text. The HTML is the primary source (it keeps the structure). When it is missing, or its cleaned text is under half of the page's rendered text (a page that builds its content
    in ways the cleaner does not read), the rendered text is used instead and the note says so: a policy is never stored short without saying why."""
    plain = normalise_text(page_text or "")
    if html and html.strip():
        doc = clean_html(html)
        if plain and len(doc.text) < 0.5 * len(plain):
            fallback = clean_plain_text(page_text or "")
            fallback.note = f"the cleaned HTML held {len(doc.text):,} characters against {len(plain):,} in the rendered text, so the rendered text was used"
            return fallback
        return doc
    if plain:
        return clean_plain_text(page_text or "")
    return CleanDoc("", [], "text", "the page has no stored text")


def chunk_document(doc: CleanDoc, *, max_chars: int = CHUNK_CHARS, min_chars: int = MIN_CHARS) -> list[PolicyChunk]:
    """Document-aware chunks: whole paragraphs under one heading; a short section joins the next; a long section is split paragraph by paragraph; one oversized paragraph is split by length."""
    pieces: list[tuple[str | None, str]] = []                      # (heading, paragraph) in reading order, oversized paragraphs already split
    for s in doc.sections:
        paras = list(s.paragraphs) or ([] if not s.heading else [])
        for p in paras:
            if len(p) > max_chars:
                pieces.extend((s.heading, c.text.strip()) for c in chunk_text(p, max_chars=max_chars, overlap=0) if c.text.strip())
            else:
                pieces.append((s.heading, p))
        if s.heading and not paras:
            pieces.append((s.heading, ""))                         # a heading with nothing under it still travels with the next paragraph

    chunks: list[PolicyChunk] = []
    cur_heads: list[str] = []
    cur: list[str] = []
    size = 0

    def flush() -> None:
        nonlocal cur_heads, cur, size
        body = "\n".join(cur).strip()
        if body:
            heading = " / ".join(dict.fromkeys(h for h in cur_heads if h))[:300] or None
            content = f"{heading}\n{body}" if heading and not body.startswith(heading) else body
            chunks.append(PolicyChunk(len(chunks), heading, content, sha256(content), max(1, len(content) // 4)))
        cur_heads, cur, size = [], [], 0

    prev_heading: str | None = object()                            # type: ignore[assignment]
    for heading, para in pieces:
        new_section = heading != prev_heading
        # close the chunk when it is big enough and a new section begins, or when this paragraph would overflow it
        if cur and ((new_section and size >= min_chars) or size + len(para) > max_chars):
            flush()
        if new_section and heading:
            cur_heads.append(heading)
        if para:
            cur.append(para)
            size += len(para) + 1
        prev_heading = heading
    flush()
    return chunks
