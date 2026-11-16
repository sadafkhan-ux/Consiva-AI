"""Document extraction with OCR: statuses, methods and limits. No network and no tesseract needed: OCR is a mock, except one real test that skips when the binary is absent."""
import asyncio
import io
import types
import uuid
import zipfile

import pytest
from pypdf import PdfReader, PdfWriter

from app.enterprise.crawl import runner
from app.enterprise.documents import extract, ocr, readers
from tests.enterprise.test_enterprise_documents import make_docx, make_pdf

URL = "https://a.test/policy.pdf"
REAL_EXTRACT = extract.extract      # captured before any test patches it


def fake_ocr(texts: dict[int, str] | None = None, failed: dict[int, str] | None = None, calls: list | None = None):
    def run(data, pages):
        if calls is not None:
            calls.append(list(pages))
        return ocr.OcrResult(texts={n: t for n, t in (texts or {}).items() if n in pages}, failed=dict(failed or {}))
    return run


def encrypted_pdf() -> bytes:
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(make_pdf(["secret"]))))
    writer.encrypt("pw", algorithm="AES-256")
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def docx_with_parts(body: str, **parts: str) -> bytes:
    ns = f'xmlns:w="{readers.W_NS}"'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", f'<?xml version="1.0"?><w:document {ns}><w:body>{body}</w:body></w:document>')
        for name, xml in parts.items():
            z.writestr(f"word/{name}.xml", f'<?xml version="1.0"?><w:hdr {ns}>{xml}</w:hdr>')
    return buf.getvalue()


def p(text: str) -> str:
    return f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"


def test_a_text_pdf_is_extracted_without_calling_ocr():
    calls: list = []
    r = extract.extract(make_pdf(["We collect email", "Retention one year"]), URL, "application/pdf", ocr_pages=fake_ocr(calls=calls))
    assert (r.status, r.method, r.reason) == ("extracted", "pdf_text", None)
    assert [(pg.number, pg.method) for pg in r.pages] == [(1, "pdf_text"), (2, "pdf_text")]
    assert calls == []


def test_a_mixed_pdf_runs_ocr_only_for_the_empty_pages():
    calls: list = []
    r = extract.extract(make_pdf(["Notice text", "", "Grievance officer", ""]), URL, None, ocr_pages=fake_ocr({2: "scanned two", 4: "scanned four"}, calls=calls))
    assert calls == [[2, 4]]
    assert (r.status, r.method) == ("ocr_extracted", "pdf_text+pdf_ocr")
    assert [(pg.number, pg.method) for pg in r.pages] == [(1, "pdf_text"), (2, "pdf_ocr"), (3, "pdf_text"), (4, "pdf_ocr")]


def test_an_all_scanned_pdf_with_ocr_available_is_ocr_extracted():
    r = extract.extract(make_pdf(["", ""]), URL, None, ocr_pages=fake_ocr({1: "page one", 2: "page two"}))
    assert (r.status, r.method) == ("ocr_extracted", "pdf_ocr")
    assert all(pg.method == "pdf_ocr" for pg in r.pages) and len(r.pages) == 2


def test_ocr_unavailable_gives_partly_extracted_with_the_reason(monkeypatch):
    monkeypatch.setattr(ocr, "unavailable_reason", lambda: "OCR is not available: the tesseract program is not installed")
    r = extract.extract(make_pdf(["Text page", ""]), URL, None)       # the real ocr function, which must not crash
    assert r.status == "partly_extracted" and r.method == "pdf_text"
    assert "page(s) 2" in r.reason and "tesseract" in r.reason
    assert [pg.number for pg in r.pages] == [1]


def test_some_pages_failing_ocr_is_partly_extracted():
    r = extract.extract(make_pdf(["", ""]), URL, None, ocr_pages=fake_ocr({1: "ok"}, failed={2: "OCR timed out after 60 seconds"}))
    assert r.status == "partly_extracted" and r.method == "pdf_ocr" and "timed out" in r.reason


def test_a_scanned_pdf_with_ocr_missing_claims_no_method(monkeypatch):
    monkeypatch.setattr(ocr, "unavailable_reason", lambda: "OCR is not available: the tesseract program is not installed")
    r = extract.extract(make_pdf(["", ""]), URL, None)
    assert (r.status, r.method, r.pages) == ("partly_extracted", None, [])


def test_an_encrypted_pdf_is_encrypted():
    r = extract.extract(encrypted_pdf(), URL, None, ocr_pages=fake_ocr())
    assert r.status == "encrypted" and "password" in r.reason and r.pages == []


@pytest.mark.parametrize("data", [b"", b"%PDF-1.4 garbage", b"\x00\x01 not a document"])
def test_corrupted_bytes_are_unreadable(data):
    r = extract.extract(data, URL, "application/pdf")
    assert r.status == "unreadable" and r.reason


def test_a_docx_table_header_and_footer_are_read():
    table = "<w:tbl><w:tr><w:tc>" + p("Purpose") + "</w:tc><w:tc>" + p("Retention") + "</w:tc></w:tr><w:tr><w:tc>" + p("Billing") + "</w:tc><w:tc>" + p("7 years") + "</w:tc></w:tr></w:tbl>"
    data = docx_with_parts(p("Policy") + table, header1=p("Acme Pvt Ltd"), footer1=p("grievance@acme.test"))
    r = extract.extract(data, "https://a.test/t.docx", None)
    assert (r.status, r.method) == ("extracted", "docx_text")
    text = r.pages[0].text
    assert "Purpose | Retention" in text and "Billing | 7 years" in text
    assert "Acme Pvt Ltd" in text and "grievance@acme.test" in text
    assert text.index("Policy") < text.index("Billing")


def test_a_docx_with_no_text_is_partly_extracted():
    r = extract.extract(make_docx([]), "https://a.test/t.docx", None)
    assert r.status == "partly_extracted" and "images" in r.reason


@pytest.mark.parametrize("data,url", [
    (readers.OLE_MAGIC + b"\x00" * 50, "https://a.test/policy.doc"),
    (b"{\\rtf1 hello}", "https://a.test/policy.rtf"),
    (b"anything", "https://a.test/sheet.xls"),
    (b"anything", "https://a.test/slides.ppt"),
])
def test_other_office_formats_are_unsupported_with_a_reason(data, url):
    r = extract.extract(data, url, None)
    assert r.status == "unsupported" and r.reason and r.pages == []


def test_an_xlsx_and_an_odt_zip_are_unsupported_not_unreadable():
    for name in ("xl/workbook.xml", "ppt/presentation.xml"):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr(name, "<a/>")
        assert extract.extract(buf.getvalue(), "https://a.test/x", None).status == "unsupported"


def test_the_pdf_page_cap_is_reported_not_silent(monkeypatch):
    monkeypatch.setattr(readers, "MAX_PDF_PAGES", 2)
    r = extract.extract(make_pdf(["a", "b", "c", "d"]), URL, None)
    assert r.status == "partly_extracted" and [pg.number for pg in r.pages] == [1, 2] and "2 page(s) after page 2" in r.reason


def test_the_ocr_page_cap_marks_the_rest_as_failed(monkeypatch):
    monkeypatch.setattr(ocr, "unavailable_reason", lambda: None)
    monkeypatch.setattr(ocr, "MAX_OCR_PAGES", 1)
    monkeypatch.setattr(ocr, "_pymupdf", lambda: types.SimpleNamespace(open=lambda **kw: _FakeDoc()))
    monkeypatch.setattr(ocr, "_render_png", lambda page, dpi: b"png")
    monkeypatch.setattr(ocr, "_recognise", lambda png: "text")
    res = ocr.ocr_pdf_pages(b"x", [1, 2, 3])
    assert res.texts == {1: "text"} and set(res.failed) == {2, 3} and "limit" in res.failed[2]


def test_an_ocr_timeout_fails_that_page_only(monkeypatch):
    import subprocess
    monkeypatch.setattr(ocr, "unavailable_reason", lambda: None)
    monkeypatch.setattr(ocr, "_pymupdf", lambda: types.SimpleNamespace(open=lambda **kw: _FakeDoc()))
    monkeypatch.setattr(ocr, "_render_png", lambda page, dpi: b"png")

    def recognise(png):
        if recognise.n == 0:
            recognise.n += 1
            raise subprocess.TimeoutExpired("tesseract", 60)
        return "fine"
    recognise.n = 0
    monkeypatch.setattr(ocr, "_recognise", recognise)
    res = ocr.ocr_pdf_pages(b"x", [1, 2])
    assert "timed out" in res.failed[1] and res.texts == {2: "fine"}


class _FakeDoc:
    needs_pass = False

    def __getitem__(self, i):
        return object()

    def close(self):
        pass


@pytest.mark.skipif(not ocr.available(), reason="tesseract and PyMuPDF are not both installed")
def test_real_ocr_reads_a_scanned_pdf():
    import pymupdf
    src = pymupdf.open()
    src.new_page(width=400, height=200).insert_text((30, 100), "Grievance Officer contact", fontsize=28)
    png = src[0].get_pixmap(dpi=200).tobytes("png")
    scan = pymupdf.open()
    scan.new_page(width=400, height=200).insert_image(pymupdf.Rect(0, 0, 400, 200), stream=png)
    r = extract.extract(scan.tobytes(), URL, "application/pdf")
    assert r.status == "ocr_extracted" and r.method == "pdf_ocr" and r.pages[0].method == "pdf_ocr"
    assert "grievance" in r.pages[0].text.lower()


# ---- _process_document: what it records -------------------------------------------------------------------------------------------------------------------------------


class Recorder:
    def __init__(self):
        self.stored, self.done, self.extraction, self.excluded, self.failed = [], [], [], [], []
        self.commits = 0


def drive(monkeypatch, data: bytes, *, content_type="application/pdf", fetch_error=None, ocr_run=None):
    rec = Recorder()

    async def fetch(*a, **k):
        if fetch_error:
            raise fetch_error
        return data, content_type, "https://a.test/policy.pdf"

    async def store(db, scan_id, **kw):
        rec.stored.append(kw)

    async def mark_done(db, row_id, **kw):
        rec.done.append(kw)

    async def mark_excluded(db, row_id, reason):
        rec.excluded.append(reason)

    async def mark_failed(db, row_id, error, **kw):
        rec.failed.append(error)
        return "failed"

    async def set_extraction(db, row_id, status, method):
        rec.extraction.append((status, method))

    class Db:
        async def commit(self):
            rec.commits += 1

    monkeypatch.setattr(runner.doc_fetch, "fetch", fetch)
    monkeypatch.setattr(runner.evidence, "store", store)
    monkeypatch.setattr(runner.frontier, "mark_done", mark_done)
    monkeypatch.setattr(runner.frontier, "mark_excluded", mark_excluded)
    monkeypatch.setattr(runner.frontier, "mark_failed", mark_failed)
    monkeypatch.setattr(runner.doc_extract, "set_extraction", set_extraction)
    monkeypatch.setattr(runner.doc_extract, "extract", lambda d, u, c: REAL_EXTRACT(d, u, c, ocr_pages=ocr_run or fake_ocr()))
    claimed = types.SimpleNamespace(id=uuid.uuid4(), url="https://a.test/policy.pdf", depth=1, attempts=0)
    result = asyncio.run(runner._process_document(Db(), claimed, scan_id=uuid.uuid4(), root_url="https://a.test/", context=object()))
    return result, rec


def test_process_document_records_pages_with_their_methods_and_ocr_status(monkeypatch):
    result, rec = drive(monkeypatch, make_pdf(["Typed page", ""]), ocr_run=fake_ocr({2: "scanned page"}))
    assert result == "done"
    assert [(s["page_number"], s["detection_method"], s["kind"]) for s in rec.stored] == [(1, "pdf_text", "document_text"), (2, "pdf_ocr", "document_text")]
    assert rec.extraction == [("ocr_extracted", "pdf_text+pdf_ocr")]
    assert rec.commits == 1 and rec.done[0]["http_status"] == 200


def test_process_document_text_pdf_is_extracted(monkeypatch):
    _, rec = drive(monkeypatch, make_pdf(["Only text"]))
    assert rec.extraction == [("extracted", "pdf_text")]


def test_process_document_unsupported_is_done_with_a_reason_and_no_evidence(monkeypatch):
    result, rec = drive(monkeypatch, readers.OLE_MAGIC + b"\x00" * 20, content_type="application/msword")
    assert result == "done" and rec.stored == []
    assert rec.extraction == [("unsupported", None)] and "legacy Office" in rec.done[0]["note"]


def test_process_document_encrypted_and_corrupt(monkeypatch):
    _, rec = drive(monkeypatch, encrypted_pdf())
    assert rec.extraction == [("encrypted", None)]
    _, rec = drive(monkeypatch, b"%PDF-1.4 garbage")
    assert rec.extraction == [("unreadable", None)]


def test_process_document_too_large_is_excluded_and_marked(monkeypatch):
    result, rec = drive(monkeypatch, b"", fetch_error=runner.doc_fetch.DocumentTooLarge("99999999 bytes"))
    assert result == "excluded" and rec.extraction == [("too_large", None)] and "document_too_large" in rec.excluded[0]


def test_set_extraction_rejects_an_unknown_status():
    with pytest.raises(ValueError):
        asyncio.run(extract.set_extraction(None, uuid.uuid4(), "analysed", None))
