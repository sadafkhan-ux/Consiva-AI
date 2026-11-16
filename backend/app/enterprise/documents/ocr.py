"""OCR for scanned PDF pages: PyMuPDF renders a page to a PNG in memory, the Tesseract binary reads it. Optional at runtime: when the binary or PyMuPDF is missing, `available()` is
False and the caller records the pages as not read, with the reason. Nothing here ever raises into the crawl.

Tesseract is run as a subprocess with the image on stdin and the text on stdout (no pytesseract, no temporary files, no shell). The image comes from a renderer that never runs the
document's scripts. Bounds: a page cap per document, a time limit per page, a DPI that is lowered when the page is huge so that no rendered image exceeds MAX_PIXELS.
OCR output is machine-read and may be wrong: callers label it with detection_method `pdf_ocr`, and it is untrusted data like any other extracted text.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass, field
from functools import lru_cache

logger = logging.getLogger(__name__)

MAX_OCR_PAGES = 50           # pages OCR'd per document; the rest are reported as not read
OCR_PAGE_TIMEOUT_SECONDS = 60
OCR_DPI = 250                # 200-300 reads body text reliably without huge images
MAX_PIXELS = 40_000_000      # a rendered page larger than this is rendered at a lower DPI (a hostile MediaBox must not exhaust memory)
OCR_PREFERRED_LANGS = ("eng", "hin")
TESSERACT_BINARY = "tesseract"


@dataclass
class OcrResult:
    texts: dict[int, str] = field(default_factory=dict)      # page number -> recognised text (may be empty: a blank page)
    failed: dict[int, str] = field(default_factory=dict)     # page number -> why it could not be read


def _pymupdf():
    try:
        import pymupdf
        return pymupdf
    except Exception:                                         # not installed, or a broken native library
        return None


def unavailable_reason() -> str | None:
    """None when OCR can run, otherwise a plain sentence saying what is missing."""
    if shutil.which(TESSERACT_BINARY) is None:
        return "OCR is not available: the tesseract program is not installed"
    if _pymupdf() is None:
        return "OCR is not available: the PDF page renderer (PyMuPDF) is not installed"
    return None


def available() -> bool:
    return unavailable_reason() is None


@lru_cache(maxsize=1)
def _languages() -> str:
    """Preferred languages that are actually installed, joined for tesseract's -l flag. Falls back to English, which every install has."""
    try:
        out = subprocess.run([TESSERACT_BINARY, "--list-langs"], capture_output=True, text=True, timeout=15, check=False).stdout
        installed = {line.strip() for line in out.splitlines()[1:]}
    except Exception:
        installed = set()
    chosen = [lang for lang in OCR_PREFERRED_LANGS if lang in installed]
    return "+".join(chosen) if chosen else "eng"


def _render_png(page, dpi: int) -> bytes:
    rect = page.rect
    width_in, height_in = rect.width / 72.0, rect.height / 72.0
    pixels = (width_in * dpi) * (height_in * dpi)
    if pixels > MAX_PIXELS:
        dpi = max(50, int(dpi * (MAX_PIXELS / pixels) ** 0.5))
    zoom = dpi / 72.0
    pymupdf = _pymupdf()
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), colorspace=pymupdf.csGRAY, alpha=False)
    return pix.tobytes("png")


def _recognise(png: bytes) -> str:
    proc = subprocess.run([TESSERACT_BINARY, "stdin", "stdout", "-l", _languages(), "--psm", "3"], input=png, capture_output=True,
                          timeout=OCR_PAGE_TIMEOUT_SECONDS, check=False)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or b"tesseract failed").decode("utf-8", "replace").strip().splitlines()[-1][:200])
    return proc.stdout.decode("utf-8", "replace").strip()


def ocr_pdf_pages(data: bytes, page_numbers: list[int]) -> OcrResult:
    """OCR the given 1-based pages of a PDF. Pages beyond MAX_OCR_PAGES are not attempted and are listed as failed with that reason. Never raises."""
    result = OcrResult()
    reason = unavailable_reason()
    if reason:
        result.failed = {n: reason for n in page_numbers}
        return result
    pymupdf = _pymupdf()
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:
        result.failed = {n: f"the PDF could not be opened for OCR: {type(exc).__name__}" for n in page_numbers}
        return result
    try:
        if doc.needs_pass:
            doc.authenticate("")
        for index, number in enumerate(page_numbers):
            if index >= MAX_OCR_PAGES:
                result.failed[number] = f"not OCR'd: the limit is {MAX_OCR_PAGES} scanned pages per document"
                continue
            try:
                png = _render_png(doc[number - 1], OCR_DPI)
                result.texts[number] = _recognise(png)
            except subprocess.TimeoutExpired:
                result.failed[number] = f"OCR timed out after {OCR_PAGE_TIMEOUT_SECONDS} seconds"
            except Exception as exc:
                result.failed[number] = f"OCR failed: {str(exc)[:150] or type(exc).__name__}"
    finally:
        try:
            doc.close()
        except Exception:
            pass
    return result
