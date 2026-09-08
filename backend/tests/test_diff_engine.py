"""Continuous Monitoring's diff engine (app/services/diff_engine.py) — pure function,
no DB needed. Covers: added/removed/changed detection, the query-string dedup key
(matching tracker_detector.py's own dedup so a cache-busted param never looks like a
removed+added pair), and the has_material_change heuristic."""

from app.services.diff_engine import diff_evidence


def _evidence(**overrides) -> dict:
    base = {
        "pages": [], "cookies": [], "trackers": [], "third_party_services": [], "policies": [],
        "consent_signals": [{"mechanism_type": "banner", "has_reject_all": False, "has_granular_choices": False}],
    }
    base.update(overrides)
    return base


def test_new_tracker_and_cookie_are_added_and_material():
    baseline = _evidence()
    new = _evidence(
        trackers=[{"script_src": "https://ads.example.com/pixel.js", "category": "marketing", "vendor": "AdCo"}],
        cookies=[{"name": "_new", "domain": "x.com", "category": "marketing", "vendor": "AdCo", "is_first_party": False}],
    )
    result = diff_evidence(baseline, new)
    assert result["has_material_change"] is True
    assert len(result["added"]["trackers"]) == 1
    assert len(result["added"]["cookies"]) == 1
    assert result["removed"] == {}


def test_removed_tracker_is_material():
    baseline = _evidence(trackers=[{"script_src": "https://ads.example.com/pixel.js", "category": "marketing", "vendor": "AdCo"}])
    new = _evidence()
    result = diff_evidence(baseline, new)
    assert result["has_material_change"] is True
    assert len(result["removed"]["trackers"]) == 1
    assert result["added"] == {}


def test_cache_busted_tracker_url_is_not_added_or_removed():
    """The same real tracker with a different cache-busting query param must diff as
    unchanged, not as a removed+added pair -- matches tracker_detector.py's own
    query-stripped dedup key."""
    baseline = _evidence(trackers=[{"script_src": "https://cdn.x.com/ga.js?v=1&session=abc", "category": "analytics", "vendor": "GA"}])
    new = _evidence(trackers=[{"script_src": "https://cdn.x.com/ga.js?v=2&session=xyz", "category": "analytics", "vendor": "GA"}])
    result = diff_evidence(baseline, new)
    assert result["added"] == {}
    assert result["removed"] == {}
    assert result["has_material_change"] is False


def test_reclassification_into_non_essential_is_material():
    """An item that stays present but is re-classified INTO analytics/marketing is a
    real, useful signal (e.g. lookup table learned something new) even though nothing
    was added or removed."""
    baseline = _evidence(trackers=[{"script_src": "https://cdn.x.com/x.js", "category": "functional", "vendor": "CDN"}])
    new = _evidence(trackers=[{"script_src": "https://cdn.x.com/x.js", "category": "marketing", "vendor": "CDN"}])
    result = diff_evidence(baseline, new)
    assert result["has_material_change"] is True
    assert result["changed"]["trackers"][0]["fields"]["category"] == {"from": "functional", "to": "marketing"}


def test_functional_to_functional_reclassification_is_not_material():
    baseline = _evidence(trackers=[{"script_src": "https://cdn.x.com/x.js", "category": "functional", "vendor": "OldName"}])
    new = _evidence(trackers=[{"script_src": "https://cdn.x.com/x.js", "category": "functional", "vendor": "NewName"}])
    result = diff_evidence(baseline, new)
    assert result["changed"]["trackers"][0]["fields"] == {"vendor": {"from": "OldName", "to": "NewName"}}
    assert result["has_material_change"] is False


def test_consent_mechanism_change_is_always_material():
    baseline = _evidence(consent_signals=[{"mechanism_type": "banner", "has_reject_all": True, "has_granular_choices": True}])
    new = _evidence(consent_signals=[{"mechanism_type": "none", "has_reject_all": None, "has_granular_choices": None}])
    result = diff_evidence(baseline, new)
    assert result["has_material_change"] is True
    assert "consent_signals" in result["changed"]


def test_no_changes_at_all_is_not_material():
    evidence = _evidence(
        cookies=[{"name": "_ga", "domain": "x.com", "category": "analytics", "vendor": "GA", "is_first_party": False}],
    )
    result = diff_evidence(evidence, evidence)
    assert result == {"added": {}, "removed": {}, "changed": {}, "has_material_change": False}


def test_new_page_alone_is_not_material():
    """A new/removed PAGE (as opposed to a new tracker/cookie) isn't, on its own,
    treated as material -- only cookies/trackers and consent-signal changes are."""
    baseline = _evidence(pages=[{"url": "https://x.com/", "http_status": 200}])
    new = _evidence(pages=[{"url": "https://x.com/", "http_status": 200}, {"url": "https://x.com/about", "http_status": 200}])
    result = diff_evidence(baseline, new)
    assert result["added"]["pages"]
    assert result["has_material_change"] is False
