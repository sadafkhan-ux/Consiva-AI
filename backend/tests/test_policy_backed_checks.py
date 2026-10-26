"""Checks answered by what the privacy policy says (DPDP-R9, R14A, R14B), and the two that a policy can only describe (S6, S7).

The policy analysis already reads the policy once per analysis and returns quotations checked word for word against the page. These checks use that result: no
extra model call. The rules under test: verified evidence is Met; a statement without the concrete detail is Partially Met; anything NOT found stays Unable to
Verify (never a failure, because the site may publish it elsewhere); a description of security or breach practice is never proof that the practice exists."""

import asyncio
import copy
import json
import random
from dataclasses import asdict
from datetime import date

import pytest

from app.scoring import dpdp_score as ds
from app.scoring.dpdp_score import assess
from tests.test_dpdp_score import evidence

AS_OF = date(2026, 10, 21)
URL = "https://x.test/privacy"


def row(rid, status, quote="", **extra):
    return {"id": rid, "status": status, "quote": quote, "source_url": URL if status in ("verified", "partial") else None,
            "detail": extra.pop("detail", "Quoted from the policy."), "contacts": extra.pop("contacts", None), "response_days": extra.pop("response_days", None), **extra}


def analysis(*rows, status="analysed", reason=None):
    return {"status": status, "reason": reason, "requirements": list(rows)}


def scored(policy, **kw):
    ev = evidence(**kw)
    if policy is not None:
        ev["policy_analysis"] = policy
    return assess(ev, AS_OF)


def res(s, rid):
    return next(r for r in s.requirements if r.id == rid)


DPO_OK = row("dpo_contact", "verified", "Contact our Data Protection Officer at dpo@x.test for any question about your data.", contacts={"emails": ["dpo@x.test"]})
RIGHTS_OK = row("rights_means", "verified", "To exercise your rights, write to privacy@x.test or use the form at https://x.test/rights.", contacts={"emails": ["privacy@x.test"]})
GRIEVANCE_OK = row("grievance_contact", "verified", "Grievances go to the officer at grievance@x.test.", contacts={"emails": ["grievance@x.test"]})
PERIOD_OK = row("grievance_timeline", "verified", "We respond to grievances within 30 days.", response_days=30.0)


class TestEachCheckFromThePolicy:
    def test_a_verified_contact_is_met_with_the_quotation_as_evidence(self):
        r = res(scored(analysis(DPO_OK)), "DPDP-R9")
        assert (r.status, r.points_earned, r.method, r.check_type) == ("met", 2.0, "policy_text", "policy")
        assert "dpo@x.test" in r.observations[0] and URL in r.observations[0]
        assert r.pages == [URL] and r.sufficient is True and "word for word" in r.sufficiency_reason and r.needed_evidence == []

    def test_a_role_without_a_way_to_reach_it_is_partially_met(self):
        r = res(scored(analysis(row("dpo_contact", "partial", "Our Data Protection Officer oversees privacy matters.", detail="The role is mentioned but no email, phone number or form is given next to it."))), "DPDP-R9")
        assert (r.status, r.points_earned) == ("partially_met", 1.0)

    def test_means_of_exercising_rights(self):
        assert res(scored(analysis(RIGHTS_OK)), "DPDP-R14A").status == "met"
        assert res(scored(analysis(row("rights_means", "partial", "You have rights over your data.", detail="Rights are mentioned but no concrete means"))), "DPDP-R14A").status == "partially_met"

    @pytest.mark.parametrize("rows,expected", [
        ((GRIEVANCE_OK, PERIOD_OK), "met"),
        ((GRIEVANCE_OK, row("grievance_timeline", "partial", "We respond to grievances within 120 days.", response_days=120.0)), "partially_met"),
        ((GRIEVANCE_OK, row("grievance_timeline", "partial", "We handle grievances promptly.")), "unable_to_verify"),
        ((GRIEVANCE_OK, row("grievance_timeline", "not_found")), "unable_to_verify"),
        ((GRIEVANCE_OK,), "unable_to_verify"),
        ((row("grievance_contact", "partial", "Grievances are handled by our team.", detail="no email"), PERIOD_OK), "partially_met"),
        ((row("grievance_contact", "not_found"), PERIOD_OK), "unable_to_verify"),
        ((PERIOD_OK,), "unable_to_verify"),
    ])
    def test_the_grievance_system_needs_a_reachable_contact_and_a_period_within_90_days(self, rows, expected):
        assert res(scored(analysis(*rows)), "DPDP-R14B").status == expected

    def test_a_response_period_that_is_not_about_grievances_is_not_taken_as_one(self):
        about_erasure = row("grievance_timeline", "verified", "We delete your account data within 30 days of closing the account.", response_days=30.0)
        r = res(scored(analysis(GRIEVANCE_OK, about_erasure)), "DPDP-R14B")
        assert r.status == "unable_to_verify" and any("not about grievances" in o for o in r.observations)

    def test_a_period_above_90_days_is_stated_as_such(self):
        r = res(scored(analysis(GRIEVANCE_OK, row("grievance_timeline", "partial", "We respond to grievances within 120 days.", response_days=120.0))), "DPDP-R14B")
        assert any("120 days" in o and "at most 90" in o for o in r.observations)


class TestAbsenceIsNeverAFailure:
    ABSENT = analysis(*[row(i, "not_found") for i in ("dpo_contact", "rights_means", "grievance_contact", "grievance_timeline", "security", "breach_response")])

    @pytest.mark.parametrize("policy", [None, ABSENT, analysis(status="unavailable", reason="No privacy or cookie policy page was found on the scanned pages, so there was nothing to read.")])
    @pytest.mark.parametrize("rid", ["DPDP-R9", "DPDP-R14A", "DPDP-R14B", "DPDP-S6", "DPDP-S7"])
    def test_not_found_stays_unable_to_verify_and_says_what_is_needed(self, policy, rid):
        r = res(scored(policy), rid)
        assert r.status == "unable_to_verify" and r.counts_in_score is False and r.points_earned == 0.0
        assert r.needed_evidence, "an unverified check must say what evidence would settle it"

    def test_no_policy_input_is_exactly_the_old_behaviour(self):
        with_none = scored(None)
        for rid in ("DPDP-R9", "DPDP-R14A", "DPDP-R14B", "DPDP-S6", "DPDP-S7"):
            assert "cannot be observed through a public website scan" in res(with_none, rid).observations[0]
        assert with_none.readiness.assessed_weight == 19      # unchanged: the documented worked example

    def test_an_unreadable_policy_is_reported_as_such_not_as_missing(self):
        r = res(scored(analysis(status="unavailable", reason="A policy page was found but its text could not be read.")), "DPDP-R9")
        assert "could not be read" in r.observations[0]

    def test_the_policy_can_only_ever_help_or_leave_a_check_unverified_never_fail_it_outright(self):
        for status in ("verified", "partial", "not_found", "not_applicable"):
            for rid, rows in (("DPDP-R9", [row("dpo_contact", status, "q" * 30)]), ("DPDP-R14A", [row("rights_means", status, "q" * 30)]),
                              ("DPDP-R14B", [row("grievance_contact", status, "q" * 30), row("grievance_timeline", status, "q" * 30)])):
                assert res(scored(analysis(*rows)), rid).status != "not_met"


class TestAPolicyCannotProveWhatItOnlyDescribes:
    @pytest.mark.parametrize("rid,rowid", [("DPDP-S6", "security"), ("DPDP-S7", "breach_response")])
    def test_a_described_practice_is_shown_but_never_settles_the_check(self, rid, rowid):
        r = res(scored(analysis(row(rowid, "verified", "We use industry standard encryption and respond to every breach without delay."))), rid)
        assert r.status == "unable_to_verify" and r.counts_in_score is False
        assert "encryption" in r.observations[0] and "not evidence" in r.observations[1]
        assert r.method == "organisation_records" and r.needed_evidence


class TestTheScoreFollowsTheEvidence:
    def test_three_verified_checks_raise_coverage_and_leave_a_clean_score_at_100(self):
        base = scored(None)
        full = scored(analysis(DPO_OK, RIGHTS_OK, GRIEVANCE_OK, PERIOD_OK))
        assert base.readiness.score_percent == full.readiness.score_percent == 100.0
        assert full.readiness.assessed_weight == base.readiness.assessed_weight + 2 + 3 + 3
        assert full.readiness.coverage_percent > base.readiness.coverage_percent

    def test_a_partial_policy_statement_lowers_the_score_honestly(self):
        partial = scored(analysis(row("dpo_contact", "partial", "The officer looks after privacy.", detail="no email")))
        assert partial.readiness.score_percent < 100.0
        assert any(s.requirement_id == "DPDP-R9" and s.points_lost == 1.0 for s in partial.deductions.steps)

    def test_the_row_order_of_the_policy_never_changes_the_score(self):
        pa = analysis(DPO_OK, RIGHTS_OK, GRIEVANCE_OK, PERIOD_OK, row("security", "verified", "We use encryption everywhere in our systems."))
        base = json.dumps([(r.id, r.status, r.points_earned) for r in scored(pa).requirements])
        for seed in range(40):
            shuffled = copy.deepcopy(pa)
            random.Random(seed).shuffle(shuffled["requirements"])
            assert json.dumps([(r.id, r.status, r.points_earned) for r in scored(shuffled).requirements]) == base

    def test_canonical_ordering_leaves_the_policy_result_untouched(self):
        ev = evidence()
        ev["policy_analysis"] = analysis(DPO_OK)
        assert ds.canonical_evidence(ev)["policy_analysis"] == ev["policy_analysis"]


class TestTheResponseCarriesHowEachCheckWasAnswered:
    def test_method_and_needed_evidence_reach_the_api_model(self):
        from app.api.v1.routes.consent_scans import DpdpScoreResponse
        resp = DpdpScoreResponse(**{k: v for k, v in asdict(scored(analysis(DPO_OK))).items() if k != "deductions"})
        by = {r.id: r for r in resp.requirements}
        assert by["DPDP-R9"].method == "policy_text" and by["DPDP-R9"].needed_evidence == []
        assert by["DPDP-S6"].method == "organisation_records" and len(by["DPDP-S6"].needed_evidence) >= 4
        assert by["DPDP-C1"].method == "website_scan"

    def test_every_unverified_applicable_check_says_what_would_settle_it(self):
        s = scored(None, reject=True)
        for r in s.requirements:
            if r.status == "unable_to_verify":
                assert r.needed_evidence, f"{r.id} is unverified but names no evidence that would settle it"


class TestOneLoaderForEveryScorer:
    def test_the_loader_adds_the_policy_result_to_the_stored_evidence(self, monkeypatch):
        import uuid

        from app.db.repositories import scan_repository

        async def evid(scan_id, include_cookie_detail=False):
            return evidence()

        async def pol(scan_id):
            return analysis(DPO_OK)

        monkeypatch.setattr(scan_repository, "get_scan_evidence_summary_concurrent", evid)
        monkeypatch.setattr(scan_repository, "get_policy_analysis_result", pol)
        out = asyncio.run(scan_repository.get_scan_evidence_for_score(uuid.uuid4()))
        assert out["policy_analysis"]["requirements"][0]["id"] == "dpo_contact" and "pages" in out

    def test_without_an_analysis_the_evidence_is_unchanged(self, monkeypatch):
        import uuid

        from app.db.repositories import scan_repository

        async def evid(scan_id, include_cookie_detail=False):
            return evidence()

        async def pol(scan_id):
            return None

        monkeypatch.setattr(scan_repository, "get_scan_evidence_summary_concurrent", evid)
        monkeypatch.setattr(scan_repository, "get_policy_analysis_result", pol)
        assert "policy_analysis" not in asyncio.run(scan_repository.get_scan_evidence_for_score(uuid.uuid4()))

    def test_every_scorer_uses_the_one_loader(self):
        import inspect

        from app.api.v1.routes import consent_scans
        from app.services import score_deduction_log_service
        for fn in (consent_scans.get_scan_dpdp_score, consent_scans.compare_scan_dpdp_score, score_deduction_log_service.record_scan_deductions):
            assert "get_scan_evidence_for_score" in inspect.getsource(fn), fn.__name__
