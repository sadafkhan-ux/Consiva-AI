"""An image request with no query string is a static asset (a product photo, a logo), but one that names itself a beacon is not: that would label a tracking pixel "functional"."""

import pytest

from app.rules.consent_rules import classify_tracker
from app.scanner.schemas import TrackerRecord


def tracker(src, resource_type="image"):
    return TrackerRecord(local_id="t", script_src=src, resource_type=resource_type, consent_states=["pre_consent"])


@pytest.mark.parametrize("src", [
    "https://edge.sitecorecloud.io/hondamotorc388f/media/Project/HONDA2WI/home/The-Cabinet.webp",      # seen live: a site's own CMS images
    "https://images.unsplash.com/photo-1523240795612-9a054b0db644.jpg",
    "https://d2xsxph8kpxj0f.cloudfront.net/310519663184702328/abc/trusted-06_803e00a7.png",
    "https://cdn-au.onetrust.com/logos/static/ot_company_logo.png",
    "https://cdn.example.org/blog/2026/10/team-photo.jpg",
    "https://cdn.example.org/images/blog-hero.png",
])
def test_an_ordinary_image_or_font_is_still_a_static_asset(src):
    t = classify_tracker(tracker(src))
    assert t.vendor == "Static CDN Asset" and t.category == "functional"


@pytest.mark.parametrize("src", [
    "https://t.example.net/pixel.gif",
    "https://collect.example.net/i.png",
    "https://analytics.example.net/hit.gif",
    "https://stats.example.net/1x1.gif",
    "https://cdn.example.net/track/open.png",
    "https://cdn.example.net/img/beacon.gif",
    "https://cdn.example.net/tracking/abc",
    "https://pixel.example.net/a.png",
    "https://cdn.example.net/telemetry/ping.gif",
    "https://cdn.example.net/impression/42.png",
])
def test_an_image_that_names_itself_a_beacon_is_not_called_functional(src):
    t = classify_tracker(tracker(src))
    assert t.vendor != "Static CDN Asset" and t.category is None, "left unclassified, so the score reports it for review instead of passing it"


def test_the_existing_rules_for_scripts_and_query_strings_are_unchanged():
    assert classify_tracker(tracker("https://cdn.example.net/a.png?id=7")).category is None        # a query string was always disqualifying
    assert classify_tracker(tracker("https://cdn.example.net/app.js", "script")).category is None   # a script was never a static asset
    assert classify_tracker(tracker("https://www.google-analytics.com/analytics.js", "script")).category == "analytics"   # the vendor catalogue still decides first


def test_an_unclassified_beacon_before_consent_is_reported_for_review_not_passed():
    from datetime import UTC, datetime

    from app.scoring.dpdp_score import assess
    from tests.test_dpdp_score import evidence
    t = classify_tracker(tracker("https://t.example.net/pixel.gif"))
    ev = evidence()
    ev["trackers"] = [{"id": "t1", "script_src": t.script_src, "vendor": t.vendor, "category": t.category, "source": "rule", "consent_states": ["pre_consent"], "page_url": "https://x.test/"}]
    c1 = next(r for r in assess(ev, datetime(2026, 10, 27, tzinfo=UTC).date()).requirements if r.id == "DPDP-C1")
    assert c1.status == "partially_met" and any("unclassified" in o for o in c1.observations)
