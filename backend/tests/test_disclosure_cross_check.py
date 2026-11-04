"""What the scan observed the site collecting, against what the policy says (scoring/disclosure.py): checks 2 to 5 of the ten-check engine.

The base is a recorded scan where every check passes (tests/fixtures/ten_check_golden.json, case 5). Each test changes the observed collection or the verified policy passage and
asserts which check moves, and that the result names what the policy does not cover.
"""

import copy
import json

from app.scoring.ten_checks import assess_ten_checks
from app.scoring.ten_inputs import (
    build_scan_pages,  # noqa: F401  (documents where scan_pages come from)
)
from tests.test_ten_checks_parity import GOLDEN, _inputs

CASE = GOLDEN["cases"][5]
GA = {"id": "t1", "script_src": "https://www.google-analytics.com/g/collect", "vendor": "Google Analytics", "category": "analytics", "source": "rule",
      "consent_states": ["pre_consent", "post_accept"], "page_url": "https://x.test/"}
GTM = {**GA, "id": "t2", "script_src": "https://www.googletagmanager.com/gtm.js", "vendor": "Google Tag Manager"}
PIXEL = {**GA, "id": "t3", "script_src": "https://connect.facebook.net/en_US/fbevents.js", "vendor": "Meta Pixel", "category": "marketing"}
GA_COOKIE = {"id": "c1", "name": "_ga", "domain": "x.test", "category": "analytics", "vendor": "Google Analytics", "is_session": False, "lifetime_days": 400, "consent_states": ["post_accept"]}
FORM = {"id": "f1", "selector": "form#c", "purpose_guess": "contact_or_support", "fields": [{"name": "email", "field_type": "email", "required": True}, {"name": "phone", "field_type": "tel", "required": False}]}


def _run(trackers=(), cookies=(), forms=None, quotes=None, cross_check=True, drop=()):
    args = list(_inputs(CASE))
    ev = copy.deepcopy(args[0])
    ev["trackers"] = list(trackers)
    ev["cookies"] = list(cookies)
    if forms is not None:
        ev["forms"] = forms
    args[0] = ev
    pa = copy.deepcopy(args[1])
    for r in pa["requirements"]:
        if r["id"] in (quotes or {}):
            if quotes[r["id"]] is None:          # the policy was read and says nothing about it
                r.update(status="not_found", quote="", source_url=None)
            else:
                r["quote"] = quotes[r["id"]]
    pa["requirements"] = [r for r in pa["requirements"] if r["id"] not in drop]
    args[1] = pa
    out = assess_ten_checks(*args, cross_check=cross_check)
    return {c["id"]: c for c in out["checks"]}, out


FORM_ONLY_PURPOSE = {"notice_purposes": "We use the details you send through our contact form to reply to your enquiry."}


def test_the_base_scan_is_untouched_by_the_comparison():
    checks, _ = _run(forms=[], trackers=[], cookies=[])
    assert all(checks[i]["result"] == "Passed" for i in (2, 3, 4, 5))


def test_purposes_for_form_submissions_only_do_not_cover_analytics_and_marketing():
    checks, out = _run(trackers=[GA, GTM, PIXEL], quotes=FORM_ONLY_PURPOSE)
    c3 = checks[3]
    assert c3["result"] == "Partially Passed" and c3["marks_text"] == "0.5 / 1"
    assert "Google Analytics" in c3["explanation"] and "Meta Pixel" in c3["explanation"]
    assert "does not mention it" in c3["explanation"]
    assert "Purpose not stated" in c3["coverage"]
    assert out["score"] < 100


def test_a_purpose_that_names_analytics_and_advertising_still_passes():
    q = {"notice_purposes": "We use cookies for analytics and measuring how visitors use the site, and for advertising and marketing."}
    checks, _ = _run(trackers=[GA, PIXEL], forms=[], quotes=q)
    assert checks[3]["result"] == "Passed"


def test_each_missing_purpose_is_named_separately():
    q = {"notice_purposes": "We use cookies to measure how visitors use the site (analytics)."}
    checks, _ = _run(trackers=[GA, PIXEL], forms=[], quotes=q)
    c3 = checks[3]
    assert c3["result"] == "Partially Passed"
    assert "Meta Pixel" in c3["explanation"] and "Google Analytics" not in c3["explanation"].split("observed")[1].split("but")[0]


def test_a_form_without_a_stated_purpose_is_named():
    q = {"notice_purposes": "We use cookies to measure how visitors use the site (analytics)."}
    checks, _ = _run(trackers=[GA], forms=[FORM], quotes={**q, "notice_purposes": "We may use data for business operations."})
    assert checks[3]["result"] == "Partially Passed"
    assert "a form collecting email and phone" in checks[3]["explanation"]


def test_a_failed_purpose_check_names_what_was_observed():
    checks, _ = _run(trackers=[GA], quotes={"notice_purposes": None})
    c3 = checks[3]
    assert c3["result"] == "Failed" and "Google Analytics" in c3["explanation"] and "a purpose should be stated" in c3["explanation"]


def test_check_2_names_the_data_a_form_asks_for_that_the_policy_does_not_describe():
    q = {"notice_data_itemised": "We collect your postal address and your date of birth."}
    checks, _ = _run(forms=[FORM], quotes=q)
    c2 = checks[2]
    assert c2["result"] == "Partially Passed" and "email and phone" in c2["explanation"]


def test_check_2_accepts_a_general_description_of_what_you_provide():
    q = {"notice_data_itemised": "We collect the contact details and the information you provide to us."}
    checks, _ = _run(forms=[FORM], quotes=q)
    assert checks[2]["result"] == "Passed"


def test_check_4_names_a_tracker_the_sharing_passage_does_not_cover():
    q = {"data_sharing": "We share personal data with payment processors and hosting providers."}
    checks, _ = _run(trackers=[GA, PIXEL], quotes=q)
    c4 = checks[4]
    assert c4["result"] == "Partially Passed" and "Meta Pixel" in c4["explanation"] and "Google Analytics" in c4["explanation"]


def test_check_4_passes_when_the_recipients_include_what_was_observed():
    checks, _ = _run(trackers=[GA])
    assert checks[4]["result"] == "Passed"          # the base sharing passage names analytics providers


def test_check_5_names_persistent_cookies_with_no_stated_retention():
    checks, _ = _run(cookies=[GA_COOKIE])
    c5 = checks[5]
    assert c5["result"] == "Partially Passed" and "_ga (400 days)" in c5["explanation"] and c5["coverage"] == "Cookie retention not stated"


def test_check_5_passes_when_the_cookie_policy_states_the_cookie_lifetime():
    pa_quote = {"retention": "We keep personal information for 24 months. Analytics cookies expire after 13 months."}
    checks, _ = _run(cookies=[GA_COOKIE], quotes=pa_quote)
    assert checks[5]["result"] == "Passed"


def test_an_unassessed_check_is_left_alone():
    checks, _ = _run(trackers=[GA], forms=[FORM], drop=("notice_purposes", "retention", "data_sharing", "notice_data_itemised"), cross_check=True)
    for i in (2, 3, 4, 5):
        assert checks[i]["result"] in ("Failed", "Not Assessed")


def test_the_comparison_is_off_for_the_recorded_1_0_0_results():
    on, _ = _run(trackers=[GA, PIXEL], quotes=FORM_ONLY_PURPOSE)
    off, _ = _run(trackers=[GA, PIXEL], quotes=FORM_ONLY_PURPOSE, cross_check=False)
    assert on[3]["result"] == "Partially Passed" and off[3]["result"] == "Passed"


def test_the_same_inputs_give_the_same_result_twice():
    a = _run(trackers=[GA, GTM, PIXEL], cookies=[GA_COOKIE], forms=[FORM], quotes=FORM_ONLY_PURPOSE)[1]
    b = _run(trackers=[GA, GTM, PIXEL], cookies=[GA_COOKIE], forms=[FORM], quotes=FORM_ONLY_PURPOSE)[1]
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


# ── check 7: the parts of the DPO or grievance contact (advisory; no mark depends on them) ──
from app.scoring.disclosure import contact_elements


def test_contact_elements_name_what_was_found_and_what_was_not_detected():
    found, missing = contact_elements("Privacy Office — dpo@digitalanumati.com General Support — info@digitalanumati.com")
    assert found == ["email", "designation"] and "name" in missing and "phone" in missing
    found, missing = contact_elements("Grievance Officer: Mr Rahul Sharma, Data Protection Officer, g@x.in, +91 98765 43210, Registered office: Mumbai 400001. We respond within 30 days.")
    assert missing == []


def test_check_7_gains_an_advisory_and_keeps_its_mark():
    quotes = {"grievance_contact": "Write to our Grievance Officer at grievance@x.test", "grievance_timeline": "We respond within 30 days."}
    on, _ = _run(quotes=quotes)
    off, _ = _run(quotes=quotes, cross_check=False)
    assert on[7]["result"] == off[7]["result"] and on[7]["marks_text"] == off[7]["marks_text"]      # no mark moves
    assert "Contact details in the passages found" in on[7]["explanation"] and "Not detected" in on[7]["explanation"]
    assert "not a name or designation" in on[7]["explanation"]
    assert "Contact details in the passages found" not in off[7]["explanation"]


def test_check_4_does_not_say_twice_what_the_scan_observed():
    q = {"data_sharing": "We may share information with trusted service providers who assist us."}
    checks, _ = _run(trackers=[GA, PIXEL], quotes=q)
    assert checks[4]["explanation"].count("The scan observed") == 1


# ── check 9: what was seen in which consent state ──
def _consent_run(accept=True, reject=True, trackers=(), cookies=()):
    args = list(_inputs(CASE))
    ev = copy.deepcopy(args[0])
    ev["trackers"], ev["cookies"] = list(trackers), list(cookies)
    args[0] = ev
    visual = copy.deepcopy(args[3])
    known = [sum(1 for t in trackers if s in t["consent_states"]) for s in ("pre_consent", "post_accept", "post_reject")]
    visual["consent"] = {"accept_tested": accept, "reject_tested": reject, "known": known}
    args[3] = visual
    return {c["id"]: c for c in assess_ten_checks(*args)["checks"]}[9]


def test_check_9_names_what_fired_in_each_state_with_the_cookie_time():
    accepted = {**GA, "consent_states": ["post_accept"]}
    gtm = {**GTM, "consent_states": ["post_accept"]}
    cookie = {**GA_COOKIE, "consent_states": ["post_accept"], "observations": [{"consent_state": "post_accept", "observed_at": "2026-11-03T06:10:17.710832+00:00", "method": "single_page_load"}]}
    c9 = _consent_run(trackers=[accepted, gtm], cookies=[cookie])
    e = c9["evidence"]
    assert "before consent: none" in e and "after Reject: none" in e
    assert "after Accept: requests to Google Analytics and Google Tag Manager; cookies _ga, first seen 2026-11-03 06:10:17 UTC" in e
    assert c9["result"] == "Passed"                      # the names are evidence, not a different rule


def test_check_9_says_untested_states_are_untested_never_clean():
    c9 = _consent_run(accept=True, reject=False, trackers=[{**GA, "consent_states": ["post_accept"]}])
    assert "after Reject: not tested" in c9["evidence"] and "after Reject: none" not in c9["evidence"]


def test_check_9_names_what_fired_before_consent():
    c9 = _consent_run(trackers=[{**PIXEL, "consent_states": ["pre_consent", "post_accept"]}])
    assert "before consent: requests to Meta Pixel" in c9["evidence"]
    assert c9["result"] in ("Failed", "Partially Passed")
