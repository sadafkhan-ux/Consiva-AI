"""Reading linked documents: PDF page by page, DOCX body, tables, headers and footers. Pure functions on bytes: no network, no database, so they are tested offline.

Each PDF page is returned with its 1-based number, so a finding can cite "page 7". A page with no extractable text (a scanned image) is returned with empty text and listed in
`needs_ocr`; documents/extract.py runs OCR on those pages when it is available, and the report must say so for any page that stays unread. A DOCX has no fixed pages, so its text is
returned as one unpaged text (page_number 0). Formats that are not read (DOC, XLS, PPT, ODT, RTF, XLSX, PPTX) raise DocumentUnsupported: nothing is pretended.
Nothing is executed: a PDF's scripts and a DOCX's macros are never run, and all extracted text is untrusted data.
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass, field

from lxml import etree
from pypdf import PdfReader

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
MAX_DOCX_XML_BYTES = 50_000_000   # an uncompressed word/document.xml larger than this is refused (a zip bomb)
MAX_DOCX_TOTAL_BYTES = 80_000_000  # all the XML parts read from one DOCX together
MAX_PDF_PAGES = 500               # pages read per PDF; later pages are counted in `skipped_pages`, never silently dropped
OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"   # legacy DOC/XLS/PPT, and password-protected OOXML
UNSUPPORTED_EXTENSIONS = {".doc": "DOC", ".xls": "XLS", ".ppt": "PPT", ".odt": "ODT", ".ods": "ODS", ".odp": "ODP", ".rtf": "RTF", ".xlsx": "XLSX", ".pptx": "PPTX"}


class DocumentUnreadable(Exception):
    """The document cannot be read at all (corrupt, not the format it claims). The reason goes into the coverage record."""


class DocumentEncrypted(DocumentUnreadable):
    """Password-protected: the scanner does not have the password and never guesses one."""


class DocumentUnsupported(DocumentUnreadable):
    """A real document in a format this reader does not handle (DOC, XLS, PPT, ODT, RTF...). Recorded as unsupported, never as empty."""


@dataclass
class DocumentText:
    kind: str                                   # "pdf" | "docx"
    pages: list[tuple[int, str]] = field(default_factory=list)   # (page_number, text); page_number 0 = unpaged
    needs_ocr: list[int] = field(default_factory=list)           # PDF pages with no extractable text
    skipped_pages: int = 0                      # pages after MAX_PDF_PAGES that were not read at all

    @property
    def page_count(self) -> int:
        return len(self.pages)

    @property
    def text(self) -> str:
        return "\n\n".join(t for _, t in self.pages if t)


def read_pdf(data: bytes) -> DocumentText:
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            try:
                opened = reader.decrypt("")          # many PDFs are "encrypted" with an empty user password and open for anyone
            except Exception:
                opened = 0
            if not opened:
                raise DocumentEncrypted("the PDF is password-protected")
        out = DocumentText(kind="pdf")
        total = len(reader.pages)
        for number, page in enumerate(reader.pages, start=1):
            if number > MAX_PDF_PAGES:
                out.skipped_pages = total - MAX_PDF_PAGES
                break
            try:
                text = (page.extract_text() or "").strip()
            except Exception:                     # one bad page must not lose the others
                text = ""
            out.pages.append((number, text))
            if not text:
                out.needs_ocr.append(number)
        return out
    except DocumentUnreadable:
        raise
    except Exception as exc:                      # pypdf raises many types for damaged files
        raise DocumentUnreadable(f"not a readable PDF: {type(exc).__name__}: {str(exc)[:150]}") from exc


def _q(tag: str) -> str:
    return f"{{{W_NS}}}{tag}"


def _paragraph_text(p) -> str:
    return "".join(t.text or "" for t in p.iter(_q("t"))).strip()


def _lines(parent) -> list[str]:
    """Text lines of a body, in document order: paragraphs, and table rows with their cells joined by ' | '. Content controls (w:sdt) are looked into."""
    lines: list[str] = []
    for child in parent:
        if child.tag == _q("p"):
            if text := _paragraph_text(child):
                lines.append(text)
        elif child.tag == _q("tbl"):
            for row in child.iter(_q("tr")):
                cells = []
                for cell in row.findall(_q("tc")):
                    cell_text = " ".join(_lines(cell))
                    if cell_text:
                        cells.append(cell_text)
                if cells:
                    lines.append(" | ".join(cells))
        elif child.tag == _q("sdt"):
            content = child.find(_q("sdtContent"))
            if content is not None:
                lines.extend(_lines(content))
        elif child.tag in (_q("sdtContent"), _q("body")):
            lines.extend(_lines(child))
    return lines


_EXTRA_PARTS = re.compile(r"^word/(header\d*|footer\d*|footnotes|endnotes)\.xml$")


def _parse(xml: bytes):
    return etree.fromstring(xml, parser=etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False))


def read_docx(data: bytes) -> DocumentText:
    extra_xml: list[bytes] = []
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            info = z.getinfo("word/document.xml")
            if info.file_size > MAX_DOCX_XML_BYTES:
                raise DocumentUnreadable("the document body is too large to read safely")
            total = info.file_size
            xml = z.read("word/document.xml")
            for name in sorted(n for n in z.namelist() if _EXTRA_PARTS.match(n)):
                size = z.getinfo(name).file_size
                total += size
                if size > MAX_DOCX_XML_BYTES or total > MAX_DOCX_TOTAL_BYTES:
                    raise DocumentUnreadable("the document is too large to read safely")
                extra_xml.append(z.read(name))
    except DocumentUnreadable:
        raise
    except (zipfile.BadZipFile, KeyError, OSError) as exc:
        raise DocumentUnreadable(f"not a readable DOCX: {exc}") from exc
    try:
        root = _parse(xml)
    except etree.XMLSyntaxError as exc:
        raise DocumentUnreadable(f"the DOCX body is not valid XML: {exc}") from exc
    lines = _lines(root)
    seen = set(lines)
    for part in extra_xml:                         # headers, footers, notes: where a company name, date or officer contact often lives
        try:
            for line in _lines(_parse(part)):
                if line not in seen:
                    seen.add(line)
                    lines.append(line)
        except etree.XMLSyntaxError:
            continue                               # one broken header must not lose the body
    out = DocumentText(kind="docx")
    out.pages.append((0, "\n".join(lines)))
    return out


def _unsupported_from_zip(data: bytes) -> str | None:
    """Name the format of a zip that is not a DOCX (XLSX, PPTX, ODT...), or None when it is a DOCX or not a readable zip."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = set(z.namelist())
            if "word/document.xml" in names:
                return None
            if "xl/workbook.xml" in names:
                return "XLSX"
            if "ppt/presentation.xml" in names:
                return "PPTX"
            if "mimetype" in names:
                try:
                    mime = z.read("mimetype")[:100].decode("ascii", "ignore")
                except Exception:
                    mime = ""
                if "opendocument" in mime:
                    return "OpenDocument"
    except Exception:
        return None
    return None


def read_document(data: bytes, url: str, content_type: str | None) -> DocumentText:
    """Choose the reader from the bytes themselves where possible (a PDF starts with %PDF, a DOCX is a zip), then from the address and the content type."""
    ctype = (content_type or "").lower()
    path = url.lower().split("?")[0].split("#")[0]
    if data[:5] == b"%PDF-":
        return read_pdf(data)
    if data[:8] == OLE_MAGIC:
        raise DocumentUnsupported("this is a legacy Office file (DOC, XLS or PPT) or a password-protected Office file; it is not read")
    if data[:5] == b"{\\rtf":
        raise DocumentUnsupported("RTF documents are not read")
    if data[:2] == b"PK":
        if (other := _unsupported_from_zip(data)) is not None:
            raise DocumentUnsupported(f"{other} documents are not read (only PDF and DOCX are)")
        return read_docx(data)
    if "pdf" in ctype or path.endswith(".pdf"):             # no recognisable header: the claim is all there is, and a damaged file is reported as unreadable
        return read_pdf(data)
    if "wordprocessingml" in ctype or path.endswith(".docx"):
        return read_docx(data)
    for ext, label in UNSUPPORTED_EXTENSIONS.items():
        if path.endswith(ext):
            raise DocumentUnsupported(f"{label} documents are not read (only PDF and DOCX are)")
    raise DocumentUnsupported("this document format is not supported yet (only PDF and DOCX are read)")
