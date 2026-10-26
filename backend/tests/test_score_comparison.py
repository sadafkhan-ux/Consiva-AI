"""Why two scans of one website scored differently (scoring/comparison.py, GET /consent/scans/{id}/dpdp-score/compare)."""

import asyncio
import uuid
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest

from app.api.v1.routes import consent_scans
from app.core.exceptions import NotFoundError, ScanNotReadyError
from app.db.repositories import scan_repository
from app.scoring import comparison
from app.scoring.dpdp_score import RequirementResult
from tests.test_dpdp_score import evidence, tr

TODAY = date(2026, 10, 26)


def info(i, host="https://shop.example"):
    return {"scan_id": f"scan-{i}", "scanned_at": f"2026-10-2{i}T10:00:00+00:00", "url": host}


def compare(a, b, host="https://shop.example"):
    return comparison.compare_scans(a, b, as_of=TODAY, previous_info=info(1, host), latest_info=info(2, host))


def row(result, rid):
    return next(c for c in result["changes"] if c["id"] == rid)


@pytest.mark.parametrize("host", ["https://shop.example", "https://clinic.example.org", "https://news.example.co.uk"])
def test_two_identical_scans_give_the_same_result_and_no_changes(host):
    r = compare(evidence(pages=3), evidence(pages=3), host)
    assert r["changes"] == [] and r["causes"] == [] and r["score_change"] == 0.0
    assert r["summary"] == "The two scans give the same result: no requirement changed."


@pytest.mark.parametrize("host", ["https://shop.example", "https://bank.example.in"])
def test_a_check_the_later_scan_could_not_test_is_a_coverage_change_not_a_behaviour_change(host):
    earlier = evidence(pages=3)                                                                    # Accept and Reject both exercised and confirmed
    later = evidence(pages=3, accept_outcome="not_found", reject_outcome="not_found", confirmed=False)
    r = compare(earlier, later, host)
    c3 = row(r, "DPDP-C3")
    assert (c3["previous"]["status"], c3["latest"]["status"]) == ("met", "unable_to_verify")
    assert c3["cause"] == "scan_coverage" and "reject outcome" in c3["explanation"]
    assert r["causes"] == ["scan_coverage"]
    assert r["denominators"]["changed"] is True
    assert {i["name"] for i in r["changed_inputs"]} >= {"accept_outcome", "reject_outcome", "reject_confirmed"}
    assert "scan coverage" in r["summary"] and "assessment coverage went from" in r["summary"]


def test_a_site_that_starts_tracking_before_consent_is_a_website_change_and_its_points_are_accounted_for():
    earlier = evidence(pages=3, trackers=[])
    later = evidence(pages=3, trackers=[tr(1)])                                                    # an analytics script now fires before consent
    r = compare(earlier, later)
    c1 = row(r, "DPDP-C1")
    assert (c1["previous"]["status"], c1["latest"]["status"]) == ("met", "not_met")
    assert c1["cause"] == "website_behaviour" and "analytics or marketing before consent" in c1["explanation"]
    assert c1["latest"]["evidence"] and c1["latest"]["evidence_ids"] == ["t1"]
    assert r["score_change"] is not None and r["score_change"] < 0
    assert r["attribution"]["sums_to_total"] is True
    assert sum(c["score_change"] for c in r["changes"] if c["score_change"] is not None) == pytest.approx(r["score_change"], abs=0.11)


def test_the_steps_add_up_to_the_whole_change_when_several_requirements_move():
    earlier = evidence(pages=3, trackers=[])
    later = evidence(pages=3, trackers=[tr(1)], policies=(), mechanism="banner", reject=False)
    r = compare(earlier, later)
    assert len(r["changes"]) >= 3 and r["attribution"]["sums_to_total"] is True
    assert sum(c["score_change"] for c in r["changes"] if c["score_change"] is not None) == pytest.approx(r["score_change"], abs=0.2)


def test_a_score_that_becomes_available_or_is_withheld_is_said_so_not_shown_as_zero():
    withheld = evidence(pages=0)
    scored = evidence(pages=3)
    up = compare(withheld, scored)
    assert up["previous"]["score_percent"] is None and up["latest"]["score_percent"] is not None
    assert up["score_change"] is None and "no score" in up["summary"]
    assert any(c["score_becomes_available"] for c in up["changes"])
    down = compare(scored, withheld)
    assert down["latest"]["score_percent"] is None and down["score_change"] is None
    assert "has no score" in down["summary"] and any(c["score_becomes_withheld"] for c in down["changes"])


def test_a_framed_wrapper_in_the_later_scan_is_reported_as_the_reason():
    r = compare(evidence(pages=3), evidence(pages=3, framed=["https://app.example"]))
    assert any(i["name"] == "framed_wrapper" for i in r["changed_inputs"])
    assert r["causes"] == ["scan_coverage"]


def test_a_change_with_no_recorded_cause_is_called_unexplained_never_given_a_reason():
    def res(status):
        return RequirementResult(id="DPDP-C9", category="Consent", title="t", provisions=[], weight=3, effective=TODAY, in_force=True, status=status,
                                 points_earned=3.0 if status == "met" else 0.0, counts_in_score=True, observations=[], evidence_ids=[], rule_ids=[])

    cause, sentence = comparison._cause(res("met"), res("not_met"), [])
    assert cause == "unexplained" and "data-quality" in sentence


def test_the_result_says_what_it_cannot_explain():
    note = compare(evidence(pages=3), evidence(pages=3))["methodology_note"]
    assert "older version" in note and "cannot be re-derived" in note


# ── the endpoint ────────────────────────────────────────────────────────────────────────────────────────────────────────────────

ORG = uuid.uuid4()
SITE = uuid.uuid4()


def scan(i, website=SITE, org=ORG, status="completed"):
    stamp = datetime(2026, 10, 20 + i, 10, 0, tzinfo=UTC)
    return SimpleNamespace(id=uuid.uuid4(), org_id=org, website_id=website, url="https://shop.example", status=status, created_at=stamp, started_at=stamp, completed_at=stamp)


def call(monkeypatch, latest, previous, evidence_by_id, against=None):
    async def get_scan(db, scan_id, org_id):
        return next((s for s in (latest, previous) if s is not None and s.id == scan_id and s.org_id == org_id), None)

    async def prior(db, s):
        return previous

    async def ev(scan_id, include_cookie_detail=False):
        return evidence_by_id[scan_id]

    monkeypatch.setattr(scan_repository, "get_scan", get_scan)
    monkeypatch.setattr(scan_repository, "previous_completed_scan", prior)
    monkeypatch.setattr(scan_repository, "get_scan_evidence_for_score", ev)
    user = SimpleNamespace(org_id=str(ORG), user_id=str(uuid.uuid4()), role="member")

    class _Db:
        async def rollback(self):
            return None

    return asyncio.run(consent_scans.compare_scan_dpdp_score(latest.id, against, TODAY, user, _Db()))


def test_the_endpoint_compares_a_scan_with_the_previous_completed_scan_of_the_site(monkeypatch):
    old, new = scan(1), scan(2)
    out = call(monkeypatch, new, old, {old.id: evidence(pages=3), new.id: evidence(pages=3, trackers=[tr(1)])})
    assert out.previous["scan_id"] == str(old.id) and out.latest["scan_id"] == str(new.id)
    assert out.causes == ["website_behaviour"] and out.score_change < 0


def test_the_endpoint_compares_with_a_scan_named_by_the_caller(monkeypatch):
    old, new = scan(1), scan(2)
    out = call(monkeypatch, new, old, {old.id: evidence(pages=3), new.id: evidence(pages=3)}, against=old.id)
    assert out.changes == []


def test_a_scan_of_another_website_cannot_be_compared(monkeypatch):
    old, new = scan(1, website=uuid.uuid4()), scan(2)
    with pytest.raises(NotFoundError):
        call(monkeypatch, new, old, {old.id: evidence(), new.id: evidence()}, against=old.id)


def test_a_scan_of_another_organisation_is_not_found(monkeypatch):
    old, new = scan(1, org=uuid.uuid4()), scan(2)
    with pytest.raises(NotFoundError):
        call(monkeypatch, new, old, {old.id: evidence(), new.id: evidence()}, against=old.id)


def test_with_no_earlier_scan_there_is_nothing_to_compare_and_the_answer_is_409(monkeypatch):
    new = scan(2)
    with pytest.raises(ScanNotReadyError):
        call(monkeypatch, new, None, {new.id: evidence()})


def test_reading_the_policy_in_only_one_scan_is_a_coverage_difference_not_a_change_in_the_website():
    from tests.test_policy_backed_checks import DPO_OK, analysis

    plain = evidence(policies=("privacy_policy",))
    read = {**evidence(policies=("privacy_policy",)), "policy_analysis": analysis(DPO_OK)}
    out = comparison.compare_scans(plain, read, as_of=TODAY, previous_info={"scan_id": "a", "scanned_at": None, "url": "https://x.test"}, latest_info={"scan_id": "b", "scanned_at": None, "url": "https://x.test"})
    change = next(c for c in out["changes"] if c["id"] == "DPDP-R9")
    assert (change["previous"]["status"], change["latest"]["status"]) == ("unable_to_verify", "met")
    assert change["cause"] == "scan_coverage"
    assert any(i["name"] == "privacy_policy_text_read" and i["latest"] is True for i in out["changed_inputs"])


def test_a_policy_that_changed_between_two_readings_is_the_website_behaving_differently():
    from tests.test_policy_backed_checks import DPO_OK, analysis, row

    before = {**evidence(policies=("privacy_policy",)), "policy_analysis": analysis(DPO_OK)}
    after = {**evidence(policies=("privacy_policy",)), "policy_analysis": analysis(row("dpo_contact", "not_found"))}
    out = comparison.compare_scans(before, after, as_of=TODAY, previous_info={"scan_id": "a", "scanned_at": None, "url": "https://x.test"}, latest_info={"scan_id": "b", "scanned_at": None, "url": "https://x.test"})
    change = next(c for c in out["changes"] if c["id"] == "DPDP-R9")
    assert (change["previous"]["status"], change["latest"]["status"]) == ("met", "unable_to_verify")
    assert "policy_statements_verified" in [i["name"] for i in out["changed_inputs"]]
