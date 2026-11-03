"""A scanned page carries where it ended up and what the server said it was; a page that did not load carries neither, and nothing is guessed."""

from app.api.v1.routes.consent_scans import EvidencePageItem
from app.db.models import WebsitePage
from app.scanner.schemas import PageRecord


def test_a_page_record_carries_the_final_url_and_content_type():
    p = PageRecord(local_id="page-0", url="http://x.test/", title="Home", http_status=200, discovered_via="seed",
                   final_url="https://x.test/", content_type="text/html; charset=utf-8")
    assert p.final_url == "https://x.test/" and p.content_type.startswith("text/html")


def test_a_page_that_did_not_load_has_neither():
    p = PageRecord(local_id="page-1", url="https://x.test/gone", discovered_via="failed")
    assert p.final_url is None and p.content_type is None


def test_the_stored_row_and_the_api_item_have_the_fields():
    assert hasattr(WebsitePage, "final_url") and hasattr(WebsitePage, "content_type")
    item = EvidencePageItem(id="1", url="http://x.test/", title=None)
    assert item.final_url is None and item.content_type is None      # scans made before the columns existed
    assert EvidencePageItem(id="1", url="http://x.test/", title=None, final_url="https://x.test/", content_type="text/html").final_url == "https://x.test/"
