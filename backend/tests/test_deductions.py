"""Why were points deducted: every deduction is a real, evidence-backed difference between a check's weight and what it earned, and the steps rebuild the
score exactly. Nothing here is specific to one website."""

import itertools
from fractions import Fraction

import pytest

from app.scoring.dpdp_score import assess
from tests.test_dpdp_score import TODAY, evidence, tr


def ded(**kw):
    return assess(evidence(**kw), TODAY).deductions


def step(d, rid):
    return next(s for s in d.steps if s.requirement_id == rid)


class TestWorkedExample:
    def test_one_pre_consent_tracker_costs_exactly_the_weight_of_the_check(self):
        s = assess(evidence(trackers=[tr(1)]), TODAY)
        d = s.deductions
        c1 = step(d, "DPDP-C1")
        assert (c1.outcome, c1.weight, c1.points_earned, c1.points_lost) == ("Not Met", 5, 0.0, 5.0)
        # 25 points are assessed (the tracker also makes C2 assessable); this check loses 5 of them: 5 / 25 = 20 points off 100, so 100 -> 80.
        assert (c1.percent_lost, c1.score_before, c1.score_after) == (20.0, 100.0, 80.0)
        # N2 (cookie policy missing) is partly met and takes 1 more point (4.0): 80 -> 76, the score the report shows.
        assert (d.score_percent, step(d, "DPDP-N2").score_after) == (76.0, 76.0)
        assert s.readiness.score_percent == d.score_percent
        assert "5 ÷ 25 × 100 = 20.0" in c1.calculation

    def test_the_evidence_names_the_request_the_page_and_the_consent_state(self):
        ev = step(ded(trackers=[tr(1)]), "DPDP-C1").evidence
        req = next(e for e in ev if e.kind == "request")
        assert "t1.test/a.js" in req.text
        assert req.consent_states == ["Before Consent"]
        assert req.category == "analytics"

    def test_a_cookie_is_named_with_its_page(self):
        cookie = {"id": "c1", "name": "_ga", "domain": "x.test", "category": "analytics", "vendor": "Google Analytics", "is_first_party": True, "source": "rule",
                  "consent_states": ["pre_consent"], "observations": [{"consent_state": "pre_consent", "page_url": "https://x.test/0"}]}
        ev = step(ded(cookies=[cookie]), "DPDP-C1").evidence
        ck = next(e for e in ev if e.kind == "cookie")
        assert ("_ga" in ck.text, ck.url, ck.vendor) == (True, "https://x.test/0", "Google Analytics")

    def test_a_partial_check_loses_half_its_weight(self):
        d = ded(trackers=[tr(1)], reject=False, granular=False)   # N2 is partly met in the module's own worked example
        n2 = step(d, "DPDP-N2")
        assert (n2.outcome, n2.weight, n2.points_earned, n2.points_lost) == ("Partially Met", 2, 1.0, 1.0)


class TestRebuildsTheScore:
    SCENARIOS = [
        {},
        {"trackers": [tr(1)]},
        {"trackers": [tr(1)], "reject": False, "granular": False},
        {"reject": False},
        {"granular": False},
        {"policies": ()},
        {"mechanism": "none", "trackers": [tr(1), tr(2, "advertising")]},
        {"trackers": [tr(1)], "reject": False, "granular": False, "policies": ()},
        {"trackers": [tr(1, "marketing", ("pre_consent", "post_accept"))], "confirmed": False},
    ]

    @pytest.mark.parametrize("kw", SCENARIOS)
    def test_running_score_ends_on_the_real_score_and_steps_add_up(self, kw):
        s = assess(evidence(**kw), TODAY)
        d = s.deductions
        if s.readiness.status != "scored":
            assert d.steps == [] and d.score_percent is None
            return
        assert d.score_percent == s.readiness.score_percent
        last = d.steps[-1].score_after if d.steps else 100.0
        assert last == s.readiness.score_percent
        # exact: points lost over points assessed, from the same figures the score uses
        lost = sum(Fraction(x.points_lost).limit_denominator(2) for x in d.steps)
        assert Fraction(s.readiness.points_assessed) - lost == Fraction(s.readiness.points_earned).limit_denominator(2)
        # every step is a genuine difference between weight and points earned, never an invention
        for x in d.steps:
            assert x.points_lost == x.weight - x.points_earned > 0
            assert x.score_after < x.score_before
        # the steps chain: each one starts where the last ended
        for a, b in itertools.pairwise(d.steps):
            assert b.score_before == a.score_after

    def test_a_clean_site_loses_nothing_and_says_so(self):
        d = ded()
        assert d.steps == [] and d.score_percent == 100.0
        assert "No points were deducted" in d.summary

    def test_heaviest_deduction_is_listed_first(self):
        d = ded(trackers=[tr(1)], reject=False, granular=False, policies=())
        losses = [s.points_lost for s in d.steps]
        assert losses == sorted(losses, reverse=True)


class TestChecksThatCostNothing:
    def test_unable_to_verify_and_not_applicable_are_listed_and_never_deducted(self):
        d = ded(trackers=[tr(1)])
        by = {n.requirement_id: n for n in d.not_deducted}
        assert by["DPDP-R9"].status == "Unable to Verify" and "no points are taken off" in by["DPDP-R9"].reason.lower()
        assert all(n.requirement_id not in {s.requirement_id for s in d.steps} for n in d.not_deducted)

    def test_a_duplicate_gap_is_counted_once(self):
        d = ded(trackers=[tr(1)], reject=False, granular=False)     # C5 carries the same gap as C4
        c5 = next(n for n in d.not_deducted if n.requirement_id == "DPDP-C5")
        assert c5.status == "Not Applicable" and "DPDP-C4" in c5.reason
        assert "DPDP-C5" not in {s.requirement_id for s in d.steps}

    def test_no_score_means_no_deductions_and_it_is_not_a_zero(self):
        s = assess(evidence(pages=0), TODAY)
        d = s.deductions
        assert d.status == "insufficient_evidence" and d.score_percent is None and d.steps == []
        assert "not a score of zero" in d.note


class TestKeptOutOfTheClientResponse:
    def test_the_score_response_does_not_carry_the_deductions(self):
        from dataclasses import asdict

        from app.api.v1.routes.consent_scans import DpdpScoreResponse
        result = assess(evidence(trackers=[tr(1)]), TODAY)
        assert result.deductions is not None                    # still computed, for the backend
        resp = DpdpScoreResponse(**{k: v for k, v in asdict(result).items() if k != "deductions"})
        assert "deductions" not in resp.model_dump()
        assert "deductions" not in DpdpScoreResponse.model_fields


class TestBackendRecord:
    def test_the_log_block_names_the_check_the_marks_the_calculation_and_the_evidence(self):
        import uuid

        from app.services.score_deduction_log_service import format_log
        d = assess(evidence(trackers=[tr(1)]), TODAY).deductions
        text = format_log(uuid.UUID(int=1), "https://x.test/", d, methodology_version="1.0.0", as_of="2026-10-21")
        assert "DPDP-C1" in text and "weight 5, earned 0, lost 5" in text
        assert "5 ÷ 25 × 100 = 20.0" in text and "100.0% -> 80.0%" in text
        assert "t1.test/a.js" in text and "Before Consent" in text
        assert "not deducted, DPDP-R9" in text
        assert "Score = points earned ÷ points assessed × 100 = 19 ÷ 25 × 100 = 76.0%" in text

    async def test_it_is_logged_and_stored_as_an_audit_row_with_the_whole_breakdown(self, monkeypatch, caplog):
        import logging
        import uuid
        from types import SimpleNamespace

        from app.services import score_deduction_log_service as svc
        scan = SimpleNamespace(url="https://x.test/", completed_at=None, started_at=None, created_at=None)
        recorded: dict = {}

        async def get_scan(db, scan_id, org_id):
            return scan

        async def get_evidence(scan_id, include_cookie_detail=False):
            return evidence(trackers=[tr(1)])

        async def record(db, **kw):
            recorded.update(kw)

        monkeypatch.setattr(svc.scan_repository, "get_scan", get_scan)
        monkeypatch.setattr(svc.scan_repository, "get_scan_evidence_for_score", get_evidence)
        monkeypatch.setattr(svc.audit_repository, "record", record)
        with caplog.at_level(logging.INFO, logger="consiva.score_deductions"):
            d = await svc.record_scan_deductions(None, scan_id=uuid.UUID(int=7), org_id=uuid.UUID(int=8))
        assert d is not None and d.steps[0].requirement_id == "DPDP-C1"
        assert recorded["action"] == "scan.score_deductions" and recorded["entity_id"] == uuid.UUID(int=7)
        assert recorded["after"]["deductions"]["steps"][0]["points_lost"] == 5.0
        assert "Score deductions for scan" in caplog.text and "DPDP-C1" in caplog.text

    async def test_a_missing_scan_records_nothing(self, monkeypatch):
        import uuid

        from app.services import score_deduction_log_service as svc

        async def get_scan(db, scan_id, org_id):
            return None

        monkeypatch.setattr(svc.scan_repository, "get_scan", get_scan)
        assert await svc.record_scan_deductions(None, scan_id=uuid.UUID(int=1), org_id=uuid.UUID(int=2)) is None

    def test_the_audit_listing_never_returns_the_stored_breakdown(self):
        from app.api.v1.routes.consent_scans import AuditLogResponse
        assert "after" not in AuditLogResponse.model_fields and "deductions" not in AuditLogResponse.model_fields
