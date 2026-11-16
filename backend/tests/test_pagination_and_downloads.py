"""A whole-site scan follows pagination but not filters; a link to a file is not a page that failed."""
import pytest

from app.scanner.url_normalize import is_pagination_only


@pytest.mark.parametrize("url,expected", [
    ("https://x.test/blog?page=2", True),
    ("https://x.test/blog?paged=3&p=3", True),
    ("https://x.test/blog?PAGE=2", True),
    ("https://x.test/blog?page=2&sort=asc", False),
    ("https://x.test/case-studies?filter=BFSI", False),
    ("https://x.test/contact?service=sap-ml&utm_source=blog", False),
    ("https://x.test/blog", False),
])
def test_only_pagination_queries_are_pagination(url, expected):
    assert is_pagination_only(url) is expected


def test_the_crawler_records_a_download_as_skipped_not_failed():
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "app" / "scanner" / "crawler.py").read_text(encoding="utf-8")
    assert '"Download is starting" in (result.get("error") or "")' in src
    block = src.split('"Download is starting" in')[1].split("return")[0]
    assert 'tracker.exclude(url, "document_download")' in block and "tracker.fail" not in block
    assert "not (budget is None and is_pagination_only(url))" in src      # only an uncapped scan follows pagination variants
