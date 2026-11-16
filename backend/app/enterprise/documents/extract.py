"""Turning a fetched document into per-page text with an honest extraction status.

`extract` reads the document (readers.py), runs OCR on the PDF pages that have no text layer when OCR is available (ocr.py), and returns one record: the status, the method, and
every page's text with the method that produced it. The statuses are the ones migration 0055 allows on ent_frontier.extraction_status:

  extracted        every page had a text layer (or the DOCX was read whole)
  ocr_extracted    the scanned pages were read by OCR (machine-read: their pages carry detection_method `pdf_ocr`)
  partly_extracted some pages could not be read (OCR missing, failed, over the page limit; or a DOCX with no text)
  unreadable       corrupt or not the format it claims
  encrypted        password-protected
  unsupported      a format that is not read (DOC, XLS, PPT, ODT, RTF...)
  too_large        over the size limit (set by the caller, which refuses the file before reading it)

This module never marks a document analysed: analysis is another stage's job. All extracted text is untrusted data.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.enterprise.documents import ocr, readers
from app.enterprise.models import EntFrontier

EXTRACTION_STATUSES = ("extracted", "ocr_extracted", "partly_extracted", "unreadable", "encrypted", "unsupported", "too_large")


@dataclass
class PageText:
    number: int          # 1-based for a PDF page, 0 for an unpaged DOCX
    text: str
    method: str          # pdf_text | pdf_ocr | docx_text  (becomes the evidence detection_method)


@dataclass
class Extraction:
    status: str
    method: str | None
    pages: list[PageText] = field(default_factory=list)
    reason: str | None = None       # why the document is not fully read; None when it is

    @property
    def page_count(self) -> int:
        return len(self.pages)


def _page_list(numbers: list[int], limit: int = 12) -> str:
    shown = ", ".join(map(str, numbers[:limit]))
    return shown + (f" and {len(numbers) - limit} more" if len(numbers) > limit else "")


def extract(data: bytes, url: str, content_type: str | None, *, ocr_pages=None) -> Extraction:
    """Blocking (PDF parsing and OCR): call it from a worker thread. Never raises for a bad document; the outcome is the returned status."""
    try:
        doc = readers.read_document(data, url, content_type)
    except readers.DocumentEncrypted as exc:
        return Extraction("encrypted", None, reason=f"{exc}; the scanner has no password")
    except readers.DocumentUnsupported as exc:
        return Extraction("unsupported", None, reason=str(exc))
    except readers.DocumentUnreadable as exc:
        return Extraction("unreadable", None, reason=f"document could not be read: {exc}")

    if doc.kind == "docx":
        text = doc.pages[0][1] if doc.pages else ""
        if text.strip():
            return Extraction("extracted", "docx_text", [PageText(0, text, "docx_text")])
        return Extraction("partly_extracted", "docx_text", [], "the DOCX holds no text (it may contain only images, which are not read)")

    pages = [PageText(n, t, "pdf_text") for n, t in doc.pages if t]
    problems: list[str] = []
    used_ocr = False
    if doc.needs_ocr:
        run = ocr_pages or ocr.ocr_pdf_pages
        result = run(data, list(doc.needs_ocr))
        for number, text in sorted(result.texts.items()):
            used_ocr = True
            if text.strip():
                pages.append(PageText(number, text, "pdf_ocr"))
        pages.sort(key=lambda p: p.number)
        if result.failed:
            failed = sorted(result.failed)
            reasons = sorted(set(result.failed.values()))
            problems.append(f"page(s) {_page_list(failed)} could not be read ({'; '.join(reasons)[:300]})")
        blank = sorted(n for n, t in result.texts.items() if not t.strip())
        if blank:
            problems_note = f"OCR found no text on page(s) {_page_list(blank)}"
        else:
            problems_note = None
    else:
        problems_note = None
    if doc.skipped_pages:
        problems.append(f"{doc.skipped_pages} page(s) after page {readers.MAX_PDF_PAGES} were not read (page limit)")

    had_text = any(p.method == "pdf_text" for p in pages)
    if used_ocr and had_text:
        method = "pdf_text+pdf_ocr"
    elif used_ocr:
        method = "pdf_ocr"
    else:
        method = "pdf_text" if had_text else None       # nothing was read at all (scanned, and OCR missing): no method to claim
    if problems:
        return Extraction("partly_extracted", method, pages, "; ".join(problems + ([problems_note] if problems_note else [])))
    if used_ocr:
        return Extraction("ocr_extracted", method, pages, problems_note)
    return Extraction("extracted", method, pages)


async def set_extraction(db: AsyncSession, row_id: uuid.UUID, status: str, method: str | None) -> None:
    """Record how a document was read on its frontier row. Touches only the two extraction columns: never the crawl status and never analysis_status."""
    if status not in EXTRACTION_STATUSES:
        raise ValueError(f"unknown extraction status {status!r}")
    await db.execute(update(EntFrontier).where(EntFrontier.id == row_id).values(extraction_status=status, extraction_method=method, updated_at=func.now()))
