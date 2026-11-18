"""The withdrawal test records the control it reopened as TEXT (a string, or None), not as a dict. `_reconcile_banner_presence` reads it and must accept both,
so a scan whose withdrawal test reopened a preferences control is never failed by it (it raised AttributeError: 'str' object has no attribute 'get')."""

from app.scanner import crawler
from app.scanner.consent_signal_detector import detect_consent_signal
from app.scanner.page_parser import ParsedScript

ONETRUST = ParsedScript(src="https://cdn.cookielaw.org/scripttemplates/otSDKStub.js", inline_snippet=None, executes=True)


def _banner_without_choices():
    signal = detect_consent_signal(scripts=[ONETRUST], visible_text="", detected_global_vars=[], control_text="accept all reject all")
    assert signal.mechanism_type == "cmp" and signal.has_granular_choices is not True
    return signal


def test_a_reopen_control_recorded_as_a_string_does_not_fail_the_scan_and_counts_as_choices():
    signal = _banner_without_choices()
    crawler._reconcile_banner_presence(signal, "not_found", "not_found", {"reopen_control": "Cookie Preferences", "withdraw_control": "Revoke all"})
    assert signal.has_granular_choices is True and signal.evidence["granular_control_seen_via"] == "withdrawal_test"


def test_a_reopen_control_that_is_not_a_preferences_control_changes_nothing():
    signal = _banner_without_choices()
    crawler._reconcile_banner_presence(signal, "not_found", "not_found", {"reopen_control": "Contact us"})
    assert signal.has_granular_choices is not True and "granular_control_seen_via" not in (signal.evidence or {})


def test_a_dict_shaped_reopen_control_is_still_read():
    signal = _banner_without_choices()
    crawler._reconcile_banner_presence(signal, "not_found", "not_found", {"reopen_control": {"text": "Cookie Preferences"}})
    assert signal.has_granular_choices is True


def test_the_banner_text_that_is_given_extra_time_is_accept_or_reject_wording_without_a_preferences_control():
    assert crawler._banner_without_choices("accept all reject all") is True
    assert crawler._banner_without_choices("accept all manage preferences") is False
    assert crawler._banner_without_choices("subscribe to our newsletter") is False
    assert crawler._banner_without_choices("") is False and crawler._banner_without_choices(None) is False


def test_no_reopen_control_and_no_withdrawal_result_are_both_fine():
    for withdrawal in (None, {}, {"reopen_control": None}):
        signal = _banner_without_choices()
        crawler._reconcile_banner_presence(signal, "not_found", "not_found", withdrawal)
        assert signal.has_granular_choices is not True
