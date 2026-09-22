"""Reading a regulator's feed as a list of items rather than as a wall of text.

`rss` was in the connector vocabulary from the start and nothing implemented it, so a
source registered as a feed was fetched and flattened by the HTML path like any other
page. That mostly worked, which is the problem: the connector column said one thing
and the code did another, and the resulting diffs were far noisier than they needed
to be.

WHY A FEED DESERVES ITS OWN NORMALISATION
-----------------------------------------
Run an RSS document through an HTML text-stripper and you get every title, link,
description, build date and TTL value run together into one paragraph. A single new
item then shows up as a large reflowed diff, and the one fact that matters -- one item
was added -- is buried in it.

Parsed as a feed, each entry becomes exactly one line. A new advisory is one added
line. That is the difference between a change record a compliance reviewer can read
and one they have to decode.

Deliberately NOT a feed library. `xml.etree` is in the standard library, the shapes
here are RSS 2.0 and Atom, and a parser failure falls back to the HTML path rather
than failing the collection -- a feed we cannot parse is still a document we can
diff.
"""

from __future__ import annotations

import logging
from xml.etree import ElementTree

logger = logging.getLogger(__name__)

# Both vocabularies, since regulators use both. Atom namespaced, RSS usually not.
_ATOM = "{http://www.w3.org/2005/Atom}"

# Fields that change on every poll without the content changing. A feed that stamps
# lastBuildDate with "now" would otherwise report a regulatory change every time it is
# checked, which is the hourly-wolf-crying the HTML normaliser also guards against.
_VOLATILE = frozenset({
    "lastBuildDate", "pubDate", "ttl", "generator", "docs", "updated",
    f"{_ATOM}updated",
})


def looks_like_a_feed(raw: str) -> bool:
    """Cheap sniff before paying for a parse."""
    head = raw[:1000].lstrip().lower()
    return (
        head.startswith("<?xml")
        or "<rss" in head
        or "<feed" in head
        or "<rdf:rdf" in head
    )


def normalize(raw: str) -> str | None:
    """One line per entry, stable across polls. None if this is not a feed we can read.

    Returning None rather than raising: the caller falls back to the HTML path, so an
    unparseable feed degrades to a coarser diff instead of to a failed collection.
    """
    if not looks_like_a_feed(raw):
        return None
    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError as exc:
        logger.info("Source looked like a feed but did not parse; falling back: %s", exc)
        return None

    entries = root.findall(".//item") or root.findall(f".//{_ATOM}entry")
    if not entries:
        return None

    lines: list[str] = []
    for entry in entries:
        lines.append(_entry_line(entry))

    # Sorted so that a feed which reorders its items without changing them does not
    # read as a change. Reordering is a presentation decision by the publisher; the
    # set of items is the substance.
    return "\n".join(sorted(line for line in lines if line.strip()))


def _entry_line(entry) -> str:
    """Title, link and identity on one line, volatile fields left out."""
    title = _first_text(entry, ("title", f"{_ATOM}title"))
    link = _first_text(entry, ("link", "guid", f"{_ATOM}id"))
    if not link:
        # Atom puts the URL in an attribute rather than in text.
        anchor = entry.find(f"{_ATOM}link")
        if anchor is not None:
            link = anchor.get("href") or ""
    summary = _first_text(entry, ("description", f"{_ATOM}summary"))

    parts = [p for p in (title.strip(), link.strip()) if p]
    if summary.strip():
        # Truncated: a feed that inlines a whole article would otherwise make every
        # entry a paragraph, which is the wall of text this exists to avoid. The link
        # is on the line, so the full text is one click away.
        parts.append(summary.strip()[:200])
    return " | ".join(parts)


def _first_text(entry, names: tuple[str, ...]) -> str:
    for name in names:
        if name in _VOLATILE:
            continue
        node = entry.find(name)
        if node is not None and node.text:
            return " ".join(node.text.split())
    return ""
