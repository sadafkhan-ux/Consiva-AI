"""Reading linked documents, from real PDF and DOCX bytes built in the test (no network, no files)."""
import io
import zipfile

import pytest

from app.enterprise.documents import readers


def make_pdf(pages: list[str]) -> bytes:
    """A small valid PDF: one page per string ('' = a page with no text, like a scanned image)."""
    objs: list[bytes] = []
    n = len(pages)
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(n))
    objs.append(b"<</Type/Catalog/Pages 2 0 R>>")
    objs.append(f"<</Type/Pages/Kids[{kids}]/Count {n}>>".encode())
    for i, text in enumerate(pages):
        content = f"BT /F1 12 Tf 20 100 Td ({text}) Tj ET".encode() if text else b""
        objs.append(f"<</Type/Page/Parent 2 0 R/MediaBox[0 0 300 200]/Contents {4 + 2 * i} 0 R/Resources<</Font<</F1 {3 + 2 * n} 0 R>>>>>>".encode())
        objs.append(b"<</Length %d>>\nstream\n" % len(content) + content + b"\nendstream")
    objs.append(b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<</Root 1 0 R/Size {len(objs) + 1}>>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def make_docx(paragraphs: list[str]) -> bytes:
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    xml = f'<?xml version="1.0"?><w:document xmlns:w="{readers.W_NS}"><w:body>{body}</w:body></w:document>'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", xml)
    return buf.getvalue()


def test_a_pdf_is_read_page_by_page_with_page_numbers():
    doc = readers.read_pdf(make_pdf(["We collect your email", "Retention is one year"]))
    assert doc.kind == "pdf" and doc.page_count == 2
    assert doc.pages[0][0] == 1 and "We collect your email" in doc.pages[0][1]
    assert doc.pages[1][0] == 2 and "Retention is one year" in doc.pages[1][1]
    assert doc.needs_ocr == []


def test_a_page_with_no_text_is_listed_as_needing_ocr_and_the_others_are_still_read():
    doc = readers.read_pdf(make_pdf(["Notice text", "", "Grievance officer"]))
    assert doc.needs_ocr == [2]
    assert [n for n, t in doc.pages if t] == [1, 3]


def test_a_docx_is_read_as_unpaged_text():
    doc = readers.read_docx(make_docx(["Privacy Policy", "We keep data for one year."]))
    assert doc.kind == "docx" and doc.pages == [(0, "Privacy Policy\nWe keep data for one year.")]


@pytest.mark.parametrize("data", [b"", b"not a pdf at all", b"%PDF-1.4 garbage"])
def test_an_unreadable_pdf_raises_a_clear_error(data):
    with pytest.raises(readers.DocumentUnreadable):
        readers.read_pdf(data)


def test_an_unreadable_docx_raises_a_clear_error():
    with pytest.raises(readers.DocumentUnreadable):
        readers.read_docx(b"PK-not-really-a-zip")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("other.xml", "<a/>")
    with pytest.raises(readers.DocumentUnreadable):
        readers.read_docx(buf.getvalue())


def test_the_reader_is_chosen_from_the_bytes_first_then_the_address():
    assert readers.read_document(make_pdf(["x"]), "https://a.test/policy", "application/octet-stream").kind == "pdf"
    assert readers.read_document(make_docx(["x"]), "https://a.test/terms", None).kind == "docx"
    with pytest.raises(readers.DocumentUnreadable):
        readers.read_document(b"plain text", "https://a.test/notes.txt", "text/plain")


def test_xml_entities_in_a_docx_are_not_expanded():
    """A hostile DOCX must not be able to read local files or blow up through entity expansion."""
    xml = f'<?xml version="1.0"?><!DOCTYPE d [<!ENTITY x SYSTEM "file:///etc/passwd">]><w:document xmlns:w="{readers.W_NS}"><w:body><w:p><w:r><w:t>a&x;b</w:t></w:r></w:p></w:body></w:document>'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", xml)
    try:
        doc = readers.read_docx(buf.getvalue())
    except readers.DocumentUnreadable:
        return
    assert "root:" not in doc.text
