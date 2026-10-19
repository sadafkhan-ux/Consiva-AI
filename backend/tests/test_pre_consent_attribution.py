"""R-001 / R-008 name WHERE the pre-consent items were seen.

Seen on gignaati.com: every pre-consent tracker came from masterclass.gignaati.com, but the finding read as if it
were the home page. Attribution only states what the scan recorded -- no page recorded, no page named.
"""

from app.rules.consent_rules import evaluate_consent_rules


def _evidence(trackers, cookies=()):
    return {
        "cookies": list(cookies), "trackers": trackers, "forms": [], "pages": [],
        "policies": [{"policy_type": "privacy_policy"}, {"policy_type": "cookie_policy"}],
        "consent_signals": [{"mechanism_type": "cmp", "cmp_vendor": "Consiva", "has_reject_all": True,
                             "has_granular_choices": True, "evidence": {}}],
    }


def _t(i, category, states, page_url=None):
    return {"id": i, "script_src": f"https://x{i}.example/t.js", "vendor": None, "category": category,
            "source": "rule", "consent_states": states, "page_url": page_url}


def _by_rule(findings):
    return {f.rule_id: f for f in findings}


def test_r001_names_the_page_hosts_the_trackers_were_seen_on():
    found = _by_rule(evaluate_consent_rules(_evidence([
        _t("a", "marketing", ["pre_consent"], "https://masterclass.gignaati.com/"),
        _t("b", "analytics", ["pre_consent"], "https://masterclass.gignaati.com/x"),
        _t("c", "analytics", ["post_accept"], "https://gignaati.com/"),
    ])))

    assert "Observed on: masterclass.gignaati.com." in found["R-001"].summary
    assert "gignaati.com/" not in found["R-001"].summary.replace("masterclass.gignaati.com", "")


def test_r001_does_not_guess_when_no_page_was_recorded():
    found = _by_rule(evaluate_consent_rules(_evidence([_t("a", "marketing", ["pre_consent"])])))

    assert "Observed on" not in found["R-001"].summary
    assert found["R-001"].risk_level == "high"          # attribution never changes the severity


def test_a_tracker_seen_only_after_consent_is_not_r001():
    found = _by_rule(evaluate_consent_rules(_evidence([_t("a", "marketing", ["post_accept"], "https://a.example/")])))

    assert "R-001" not in found


def test_unclassified_pre_consent_items_are_flagged_for_review_not_called_clean_or_violations():
    found = _by_rule(evaluate_consent_rules(_evidence([
        _t("a", None, ["pre_consent"], "https://player.vimeo.com/"),
        _t("b", None, ["post_accept"]),
    ])))

    r008 = found["R-008"]
    assert "1 of them fired BEFORE any consent interaction" in r008.summary
    assert "a reviewer must decide" in r008.summary
    assert r008.confidence == "low" and "R-001" not in found     # uncertain: reviewed, not asserted


def test_unclassified_items_not_seen_pre_consent_get_no_pre_consent_claim():
    found = _by_rule(evaluate_consent_rules(_evidence([_t("a", None, ["post_accept"])])))

    assert "BEFORE" not in found["R-008"].summary


def test_cookie_attribution_uses_only_pre_consent_observations():
    cookies = [{"id": "c1", "name": "_fbp", "domain": "x", "category": "marketing", "vendor": None,
                "is_first_party": True, "source": "rule", "consent_states": ["pre_consent", "post_accept"],
                "observations": [
                    {"consent_state": "pre_consent", "page_url": "https://masterclass.gignaati.com/"},
                    {"consent_state": "post_accept", "page_url": "https://gignaati.com/"},
                ]}]
    found = _by_rule(evaluate_consent_rules(_evidence([], cookies)))

    assert "Observed on: masterclass.gignaati.com." in found["R-001"].summary


# ── which page Accept/Reject were tried on ───────────────────────────────────────

def _r009_evidence(interaction_page=None):
    ev = _evidence([])
    ev["consent_signals"][0]["evidence"] = {
        "accept_interaction": "cmp_not_automatable", "reject_interaction": "cmp_not_automatable",
        **({"interaction_page_url": interaction_page} if interaction_page else {}),
    }
    return ev


def test_r009_says_which_page_the_interaction_was_tried_on():
    """corp.gignaati.com has no banner on its entry page; the Consiva SDK was detected on other pages of the site."""
    found = _by_rule(evaluate_consent_rules(_r009_evidence("https://corp.gignaati.com/")))

    assert "only tried on https://corp.gignaati.com/" in found["R-009"].summary
    assert found["R-009"].confidence == "low", "still a scanner limitation routed to human review, not a violation"
    assert found["R-009"].risk_level == "medium"


def test_r009_is_unchanged_when_the_page_is_not_recorded():
    found = _by_rule(evaluate_consent_rules(_r009_evidence()))

    assert "only tried on" not in found["R-009"].summary and "R-009" in found
