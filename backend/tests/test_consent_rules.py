from app.rules.consent_rules import classify_cookie, classify_tracker, evaluate_consent_rules
from app.scanner.schemas import CookieRecord, TrackerRecord


def _evidence(**overrides) -> dict:
    base = {
        "cookies": [],
        "trackers": [],
        "forms": [],
        "policies": [],
        "consent_signals": [{"mechanism_type": "none", "cmp_vendor": None,
                              "has_reject_all": None, "has_granular_choices": None}],
    }
    base.update(overrides)
    return base


def test_classify_cookie_matches_known_vendor():
    cookie = CookieRecord(local_id="cookie-0", name="_ga", domain="example.com")
    classified = classify_cookie(cookie)
    assert classified.vendor == "Google Analytics"
    assert classified.category == "analytics"
    assert classified.source == "rule"


def test_classify_tracker_unmatched_stays_unclassified():
    tracker = TrackerRecord(local_id="tracker-0", script_src="https://unknown-vendor.example/x.js")
    classified = classify_tracker(tracker)
    assert classified.vendor is None
    assert classified.category is None


def test_tracking_without_consent_mechanism_flagged_high_risk():
    evidence = _evidence(
        trackers=[{"id": "t1", "script_src": "https://google-analytics.com/x.js",
                   "vendor": "Google Analytics", "category": "analytics"}],
    )
    findings = evaluate_consent_rules(evidence)
    rule_ids = {f.rule_id for f in findings}
    assert "R-002" in rule_ids  # no consent mechanism present
    high_risk = next(f for f in findings if f.rule_id == "R-002")
    assert high_risk.risk_level == "high"


def test_missing_privacy_policy_always_flagged():
    findings = evaluate_consent_rules(_evidence())
    assert any(f.rule_id == "R-006" for f in findings)


def test_clean_site_with_policy_and_no_tracking_has_minimal_findings():
    evidence = _evidence(policies=[{"id": "p1", "url": "https://example.com/privacy", "policy_type": "privacy_policy"}])
    findings = evaluate_consent_rules(evidence)
    rule_ids = {f.rule_id for f in findings}
    assert "R-006" not in rule_ids  # missing_privacy_policy
    assert "R-002" not in rule_ids  # no_consent_mechanism_present


def test_form_with_personal_data_and_no_consent_flagged_low_confidence():
    evidence = _evidence(
        policies=[{"id": "p1", "url": "https://example.com/privacy", "policy_type": "privacy_policy"}],
        forms=[{"id": "f1", "fields": [{"name": "email", "type": "email"}]}],
    )
    findings = evaluate_consent_rules(evidence)
    form_findings = [f for f in findings if f.rule_id == "R-004"]
    assert len(form_findings) == 1
    assert form_findings[0].confidence == "low"


def test_tracker_fired_before_consent_flagged_high_risk():
    evidence = _evidence(
        trackers=[{"id": "t1", "script_src": "https://google-analytics.com/x.js",
                   "vendor": "Google Analytics", "category": "analytics", "consent_states": ["pre_consent"]}],
        policies=[{"id": "p1", "url": "https://example.com/privacy", "policy_type": "privacy_policy"},
                  {"id": "p2", "url": "https://example.com/cookies", "policy_type": "cookie_policy"}],
    )
    findings = evaluate_consent_rules(evidence)
    r001 = next(f for f in findings if f.rule_id == "R-001")
    assert r001.risk_level == "high"
    assert r001.evidence_ids == ["t1"]


def test_tracker_fires_after_reject_flagged_high_risk():
    evidence = _evidence(
        cookies=[{"id": "c1", "name": "_fbp", "domain": "example.com", "category": "marketing",
                  "consent_states": ["post_reject"]}],
        policies=[{"id": "p1", "url": "https://example.com/privacy", "policy_type": "privacy_policy"},
                  {"id": "p2", "url": "https://example.com/cookies", "policy_type": "cookie_policy"}],
    )
    findings = evaluate_consent_rules(evidence)
    r003 = next(f for f in findings if f.rule_id == "R-003")
    assert r003.risk_level == "high"
    assert r003.evidence_ids == ["c1"]


def test_missing_consent_state_data_does_not_trigger_r001_or_r003():
    """Older evidence / fixtures without consent_states shouldn't crash or false-positive."""
    evidence = _evidence(
        trackers=[{"id": "t1", "script_src": "https://google-analytics.com/x.js",
                   "vendor": "Google Analytics", "category": "analytics"}],  # no "consent_states" key at all
    )
    findings = evaluate_consent_rules(evidence)
    rule_ids = {f.rule_id for f in findings}
    assert "R-001" not in rule_ids
    assert "R-003" not in rule_ids


def test_missing_interaction_evidence_does_not_trigger_r009_or_r010():
    """Older evidence / fixtures with no "evidence" sub-dict at all (predates the
    scanner-hardening audit's accept/reject-interaction surfacing) shouldn't crash or
    false-positive -- same resilience contract as consent_states above."""
    evidence = _evidence(
        consent_signals=[{"mechanism_type": "cmp", "cmp_vendor": "OneTrust",
                           "has_reject_all": True, "has_granular_choices": True}],  # no "evidence" key at all
    )
    findings = evaluate_consent_rules(evidence)
    rule_ids = {f.rule_id for f in findings}
    assert "R-009" not in rule_ids
    assert "R-010" not in rule_ids


def test_reject_not_automatable_on_a_real_cmp_flagged():
    evidence = _evidence(
        policies=[{"id": "p1", "url": "https://example.com/privacy", "policy_type": "privacy_policy"}],
        consent_signals=[{"mechanism_type": "cmp", "cmp_vendor": "OneTrust",
                           "has_reject_all": True, "has_granular_choices": True,
                           "evidence": {"accept_interaction": "clicked", "reject_interaction": "cmp_not_automatable"}}],
    )
    findings = evaluate_consent_rules(evidence)
    r009 = next(f for f in findings if f.rule_id == "R-009")
    assert "OneTrust" in r009.summary
    assert r009.confidence == "low"


def test_reject_not_found_when_no_mechanism_detected_does_not_flag_r009():
    """R-009 is specifically about a DETECTED mechanism that couldn't be automated --
    a genuinely bannerless page correctly has nothing to automate, so "not_found"
    against mechanism_type="none" must not be conflated with R-009's scanner-
    limitation signal (that case is R-002's territory: no mechanism at all)."""
    evidence = _evidence(
        consent_signals=[{"mechanism_type": "none", "cmp_vendor": None,
                           "has_reject_all": None, "has_granular_choices": None,
                           "evidence": {"accept_interaction": "cmp_not_found", "reject_interaction": "cmp_not_found"}}],
    )
    findings = evaluate_consent_rules(evidence)
    assert "R-009" not in {f.rule_id for f in findings}


def test_page_unreachable_during_accept_or_reject_pass_flagged_r010():
    evidence = _evidence(
        policies=[{"id": "p1", "url": "https://example.com/privacy", "policy_type": "privacy_policy"}],
        consent_signals=[{"mechanism_type": "cmp", "cmp_vendor": "Cookiebot",
                           "has_reject_all": True, "has_granular_choices": True,
                           "evidence": {"accept_interaction": "clicked", "reject_interaction": "page_unreachable"}}],
    )
    findings = evaluate_consent_rules(evidence)
    r010 = next(f for f in findings if f.rule_id == "R-010")
    assert "post_reject" in r010.summary
    assert "post_accept" not in r010.summary
