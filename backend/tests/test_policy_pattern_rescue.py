"""The fixed-pattern second look at the policy text: a statement a model can miss is still found when it is plainly on the page, so a check does not flip
between scans of an unchanged page. The pattern only returns exact slices of the page that carry both the topic and a concrete detail."""

import pytest

from app.policy import analysis
from app.policy.analysis import (
    PolicyDocument,
    PolicyExtraction,
    PolicyReqResult,
    analyse,
    locate_by_pattern,
    verify_quote,
)


def doc(text, url="https://x.test/privacy"):
    return PolicyDocument(url, "privacy_policy", text, "crawl")


def model_says(*found):
    """An extraction in which the model found exactly these (id, quote) and missed everything else."""
    return PolicyExtraction(results=[PolicyReqResult(id=i, found=True, quote=q, document=1) for i, q in found])


def row(out, rid):
    return next(r for r in out["requirements"] if r["id"] == rid)


FILLER = "We process your personal data to provide our services and keep them secure. " * 6
DPO_TEXT = FILLER + "Our Data Protection Officer can be reached at dpo@x.test or on +91 98765 43210 for any question about your data. " + FILLER


class TestFindsWhatIsPlainlyThere:
    def test_a_missed_officer_contact_is_found_and_is_an_exact_slice_of_the_page(self):
        d = doc(DPO_TEXT)
        out = analyse(model_says(), [d], None, False)
        r = row(out, "dpo_contact")
        assert r["status"] == "verified" and r["found_by"] == "pattern" and "fixed pattern" in r["detail"]
        assert verify_quote(r["quote"], d.text) and "dpo@x.test" in r["quote"]
        assert r["contacts"]["emails"] == ["dpo@x.test"] and r["source_url"] == d.url

    def test_a_missed_means_of_making_a_rights_request(self):
        d = doc(FILLER + "To exercise your rights, write to privacy@x.test and we will answer. " + FILLER)
        r = row(analyse(model_says(), [d], None, False), "rights_means")
        assert r["status"] == "verified" and r["found_by"] == "pattern" and "privacy@x.test" in r["quote"]

    def test_a_missed_grievance_contact_and_period_in_one_sentence_each(self):
        d = doc(FILLER + "Send any complaint to our grievance officer at grievance@x.test. We will resolve every grievance within 30 days. " + FILLER)
        out = analyse(model_says(), [d], None, False)
        assert row(out, "grievance_contact")["status"] == "verified"
        t = row(out, "grievance_timeline")
        assert t["status"] == "verified" and t["response_days"] == 30.0 and "grievance within 30 days" in t["quote"]

    def test_a_period_above_ninety_days_is_a_partial_not_a_pass(self):
        d = doc(FILLER + "We will resolve every grievance within 120 days of receiving it. " + FILLER)
        t = row(analyse(model_says(), [d], None, False), "grievance_timeline")
        assert t["status"] == "partial" and t["response_days"] == 120.0


class TestFindsNothingThatIsNotThere:
    @pytest.mark.parametrize("text", [
        FILLER + "Our Data Protection Officer oversees privacy matters across the company. " + FILLER,                 # the role, no way to reach it
        FILLER + "You can reach us at help@x.test for anything at all. " + FILLER,                                  # a contact, no role
        FILLER + "We delete your account data within 30 days of closing the account. " + FILLER,                    # a period, not about grievances
        FILLER,
    ])
    def test_a_role_without_a_contact_or_a_contact_without_a_role_or_an_unrelated_period_is_not_found(self, text):
        out = analyse(model_says(), [doc(text)], None, False)
        assert row(out, "dpo_contact")["status"] == "not_found"
        assert row(out, "grievance_timeline")["status"] == "not_found"

    def test_it_only_returns_exact_slices_of_the_page(self):
        d = doc(DPO_TEXT)
        quote, found_in = locate_by_pattern("dpo_contact", [d])
        assert quote in d.text.replace("  ", " ") or verify_quote(quote, d.text)
        assert found_in is d
        assert len(quote) <= analysis.MAX_QUOTE_CHARS

    def test_a_requirement_without_a_pattern_is_never_found_this_way(self):
        assert locate_by_pattern("security", [doc(DPO_TEXT + " We use encryption everywhere. ")]) is None


class TestTheModelCannotChangeTheOutcome:
    def test_the_same_page_gives_the_same_statuses_whether_or_not_the_model_found_the_passage(self):
        d = doc(DPO_TEXT)
        quote = "Our Data Protection Officer can be reached at dpo@x.test or on +91 98765 43210 for any question about your data."
        found = analyse(model_says(("dpo_contact", quote)), [d], None, False)
        missed = analyse(model_says(), [d], None, False)
        assert row(found, "dpo_contact")["status"] == row(missed, "dpo_contact")["status"] == "verified"
        assert "found_by" not in row(found, "dpo_contact")            # a passage the model offered is never relabelled

    def test_a_passage_the_model_found_is_never_replaced_by_the_pattern(self):
        d = doc(DPO_TEXT + " For privacy-related questions contact privacy@x.test. ")
        offered = "For privacy-related questions contact privacy@x.test."
        r = row(analyse(model_says(("dpo_contact", offered)), [d], None, False), "dpo_contact")
        assert r["quote"] == offered and "found_by" not in r

    def test_a_made_up_quotation_is_still_rejected_and_the_pattern_then_decides_on_the_page_alone(self):
        d = doc(FILLER)
        out = analyse(model_says(("dpo_contact", "Contact our officer at invented@x.test for all questions.")), [d], None, False)
        assert row(out, "dpo_contact")["status"] == "not_found" and out["summary"]["quotes_rejected"] == 1


class TestStoredResultsFromAnOlderVersionAreNotReused:
    def test_the_hash_includes_the_analysis_version(self, monkeypatch):
        before = analysis.input_hash("s", "u", None, False, "m")
        monkeypatch.setattr(analysis, "ANALYSIS_VERSION", analysis.ANALYSIS_VERSION + 1)
        assert analysis.input_hash("s", "u", None, False, "m") != before


class TestTheScoreFollowsTheStablePolicyReading:
    def test_the_dpdp_check_is_met_whether_the_model_found_the_contact_or_missed_it(self):
        from datetime import date

        from app.scoring.dpdp_score import assess
        from tests.test_dpdp_score import evidence

        d = doc(DPO_TEXT)
        quote = "Our Data Protection Officer can be reached at dpo@x.test or on +91 98765 43210 for any question about your data."
        statuses = []
        for extraction in (model_says(("dpo_contact", quote)), model_says()):
            ev = evidence()
            ev["policy_analysis"] = {"status": "analysed", "reason": None, **analyse(extraction, [d], None, False)}
            s = assess(ev, date(2026, 10, 21))
            statuses.append(next(r for r in s.requirements if r.id == "DPDP-R9").status)
        assert statuses == ["met", "met"]
