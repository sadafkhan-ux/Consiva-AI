"""The URL inventory: one record per distinct URL, its depth, its status and why. Pure bookkeeping, tested without a browser or a database."""

from datetime import UTC, datetime, timedelta

import pytest

from app.scanner import url_inventory as inv
from app.scanner.url_inventory import InvalidTransition, InventoryTracker, incomplete_extraction


class _Clock:
    """A clock that moves one second per reading, so a timestamp that was recorded is always later than the one before it."""

    def __init__(self):
        self.t = datetime(2026, 11, 3, 12, 0, 0, tzinfo=UTC)

    def __call__(self):
        self.t += timedelta(seconds=1)
        return self.t


def _tracker(max_urls=None):
    return InventoryTracker(max_urls=max_urls, clock=_Clock())


# ── creation, normalization, duplicates, sources, depth ─────────────────────────────────────────────

def test_a_discovered_url_is_one_pending_record_with_its_source_depth_and_time():
    t = _tracker()
    rec = t.discover("https://Example.com/About/", "link", 2)
    assert rec is not None
    assert (rec.scan_status, rec.discovery_source, rec.discovery_sources, rec.crawl_depth) == ("PENDING", "link", ["link"], 2)
    assert rec.original_url == "https://Example.com/About/" and rec.domain == "example.com"
    assert rec.discovered_at == rec.last_updated_at and rec.scan_started_at is None and rec.scan_completed_at is None


def test_the_same_page_spelled_differently_is_one_record():
    t = _tracker()
    t.discover("https://example.com/about", "link", 1)
    for spelling in ("https://example.com/about/", "https://EXAMPLE.com/about", "https://example.com/about#team"):
        assert t.discover(spelling, "link", 1) is None
    assert len(t) == 1
    assert t.get("https://example.com/about/") is t.get("https://example.com/about")


def test_every_source_that_named_a_url_is_kept_and_the_first_stays_the_discovery_source():
    t = _tracker()
    t.discover("https://example.com/a", "sitemap", inv.SITEMAP_DEPTH)
    t.discover("https://example.com/a", "link", 3)
    t.discover("https://example.com/a", "link", 3)
    rec = t.get("https://example.com/a")
    assert rec.discovery_source == "sitemap" and rec.discovery_sources == ["sitemap", "link"]
    assert rec.crawl_depth == inv.SITEMAP_DEPTH           # the depth it was first found at is not rewritten by a later, deeper mention


def test_depth_is_what_the_caller_says_and_never_negative():
    t = _tracker()
    assert t.discover("https://example.com/", "seed", 0).crawl_depth == 0
    assert t.depth_of("https://example.com/") == 0 and t.depth_of("https://example.com/unknown") is None
    with pytest.raises(ValueError):
        t.discover("https://example.com/x", "link", -1)


def test_the_url_limit_counts_what_it_did_not_record():
    t = _tracker(max_urls=2)
    t.discover("https://example.com/1", "link", 1)
    t.discover("https://example.com/2", "link", 1)
    assert t.discover("https://example.com/3", "link", 1) is None
    assert t.discover("https://example.com/4", "link", 1) is None
    assert len(t) == 2 and t.over_limit == 2
    assert t.discover("https://example.com/1", "sitemap", 1) is None and t.over_limit == 2       # a known URL is not "over the limit"


# ── the status lifecycle ─────────────────────────────────────────────────────────────────────────────

def test_a_page_that_loads_goes_pending_in_progress_completed_with_its_timestamps():
    t = _tracker()
    t.discover("https://example.com/a", "link", 1)
    t.start("https://example.com/a")
    rec = t.get("https://example.com/a")
    assert rec.scan_status == "IN_PROGRESS" and rec.scan_started_at is not None and rec.scan_completed_at is None
    t.finish("https://example.com/a", final_url="https://example.com/a2", title="A", http_status=200, content_type="text/html")
    assert (rec.scan_status, rec.final_url, rec.page_title, rec.http_status, rec.content_type) == ("COMPLETED", "https://example.com/a2", "A", 200, "text/html")
    assert rec.discovered_at < rec.scan_started_at < rec.scan_completed_at and rec.last_updated_at >= rec.scan_completed_at


def test_a_failed_page_keeps_its_reason_and_status_and_is_not_completed():
    t = _tracker()
    t.discover("https://example.com/gone", "link", 1)
    t.start("https://example.com/gone")
    t.fail("https://example.com/gone", "HTTP 404", http_status=404)
    rec = t.get("https://example.com/gone")
    assert (rec.scan_status, rec.failure_reason, rec.http_status) == ("FAILED", "HTTP 404", 404)
    assert rec.scan_completed_at is not None and rec.final_url is None


def test_a_failure_without_a_message_still_says_something():
    t = _tracker()
    t.discover("https://example.com/x", "link", 1)
    t.start("https://example.com/x")
    t.fail("https://example.com/x", "")
    assert t.get("https://example.com/x").failure_reason


def test_a_page_that_loaded_but_could_not_be_extracted_is_partially_scanned_with_the_reason():
    t = _tracker()
    t.discover("https://example.com/file", "link", 1)
    t.start("https://example.com/file")
    t.finish("https://example.com/file", final_url="https://example.com/file", title=None, http_status=200, content_type="application/pdf",
             incomplete_reason=incomplete_extraction(text_length=0, content_type="application/pdf"))
    rec = t.get("https://example.com/file")
    assert rec.scan_status == "PARTIALLY_SCANNED" and "not an HTML page" in rec.status_reason and rec.http_status == 200


def test_an_excluded_url_keeps_its_reason_and_is_never_started():
    t = _tracker()
    t.discover("https://example.com/private", "link", 1)
    t.exclude("https://example.com/private", "robots_disallowed")
    rec = t.get("https://example.com/private")
    assert (rec.scan_status, rec.status_reason, rec.scan_started_at) == ("EXCLUDED", "robots_disallowed", None)
    with pytest.raises(InvalidTransition):
        t.start("https://example.com/private")


def test_a_loaded_page_that_redirected_to_a_page_already_scanned_is_excluded_and_keeps_where_it_landed():
    t = _tracker()
    t.discover("https://example.com/out", "link", 1)
    t.start("https://example.com/out")
    t.exclude("https://example.com/out", "redirected_to_already_scanned_page", final_url="https://example.com/home", http_status=200)
    rec = t.get("https://example.com/out")
    assert (rec.scan_status, rec.final_url, rec.status_reason) == ("EXCLUDED", "https://example.com/home", "redirected_to_already_scanned_page")


@pytest.mark.parametrize("final", ["COMPLETED", "FAILED", "PARTIALLY_SCANNED", "EXCLUDED"])
def test_a_final_status_cannot_change(final):
    assert inv.TRANSITIONS[final] == frozenset()


def test_the_only_moves_are_the_documented_ones():
    assert inv.TRANSITIONS["PENDING"] == {"IN_PROGRESS", "EXCLUDED"}
    assert inv.TRANSITIONS["IN_PROGRESS"] == {"COMPLETED", "PARTIALLY_SCANNED", "FAILED", "EXCLUDED"}
    assert set(inv.TRANSITIONS) == set(inv.STATUSES)


def test_an_invalid_move_raises_and_changes_nothing():
    t = _tracker()
    t.discover("https://example.com/a", "link", 1)
    with pytest.raises(InvalidTransition):
        t.finish("https://example.com/a", final_url=None, title=None, http_status=200, content_type="text/html")   # PENDING -> COMPLETED skips IN_PROGRESS
    assert t.get("https://example.com/a").scan_status == "PENDING"
    t.start("https://example.com/a")
    t.finish("https://example.com/a", final_url=None, title=None, http_status=200, content_type="text/html")
    with pytest.raises(InvalidTransition):
        t.start("https://example.com/a")                                                                               # COMPLETED -> IN_PROGRESS
    with pytest.raises(InvalidTransition):
        t.fail("https://example.com/a", "late")                                                                       # COMPLETED -> FAILED


# ── discovered is not scanned ────────────────────────────────────────────────────────────────────────

def test_a_url_the_crawl_never_started_stays_pending_with_the_reason_it_was_not_reached():
    t = _tracker()
    for u in ("a", "b", "c"):
        t.discover(f"https://example.com/{u}", "link", 1)
    t.start("https://example.com/a")
    t.finish("https://example.com/a", final_url=None, title=None, http_status=200, content_type="text/html")
    t.leave_pending({u: "page_limit" for u in t.pending_urls()})
    assert [(r.scan_status, r.status_reason) for r in t.records()] == [("COMPLETED", None), ("PENDING", "page_limit"), ("PENDING", "page_limit")]
    assert t.counts() == {"PENDING": 2, "IN_PROGRESS": 0, "COMPLETED": 1, "FAILED": 0, "PARTIALLY_SCANNED": 0, "EXCLUDED": 0}


def test_leaving_pending_never_touches_a_url_that_has_moved_on():
    t = _tracker()
    t.discover("https://example.com/a", "link", 1)
    t.exclude("https://example.com/a", "depth_limit")
    t.leave_pending({"https://example.com/a": "page_limit"})
    assert t.get("https://example.com/a").status_reason == "depth_limit"


@pytest.mark.parametrize("length,ctype,partial", [
    (500, "text/html; charset=utf-8", False),
    (500, None, False),
    (0, "text/html", True),
    (500, "application/pdf", True),
    (500, "application/xhtml+xml", False),
    (500, "image/png", True),
])
def test_http_success_is_not_extraction_success(length, ctype, partial):
    assert (incomplete_extraction(text_length=length, content_type=ctype) is not None) is partial


def test_counts_cover_every_status_even_when_empty():
    assert set(_tracker().counts()) == set(inv.STATUSES)
