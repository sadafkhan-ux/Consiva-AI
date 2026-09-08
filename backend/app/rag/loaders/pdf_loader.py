from pathlib import Path

from pypdf import PdfReader


def load_pdf(path: Path) -> list[dict]:
    """Returns [{"page_number": int, "text": str}, ...], 1-indexed."""
    reader = PdfReader(str(path))
    return [
        {"page_number": i + 1, "text": page.extract_text() or ""}
        for i, page in enumerate(reader.pages)
    ]
