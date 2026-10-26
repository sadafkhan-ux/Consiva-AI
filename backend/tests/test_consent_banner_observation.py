"""A consent tool's SCRIPT is not a consent banner.

On an 11-site scan, cnn.com, bbc.com, lemonde.fr, spiegel.de and hindustantimes.com all reported rule R-005 ("no
equally prominent reject-all option") and R-009 ("Reject could not be automated"). A probe showed why for CNN, BBC
and Le Monde: the consent tool was loaded, but no banner was shown to a visitor in India, so there was nothing to
click and no reject option to find. The scan never saw a banner, yet asserted that it lacked a reject button.

Where a banner WAS shown and lacks a first-layer reject (the Guardian's Sourcepoint message: "Yes, I accept" /
"Manage cookies"), R-005 is still reported.
"""

from app.rules.consent_rules import evaluate_consent_rules
from app.scanner.consent_signal_detector import detect_consent_signal
from app.scanner.page_parser import ParsedScript

ONETRUST = ParsedScript(src="https://cdn.cookielaw.org/scripttemplates/otSDKStub.js", inline_snippet=None, executes=True)


def _detect(control_text="", scripts=(ONETRUST,)):
    return detect_consent_signal(
        scripts=list(scripts), visible_text="", detected_global_vars=[], control_text=control_text,
    )


def test_a_cmp_script_with_no_banner_wording_leaves_reject_and_granular_unknown():
    signal = _detect("")

    assert signal.mechanism_type == "cmp" and signal.cmp_vendor == "OneTrust"
    assert signal.has_reject_all is None and signal.has_granular_choices is None
    assert signal.evidence["banner_ui_observed"] is False


def test_a_footer_cookie_link_alone_is_not_evidence_that_a_banner_was_displayed():
    signal = _detect("manage cookies")          # a footer link: cnn.com and bbc.com have one and showed no banner

    assert signal.evidence["banner_ui_observed"] is False


def test_a_banner_with_reject_all_is_recorded_as_having_it():
    signal = _detect("accept all reject all cookie settings")

    assert signal.has_reject_all is True and signal.evidence["banner_ui_observed"] is True


def test_accept_wording_on_a_control_counts_as_a_displayed_banner():
    assert _detect("accept cookies manage cookies").evidence["banner_ui_observed"] is True


def test_the_keyword_only_path_is_unchanged():
    signal = _detect("accept all manage preferences", scripts=())

    assert signal.mechanism_type == "banner" and signal.has_reject_all is False


def _evidence(signal, accept="cmp_not_automatable", reject="cmp_not_automatable"):
    return {
        "cookies": [], "trackers": [], "forms": [], "policies": [{"policy_type": "privacy_policy"}],
        "consent_signals": [{
            "mechanism_type": signal.mechanism_type, "cmp_vendor": signal.cmp_vendor,
            "has_reject_all": signal.has_reject_all, "has_granular_choices": signal.has_granular_choices,
            "evidence": {**signal.evidence, "accept_interaction": accept, "reject_interaction": reject},
        }],
    }


def _ids(findings):
    return {f.rule_id: f for f in findings}


def test_r005_does_not_fire_when_no_banner_was_ever_displayed():
    assert "R-005" not in _ids(evaluate_consent_rules(_evidence(_detect(""))))


def test_r005_does_not_fire_for_a_footer_link_when_nothing_was_clicked():
    found = _ids(evaluate_consent_rules(_evidence(_detect("manage cookies"))))

    assert "R-005" not in found


def test_r005_still_fires_for_a_banner_whose_accept_was_clicked_but_that_has_no_reject():
    """The Guardian: Sourcepoint's first layer is "Yes, I accept" / "Manage cookies". Accept was clicked, so the
    banner was displayed, and it has no first-layer reject."""
    from app.scanner import crawler

    signal = _detect("yes, i accept manage cookies")
    crawler._reconcile_banner_presence(signal, "clicked", "not_found")           # what the scan does once the Accept click has proved the banner was on screen
    found = _ids(evaluate_consent_rules(_evidence(signal, accept="clicked")))

    assert "R-005" in found and found["R-005"].risk_level == "medium"


def test_r005_still_fires_when_the_banner_wording_itself_was_seen_without_a_reject():
    found = _ids(evaluate_consent_rules(_evidence(_detect("accept all manage preferences"))))

    assert "R-005" in found


def test_r009_says_the_banner_was_not_displayed_instead_of_blaming_the_banner():
    found = _ids(evaluate_consent_rules(_evidence(_detect(""))))

    assert "R-009" in found, "the limitation is still reported, never silently dropped"
    summary = found["R-009"].summary
    assert "no consent banner was displayed" in summary and "certain regions" in summary
    assert found["R-009"].confidence == "low" and found["R-009"].risk_level == "medium"


def test_r009_keeps_its_original_wording_when_a_banner_was_displayed_but_not_operable():
    found = _ids(evaluate_consent_rules(_evidence(_detect("accept all manage preferences"))))

    assert "could not be automated" in found["R-009"].summary


def test_older_scans_without_the_observation_flag_read_as_unknown_not_as_not_displayed():
    ev = _evidence(_detect(""))
    ev["consent_signals"][0]["evidence"].pop("banner_ui_observed", None)
    ev["consent_signals"][0]["has_reject_all"] = False

    found = _ids(evaluate_consent_rules(ev))
    assert "could not be automated" in found["R-009"].summary and "R-005" in found


# A footer "manage cookies" link is on pages whose banner is not showing. It must not turn into "the banner has no Reject option" (False) or
# "the banner offers per-purpose choices" (True): neither was observed, because no banner was.
import pytest


@pytest.mark.parametrize("footer_text", ["manage cookies", "cookie settings", "privacy preferences", "manage preferences cookie preferences"])
def test_footer_wording_alone_leaves_reject_and_granular_unknown_for_a_cmp(footer_text):
    signal = _detect(footer_text)

    assert signal.mechanism_type == "cmp" and signal.evidence["banner_ui_observed"] is False
    assert signal.has_reject_all is None, "a banner that was never seen has not been observed to lack a Reject option"
    assert signal.has_granular_choices is None, "a footer link is not per-purpose choices offered in a banner"


def test_once_banner_wording_is_seen_both_are_recorded_as_before():
    signal = _detect("accept all manage cookies")
    assert signal.evidence["banner_ui_observed"] is True
    assert signal.has_reject_all is False and signal.has_granular_choices is True


def test_a_click_restores_what_the_banner_text_said_and_no_click_does_not():
    from app.scanner import crawler

    for statuses, restored in [(("clicked", "not_found"), True), (("not_found", "clicked"), True), (("cmp_not_automatable", "cmp_not_automatable"), False), (("click_failed", "not_found"), False)]:
        signal = _detect("manage cookies")
        crawler._reconcile_banner_presence(signal, *statuses)
        assert (signal.has_reject_all is False and signal.has_granular_choices is True) == restored, statuses


# The same rule for EVERY consent tool the scanner recognises, not one vendor: a footer cookie link on a page whose tool showed no banner is never "no reject-all".
from app.rules.tracker_catalog import CMP_CATALOG


@pytest.mark.parametrize("cmp", CMP_CATALOG, ids=lambda c: c.cmp_vendor)
def test_no_recognised_consent_tool_turns_a_footer_link_into_a_verdict_on_a_banner(cmp):
    script = ParsedScript(src=f"https://{cmp.domain_substrings[0]}/consent.js", inline_snippet=None, executes=True)
    for footer in ("manage cookies", "cookie preferences", "privacy preferences"):
        signal = _detect(footer, scripts=(script,))
        assert signal.mechanism_type == "cmp" and signal.cmp_vendor == cmp.cmp_vendor
        assert signal.evidence["banner_ui_observed"] is False
        assert signal.has_reject_all is None and signal.has_granular_choices is None, (cmp.cmp_vendor, footer)


@pytest.mark.parametrize("cmp", CMP_CATALOG, ids=lambda c: c.cmp_vendor)
def test_every_recognised_consent_tool_with_a_displayed_banner_still_records_what_it_offers(cmp):
    script = ParsedScript(src=f"https://{cmp.domain_substrings[0]}/consent.js", inline_snippet=None, executes=True)
    shown = _detect("accept all manage preferences", scripts=(script,))
    assert shown.evidence["banner_ui_observed"] is True
    assert shown.has_reject_all is False and shown.has_granular_choices is True
    with_reject = _detect("accept all reject all manage preferences", scripts=(script,))
    assert with_reject.has_reject_all is True
