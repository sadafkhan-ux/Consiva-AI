"""What the Enterprise crawl may follow."""
import pytest

from app.enterprise.crawl import scope

ROOT = "https://www.example.com/"


@pytest.mark.parametrize("url,expected", [
    ("https://www.example.com/about", scope.PAGE),
    ("https://example.com/privacy", scope.PAGE),                         # same registered domain
    ("https://shop.example.com/", scope.PAGE),                           # a subdomain is the same site, as in the 5-page scanner
    ("https://other.org/", scope.EXTERNAL),
    ("mailto:a@example.com", scope.SKIP), ("tel:+911234", scope.SKIP), ("javascript:void(0)", scope.SKIP),
    ("https://www.example.com/logo.png", scope.SKIP), ("https://www.example.com/app.js", scope.SKIP),
    ("https://www.example.com/policy.pdf", scope.DOCUMENT), ("https://www.example.com/terms.DOCX", scope.DOCUMENT),
    ("https://www.example.com/blog?page=2", scope.PAGE),                 # pagination is a different page
    ("https://www.example.com/blog?paged=3&p=3", scope.PAGE),
    ("https://www.example.com/shop?color=red&sort=price", scope.QUERY_VARIANT),
    ("https://www.example.com/blog?page=2&sort=asc", scope.QUERY_VARIANT),
    ("https://www.example.com/about#team", scope.PAGE),                  # a fragment is the same page
])
def test_classify(url, expected):
    assert scope.classify(url, ROOT) == expected


def test_a_fragment_is_removed():
    assert scope.clean("https://www.example.com/a#b") == "https://www.example.com/a"


def test_only_query_variants_are_recorded_as_excluded_documents_are_read():
    assert scope.EXCLUDE_REASONS[scope.QUERY_VARIANT]
    assert scope.DOCUMENT not in scope.EXCLUDE_REASONS and scope.PAGE not in scope.EXCLUDE_REASONS
