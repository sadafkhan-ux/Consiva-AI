"""The evidence store: raw captured content, kept with a hash and where it came from, so every later finding can point at its exact source.

Content is stored once per (scan, kind, source, page, hash): capturing the same text from the same place twice returns the first record, so no duplicate evidence and therefore no
duplicate finding built on it. Content larger than the configured limit is refused with an error, never silently cut, because a finding built on a truncated passage would be a
finding built on something the page did not say.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.enterprise.config import get_enterprise_settings
from app.enterprise.models import EVIDENCE_KINDS, EntEvidence


class EvidenceTooLarge(ValueError):
    """The content is larger than ENTERPRISE_MAX_EVIDENCE_BYTES. It is refused whole; the caller records that the source was too large."""


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Stored:
    id: uuid.UUID
    content_hash: str
    is_new: bool


async def store(
    db: AsyncSession, scan_id: uuid.UUID, *, kind: str, source_url: str, content: str, detection_method: str,
    page_number: int = 0, frontier_id: uuid.UUID | None = None, content_type: str | None = None,
) -> Stored:
    """Keep one piece of evidence. `page_number` is 1-based for a document page and 0 for a web page. Returns the record and whether it was new."""
    if kind not in EVIDENCE_KINDS:
        raise ValueError(f"unknown evidence kind {kind!r}")
    if page_number < 0:
        raise ValueError("page_number is 1-based, or 0 for a source with no pages")
    if not source_url or not detection_method:
        raise ValueError("evidence always names its source and how it was detected")
    # A NUL character cannot be stored in a Postgres text column; text extracted from some PDFs contains them (two documents on swaransoft.com failed this way in the 19 Nov 2026
    # end-to-end scan). It carries no meaning in text, so it is removed before the content is hashed and stored.
    content = content.replace("\x00", "")
    size = len(content.encode("utf-8"))
    limit = get_enterprise_settings().max_evidence_bytes
    if size > limit:
        raise EvidenceTooLarge(f"{size} bytes exceeds the {limit} byte evidence limit")

    digest = content_hash(content)
    inserted = await db.scalar(
        pg_insert(EntEvidence)
        .values(
            scan_id=scan_id, frontier_id=frontier_id, kind=kind, source_url=source_url, page_number=page_number, content_hash=digest,
            content_type=content_type, byte_size=size, content=content, detection_method=detection_method)
        .on_conflict_do_nothing(constraint="ent_evidence_unique")
        .returning(EntEvidence.id))
    if inserted is not None:
        return Stored(id=inserted, content_hash=digest, is_new=True)
    existing = await db.scalar(select(EntEvidence.id).where(
        EntEvidence.scan_id == scan_id, EntEvidence.kind == kind, EntEvidence.source_url == source_url,
        EntEvidence.page_number == page_number, EntEvidence.content_hash == digest))
    return Stored(id=existing, content_hash=digest, is_new=False)


def split_for_storage(content: str, *, limit_bytes: int | None = None) -> list[str]:
    """Cut a text that is over the evidence limit into parts that each fit, breaking at a paragraph, a line, a sentence or a space (never in the middle of a word when a break exists nearby). The
    parts, joined in order, are exactly the text: nothing is dropped."""
    limit = limit_bytes or get_enterprise_settings().max_evidence_bytes
    parts: list[str] = []
    rest = content
    while rest:
        if len(rest.encode("utf-8")) <= limit:
            parts.append(rest)
            break
        n = min(len(rest), limit)
        while len(rest[:n].encode("utf-8")) > limit:
            n = int(n * 0.9)
        piece = rest[:n]
        cut = max(piece.rfind("\n\n"), piece.rfind("\n"), piece.rfind(". "), piece.rfind(" "))
        cut = n if cut < n * 0.5 else cut + 1
        parts.append(rest[:cut])
        rest = rest[cut:]
    return parts


async def get(db: AsyncSession, evidence_id: uuid.UUID) -> EntEvidence | None:
    return await db.get(EntEvidence, evidence_id)
