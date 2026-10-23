"""Token-bounded chunking with overlap, per source page — kept simple (character-count
approximation of tokens) rather than pulling in a tokenizer dependency for Phase 1."""

import re
from dataclasses import dataclass, field

from app.rag.text_quality import is_readable_text

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
#
# The keyword is matched CASE-SENSITIVELY, which is the one signal plain-text extraction
# still carries once layout and font are gone, and it is what separates a real heading
# from a mid-sentence cross-reference. Measured over the whole ingested corpus (279
# chunks, 7 documents): all 15 capitalised matches are genuine headings ("CHAPTER II",
# "CHAPTER XIIA"), and all 21 lower-case ones are body text a PDF line-wrap had pushed to
# the start of a line -- "section 35;]", "section 28 were", "section 2 of the National
# Commission for Allied and Healthcare Professions Act, 2021", "rule 38-A, sub-rules (1)
# and (2) and sub-clauses (i)...". Those were reaching the customer-facing report as the
# cited DPDP reference for a finding, which is worse than citing nothing: a real-looking
# legal reference that does not support the claim. Requiring the capital drops all 21 and
# loses none of the 15 -- the "a missed section beats a wrong one" rule above, applied.
_SECTION_HEADING_RE = re.compile(
    r"(?m)^\s*((?:CHAPTER|Chapter|SECTION|Section|RULE|Rule)\s+[IVXLCDM\d]+[A-Za-z]?\b[^\n]{0,200}?)"
    r"(?=\(hereinafter|[.;]\s|[.;]$|\n|$)"
)

# A line repeated identically on most pages of a document is a running header/footer
# (title banner, page number, copyright line), not body text -- left in, it runs
# straight into whatever real heading follows it on the page (e.g. a title banner
# "...Act, 2023" immediately followed by "Section 7 (hereinafter...)" with no sentence
# break pypdf's plain-text extraction preserves), which is exactly what produced a
# `section` value like "DPDP Act 2023 2023 -- Section 7 (hereinafter referred to..."
# a duplicated "2023" and a run-on of two unrelated lines glued into one "heading".
_MIN_PAGES_FOR_BOILERPLATE_DETECTION = 3
_BOILERPLATE_LINE_FRACTION = 0.5

# A duplicated word pair ("2023 2023") is a near-universal pypdf artifact of the same
# repeated line being extracted immediately adjacent to itself (column/overlay text,
# or boilerplate detection above not catching a near-miss). Collapsed rather than left
# in, since a citation's `section` text is shown to a customer verbatim.
_DUPLICATE_WORD_RE = re.compile(r"\b(\w+)\s+\1\b", re.IGNORECASE)


def _strip_boilerplate_lines(pages: list[dict]) -> list[dict]:
    """Drops lines that repeat, verbatim, across at least half of a multi-page
    document's pages -- running headers/footers, not content. Skipped for short
    documents, where a genuinely repeated short phrase (a defined term, a cross-
    reference) is far more likely than a page banner."""
    if len(pages) < _MIN_PAGES_FOR_BOILERPLATE_DETECTION:
        return pages

    counts: dict[str, int] = {}
    for page in pages:
        for line in {ln.strip() for ln in page["text"].splitlines() if ln.strip()}:
            counts[line] = counts.get(line, 0) + 1

    threshold = len(pages) * _BOILERPLATE_LINE_FRACTION
    boilerplate = {line for line, count in counts.items() if count >= threshold}
    if not boilerplate:
        return pages

    return [
        {**page, "text": "\n".join(
            ln for ln in page["text"].splitlines() if ln.strip() not in boilerplate
        )}
        for page in pages
    ]


def _clean_heading(text: str) -> str:
    return _DUPLICATE_WORD_RE.sub(r"\1", text).strip(" -–—")


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
        best = _clean_heading(match.group(1))
    return best


def chunk_pages(pages: list[dict], *, max_chars: int = 1500, overlap_chars: int = 200) -> list[Chunk]:
    """`pages`: [{"page_number": int, "text": str}, ...] from a loader. Chunks never
    span multiple pages, so each chunk's `metadata.page_number` is unambiguous.
    `metadata.section` is a best-effort heading match — None when nothing was
    confidently detected, never guessed."""
    pages = _strip_boilerplate_lines(pages)
    chunks: list[Chunk] = []
    for page in pages:
        stripped = page["text"].strip()
        for piece, offset in _split_with_overlap(page["text"], max_chars, overlap_chars):
            if not is_readable_text(piece):
                continue   # legacy-encoded Hindi pages extract as garbage: see rag/text_quality.py
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
