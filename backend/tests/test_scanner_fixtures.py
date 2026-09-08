"""Fixture-based edge-case tests for the scanner detectors: absence of evidence must
produce an empty/neutral result, never an error or fabricated data, and evidence for
an unrecognized vendor must still be recorded (just left unclassified).

No live network, no live browser -- detector functions are called directly with
hand-constructed intermediate objects (ParsedScript/ParsedLink), mirroring the pattern
in tests/test_form_detector.py.
"""

from app.rules.tracker_catalog import TRACKER_CATALOG
from app.scanner.consent_signal_detector import detect_consent_signal
from app.scanner.cookie_detector import detect_cookies
from app.scanner.page_parser import ParsedLink, ParsedScript
from app.scanner.policy_detector import detect_policies
from app.scanner.tracker_detector import detect_trackers


def test_no_cookies_returns_empty_list():
    records = detect_cookies([], "example.com")
    assert records == []


def test_no_trackers_returns_empty_list():
    records = detect_trackers(
        site_domain="example.com",
        scripts_by_page={},
        network_requests_by_page={},
    )
    assert records == []


def test_unrecognized_tracker_is_recorded_but_left_unclassified():
    # tracker_detector.detect_trackers never calls rules.tracker_catalog.match_vendor --
    # vendor/category classification happens later in rules/consent_rules.py. So an
    # unrecognized script src still yields a TrackerRecord (evidence isn't dropped),
    # with vendor/category left at their schema default of None rather than a guessed
    # name -- confirmed by reading both modules, not assumed.
    unknown_src = "https://cdn.some-unlisted-vendor.net/track.js"
    known_domain_substrings = {
        sub for sig in TRACKER_CATALOG for sub in sig.domain_substrings
    }
    assert not any(sub in unknown_src for sub in known_domain_substrings)  # sanity: genuinely unknown

    records = detect_trackers(
        site_domain="example.com",
        scripts_by_page={"page-0": [ParsedScript(src=unknown_src, inline_snippet=None)]},
        network_requests_by_page={},
    )

    [record] = records
    assert record.script_src == unknown_src
    assert record.page_local_id == "page-0"
    assert record.vendor is None
    assert record.category is None
    assert record.source is None


def test_no_policy_links_returns_empty_list():
    links = [
        ParsedLink(href="https://example.com/about", text="About us"),
        ParsedLink(href="https://example.com/pricing", text="Pricing"),
    ]
    records = detect_policies(links, {})
    assert records == []


def test_no_consent_signal_returns_none_mechanism():
    record = detect_consent_signal(scripts=[], visible_text="", detected_global_vars=[])
    assert record.mechanism_type == "none"
    assert record.has_reject_all is None
    assert record.has_granular_choices is None
    assert record.cmp_vendor is None
