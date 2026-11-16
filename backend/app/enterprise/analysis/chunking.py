"""Splitting a long source into chunks that fit one model call, without losing or inventing a character.

A chunk is always a verbatim slice of the source (`text[text_start:end]`), so a quote taken from a chunk is a quote from the source. Chunks are cut at the boundaries a reader would use:
a heading or a paragraph first, then a sentence, and only for a single run of text longer than the budget at a word boundary. Each chunk after the first starts `overlap` characters
early (at a word boundary) so a sentence cut by a boundary is whole in one of the two chunks. `start`..`end` are the characters a chunk is responsible for: they tile the source exactly,
with no gap and no overlap, which is what the no-loss test checks. Nothing is truncated: a source of any length becomes as many chunks as it needs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.enterprise.analysis import constants as K

_PARA = re.compile(r"\n[ \t]*\n+")
_SENT = re.compile(r"(?<=[.!?;:])\s+")


@dataclass(frozen=True)
class Chunk:
    index: int
    count: int
    start: int          # the first character this chunk is responsible for
    end: int            # one past the last
    text_start: int     # where the text sent to the model begins (start minus the overlap)
    text: str           # source[text_start:end]


def _is_heading(piece: str) -> bool:
    p = piece.strip()
    return bool(p) and len(p) <= 90 and "\n" not in p and not p.endswith((".", ",", ";")) and bool(re.search(r"[A-Za-z]", p))


def _units(text: str, budget: int) -> list[tuple[int, int, bool]]:
    """(start, end, is_heading) pieces that tile `text`, none longer than `budget`."""
    pieces: list[tuple[int, int, bool]] = []
    pos = 0
    for m in _PARA.finditer(text):
        pieces.append((pos, m.end(), _is_heading(text[pos:m.start()])))
        pos = m.end()
    if pos < len(text):
        pieces.append((pos, len(text), _is_heading(text[pos:])))
    out: list[tuple[int, int, bool]] = []
    for s, e, h in pieces:
        if e - s <= budget:
            out.append((s, e, h))
            continue
        cut = s
        for m in _SENT.finditer(text, s, e):                          # a long paragraph: by sentence
            if m.end() - cut > budget and m.start() > cut:
                out.append((cut, m.start() + 0, False))
                cut = m.start()
        out.append((cut, e, False))
    final: list[tuple[int, int, bool]] = []
    for s, e, h in out:                                               # a single run still too long: at word boundaries, then hard
        while e - s > budget:
            k = text.rfind(" ", s + budget // 2, s + budget)
            k = s + budget if k == -1 else k + 1
            final.append((s, k, False))
            s = k
        final.append((s, e, h))
    return final


def _word_start(text: str, pos: int) -> int:
    """The nearest word start at or after `pos`."""
    if pos <= 0:
        return 0
    while pos < len(text) and not text[pos - 1].isspace():
        pos += 1
    return pos


def chunk_text(text: str, *, max_chars: int | None = None, overlap: int | None = None) -> list[Chunk]:
    """Split `text` into chunks of at most about `max_chars` characters of their own (plus the overlap). Empty text gives no chunk."""
    if not text or not text.strip():
        return []
    max_chars = K.CHUNK_MAX_CHARS if max_chars is None else max_chars
    overlap = K.CHUNK_OVERLAP_CHARS if overlap is None else overlap
    budget = max(200, max_chars - overlap)
    ranges: list[tuple[int, int]] = []
    cur_s = cur_e = None
    for s, e, heading in _units(text, budget):
        if cur_s is None:
            cur_s, cur_e = s, e
        elif e - cur_s <= budget and not (heading and cur_e - cur_s >= budget // 2):
            cur_e = e
        else:
            ranges.append((cur_s, cur_e))
            cur_s, cur_e = s, e
    ranges.append((cur_s, cur_e))
    n = len(ranges)
    chunks = []
    for i, (s, e) in enumerate(ranges):
        ts = 0 if i == 0 else min(_word_start(text, max(0, s - overlap)), s)
        chunks.append(Chunk(i, n, s, e, ts, text[ts:e]))
    return chunks
