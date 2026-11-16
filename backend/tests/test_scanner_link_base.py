"""Links are resolved against the address a page ended at, not the address that was asked for. A site that redirects example.com to www.example.com has its relative links on www; resolving them
against the apex sent the xpenseai.in scan (13 Nov 2026) to addresses that answered 404: eight of nine pages, the privacy policy among them."""
import re
from pathlib import Path

from app.scanner.page_parser import parse_page

CRAWLER = (Path(__file__).resolve().parents[1] / "app" / "scanner" / "crawler.py").read_text(encoding="utf-8")
HTML = '<html><head><title>x</title></head><body><a href="/privacy-policy">Privacy</a><a href="pricing">Pricing</a><a href="https://other.test/x">Other</a></body></html>'


def test_relative_links_resolve_against_the_final_address_after_a_redirect():
    hrefs = {link.href for link in parse_page(HTML, "https://www.x.test/").links}
    assert "https://www.x.test/privacy-policy" in hrefs and "https://www.x.test/pricing" in hrefs
    assert "https://x.test/privacy-policy" not in hrefs


def test_the_crawler_hands_the_final_address_to_the_parser():
    assert re.search(r"parsed = parse_page\(html, final_url or url, checked_states\)", CRAWLER), "the page parser must be given the address the page ended at"
    assert "parsed = parse_page(html, url, checked_states)" not in CRAWLER
