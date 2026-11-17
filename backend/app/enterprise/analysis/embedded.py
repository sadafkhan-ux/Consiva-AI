"""Policies that live inside another policy's page.

A site often serves one page, "Legal" or "Privacy", that holds the privacy notice AND a cookie policy AND the terms AND the grievance officer's details as sections. The page classifier gives the
page one category, so the cookie policy inside it was never seen as a cookie policy. This finds those sections from the page's own structure: a heading that names a policy (with a number or a
"Our" in front of it, or not), followed by enough text to be one. It reads headings and sections with a real HTML parser and a few anchored patterns, never the address and never a keyword
anywhere in the body, and it returns each embedded policy with its own text so it can be analysed under its own name and stored as its own document.

A heading such as "Cookies" under a privacy notice counts only when the section under it has at least MIN_CHARS of text, so a one-line mention of cookies does not make a cookie policy.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.enterprise.analysis import pages as pg
from app.enterprise.policykb import sections as sec

MIN_CHARS = 400

_PREFIX = re.compile(r"^\s*(?:(?:section|part|article)\s+\d+\s*[:.\-–]\s*|\d+(?:\.\d+)*[.)]?\s+|[A-Za-z][.)]\s+)", re.I)
_LEAD = r"(?:our |the |this |these |your )?"
_HEADINGS: tuple[tuple[str, re.Pattern], ...] = (
    (pg.COOKIE, re.compile(rf"^{_LEAD}(?:use of |how we use )?cookies?(?: and (?:similar|other)(?: tracking)? technologies)?(?: (?:policy|notice|statement|settings|preferences))?$", re.I)),
    (pg.PRIVACY, re.compile(rf"^{_LEAD}(?:privacy|data protection|data privacy)(?: (?:policy|notice|statement))?$", re.I)),
    (pg.TERMS, re.compile(rf"^{_LEAD}(?:terms(?: (?:of|and|&) (?:use|service|services|conditions|sale))?|conditions of use|user agreement)$", re.I)),
    (pg.GRIEVANCE, re.compile(r"^(?:grievance(?: redressal| redress)?(?: officer| policy| mechanism)?|data protection officer(?: \(dpo\))?|dpo|complaints?(?: policy| procedure)?)$", re.I)),
)


@dataclass
class Embedded:
    kind: str                # a page category: privacy_policy, cookie_policy, terms or grievance
    heading: str             # the heading that named it (the first one, when several sections name the same policy)
    text: str                # the section's text with its sub-sections, in reading order


def heading_kind(heading: str | None) -> str | None:
    """The policy a heading names, or None. Anchored: "Is your cookie policy ready?" names nothing."""
    h = _PREFIX.sub("", (heading or "").strip().rstrip(":").strip())
    for kind, rx in _HEADINGS:
        if rx.match(h):
            return kind
    return None


def detect(html: str | None, text: str | None, primary: str | None = None, *, doc: sec.CleanDoc | None = None) -> list[Embedded]:
    """The policies named by headings of this page, other than the page's own category, each with its section text."""
    doc = doc or sec.clean_page(html, text)
    found: dict[str, Embedded] = {}
    secs = doc.sections
    for i, s in enumerate(secs):
        kind = heading_kind(s.heading)
        if kind is None or kind == primary:
            continue
        body = list(s.paragraphs)
        for nxt in secs[i + 1:]:
            if nxt.level <= s.level:
                break
            if nxt.heading:
                body.append(nxt.heading)
            body.extend(nxt.paragraphs)
        text_ = "\n".join(body).strip()
        if len(text_) < MIN_CHARS:
            continue
        if kind in found:
            found[kind].text += "\n" + text_
        else:
            found[kind] = Embedded(kind, s.heading or "", text_)
    return list(found.values())
