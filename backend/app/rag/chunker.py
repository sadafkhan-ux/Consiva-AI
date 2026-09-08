"""Token-bounded chunking with overlap, per source page — kept simple (character-count
approximation of tokens) rather than pulling in a tokenizer dependency for Phase 1."""

import re
from dataclasses import dataclass, field

_CHARS_PER_TOKEN_ESTIMATE = 4

# Conservative on purpose: only explicit "Section"/"Rule"/"Chapter"-prefixed headings are
# matched, never a bare numbered line — a missed section beats a wrong one (master
# prompt's "never fabricate" principle applies to citations too).
#
# The capture stops at the next sentence-ending punctuation (or newline) rather than a
# flat character count: a fixed-width cutoff was found, live, to slice a real match off
# mid-word/mid-clause (e.g. "...shall come into" instead of "...shall come into force.")
# when a PDF line-wrap happened to put a body-text phrase like "section 6 and clause..."
# at the start of a line -- this doesn't fix that false-positive match itself (this
# module still can't tell a true heading from a mid-sentence section reference without
# layout/font info the plain-text extraction has already lost), but it stops the
# captured snippet from being truncated into a garbled, misleading fragment either way.
# 200 chars is a fallback ceiling, not the normal case -- most sentences end well before it.
_SECTION_HEADING_RE = re.compile(
    r"(?im)^\s*((?:chapter|section|rule)\s+[ivxlcdm\d]+[a-z]?\b[^\n]{0,200}?)(?=[.;]\s|[.;]$|\n|$)"
)


@dataclass
class Chunk:
    content: str
    chunk_index: int
    token_count: int
    metadata: dict = field(default_factory=dict)


def _split_with_overlap(text: str, max_chars: int, overlap_chars: int) -> list[tuple[str, int]]:
    """Returns [(piece, start_offset_in_text), ...]."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [(text, 0)]

    pieces = []
    start = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        if end < len(text):
            last_space = text.rfind(" ", start, end)
            if last_space > start:
                end = last_space
        piece = text[start:end].strip()
        if piece:
            pieces.append((piece, start))
        if end >= len(text):
            break
        start = max(end - overlap_chars, start + 1)
    return pieces


def _nearest_heading(text: str, upto_offset: int) -> str | None:
    """Last Section/Rule/Chapter heading at or before `upto_offset` in `text`."""
    best: str | None = None
    for match in _SECTION_HEADING_RE.finditer(text):
        if match.start() > upto_offset:
            break
        best = match.group(1).strip()
    return best


def chunk_pages(pages: list[dict], *, max_chars: int = 1500, overlap_chars: int = 200) -> list[Chunk]:
    """`pages`: [{"page_number": int, "text": str}, ...] from a loader. Chunks never
    span multiple pages, so each chunk's `metadata.page_number` is unambiguous.
    `metadata.section` is a best-effort heading match — None when nothing was
    confidently detected, never guessed."""
    chunks: list[Chunk] = []
    for page in pages:
        stripped = page["text"].strip()
        for piece, offset in _split_with_overlap(page["text"], max_chars, overlap_chars):
            chunks.append(Chunk(
                content=piece,
                chunk_index=len(chunks),
                token_count=len(piece) // _CHARS_PER_TOKEN_ESTIMATE,
                metadata={
                    "page_number": page["page_number"],
                    "section": _nearest_heading(stripped, offset + len(piece)),
                },
            ))
    return chunks
