"""Advertising networks seen "unclassified" on real news sites are classified as marketing, so R-001 can see them.

Measured on bbc.com, cnn.com and hindustantimes.com: 74-81% of third-party requests were unclassified, led by these ad
networks and exchanges. R-001 counts only analytics and marketing, so they were invisible to it.
"""

import pytest

from app.rules.consent_rules import classify_tracker, evaluate_consent_rules
from app.rules.tracker_catalog import match_vendor
from app.scanner.schemas import TrackerRecord

CASES = [
    ("https://ads.dianomi.com/x", "Dianomi"),
    ("https://trc.taboola.com/a.js", "Taboola"),
    ("https://fastlane.rubiconproject.com/a", "Magnite (Rubicon Project)"),
    ("https://d.adroll.com/pixel", "AdRoll (NextRoll)"),
    ("https://s.seedtag.com/x.js", "Seedtag"),
    ("https://cdn.permutive.app/a.js", "Permutive"),
    ("https://static.criteo.net/js/ld/publishertag.js", "Criteo"),
    ("https://widgets.outbrain.com/outbrain.js", "Outbrain"),
    ("https://static.klaviyo.com/onsite/js/klaviyo.js", "Klaviyo"),
    ("https://aax.amazon-adsystem.com/e/dtb", "Amazon Advertising"),
    ("https://u.openx.net/w/1.0/pd", "OpenX"),
    ("https://ads.pubmatic.com/AdServer/js", "PubMatic"),
    ("https://htlb.casalemedia.com/cygnus", "Index Exchange"),
    ("https://match.adsrvr.org/track/cmf", "The Trade Desk"),
    ("https://ib.adnxs.com/getuid", "Xandr (Microsoft Advertising)"),
    ("https://a.teads.tv/page/1/tag", "Teads"),
    ("https://ced-ns.sascdn.com/x", None),                 # not in the catalogue: stays unclassified
    ("https://www.bbc.co.uk/news", None),
]


@pytest.mark.parametrize("url,vendor", CASES)
def test_ad_networks_are_matched_and_unknown_hosts_are_left_alone(url, vendor):
    sig = match_vendor(script_src=url)

    assert (sig.vendor if sig else None) == vendor
    if sig:
        assert sig.default_category == "marketing"


def test_an_ad_network_request_before_consent_is_now_counted_by_r001():
    tracker = classify_tracker(TrackerRecord(local_id="t1", script_src="https://ads.dianomi.com/x", consent_states=["pre_consent"]))
    assert tracker.category == "marketing" and tracker.vendor == "Dianomi"

    evidence = {
        "cookies": [], "forms": [], "policies": [{"policy_type": "privacy_policy"}, {"policy_type": "cookie_policy"}],
        "trackers": [{"id": "t1", "script_src": tracker.script_src, "vendor": tracker.vendor, "category": tracker.category,
                      "source": "rule", "consent_states": ["pre_consent"], "page_url": "https://news.example/"}],
        "consent_signals": [{"mechanism_type": "cmp", "cmp_vendor": "OneTrust", "has_reject_all": True, "has_granular_choices": True, "evidence": {}}],
    }
    assert "R-001" in {f.rule_id for f in evaluate_consent_rules(evidence)}


def test_no_existing_vendor_is_shadowed_by_the_new_entries():
    """First match wins, so a new broad substring must not capture a host an earlier vendor already owns."""
    for url, expected in [
        ("https://www.googletagmanager.com/gtm.js", "Google Tag Manager"),
        ("https://connect.facebook.net/en_US/fbevents.js", "Meta Pixel"),
        ("https://bat.bing.com/p/action/1.js", "Bing UET"),
        ("https://stats.g.doubleclick.net/g/collect", "Google Ads"),
        ("https://api.mixpanel.com/track", "Mixpanel"),
    ]:
        assert match_vendor(script_src=url).vendor == expected
