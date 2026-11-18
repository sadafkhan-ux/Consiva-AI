"""The fixed-pattern second look at the policy text: a statement a model can miss is still found when it is plainly on the page, so a check does not flip
between scans of an unchanged page. The pattern only returns exact slices of the page that carry both the topic and a concrete detail."""

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from app.policy import analysis
from app.policy.analysis import (
    ANALYSED,
    NOT_FOUND,
    VERIFIED,
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

        from app.scoring.requirements import assess
        from tests.test_requirements_reading import evidence

        d = doc(DPO_TEXT)
        quote = "Our Data Protection Officer can be reached at dpo@x.test or on +91 98765 43210 for any question about your data."
        statuses = []
        for extraction in (model_says(("dpo_contact", quote)), model_says()):
            ev = evidence()
            ev["policy_analysis"] = {"status": "analysed", "reason": None, **analyse(extraction, [d], None, False)}
            s = assess(ev, date(2026, 10, 21))
            statuses.append(next(r for r in s.requirements if r.id == "DPDP-R9").status)
        assert statuses == ["met", "met"]


GAP = "Example Corp keeps the records it needs to run its services and to meet its legal duties. " * 12
WEAK_GRIEVANCE = "Questions or grievances about your personal data are taken seriously by Example Corp."
NAMED_OFFICER = "Our Grievance Officer, Jane Doe, can be reached on +91 98765 43210 for any grievance about your data."


class TestAPassageWithAContactIsPreferredOverAWeakModelPassage:
    def test_a_generic_quote_is_replaced_by_the_passage_that_names_an_officer_with_a_phone(self):
        d = doc(WEAK_GRIEVANCE + " " + GAP + NAMED_OFFICER + " " + GAP)
        r = row(analyse(model_says(("grievance_contact", WEAK_GRIEVANCE)), [d], None, False), "grievance_contact")
        assert NAMED_OFFICER in r["quote"] and WEAK_GRIEVANCE not in r["quote"] and verify_quote(r["quote"], d.text)
        assert r["status"] == "verified" and r["found_by"] == "pattern" and r["source_url"] == d.url
        assert r["contacts"]["phones"] == ["+91 98765 43210"]
        assert r["detail"].endswith("A passage with a contact detail was preferred over the model's passage.")

    def test_the_same_holds_for_the_officer_contact_requirement(self):
        weak = "Example Corp has a person responsible for questions about your data."
        named = "Our Data Protection Officer, Jane Doe, can be reached at privacy@example.com for any question."
        d = doc(weak + " " + GAP + named + " " + GAP)
        r = row(analyse(model_says(("dpo_contact", weak)), [d], None, False), "dpo_contact")
        assert named in r["quote"] and weak not in r["quote"] and r["status"] == "verified" and r["contacts"]["emails"] == ["privacy@example.com"]

    def test_a_model_quote_that_already_has_an_email_is_never_replaced(self):
        own = "Write to our Grievance Officer at privacy@example.com about any grievance."
        d = doc(own + " " + GAP + NAMED_OFFICER + " " + GAP)
        r = row(analyse(model_says(("grievance_contact", own)), [d], None, False), "grievance_contact")
        assert r["quote"] == own and r["status"] == "verified" and "found_by" not in r
        assert "preferred over" not in r["detail"] and r["contacts"]["emails"] == ["privacy@example.com"]

    def test_with_no_better_passage_the_model_quote_is_kept_unchanged(self):
        d = doc(WEAK_GRIEVANCE + " " + GAP)
        r = row(analyse(model_says(("grievance_contact", WEAK_GRIEVANCE)), [d], None, False), "grievance_contact")
        assert r["quote"] == WEAK_GRIEVANCE and r["status"] == "partial" and "found_by" not in r
        assert "preferred over" not in r["detail"]

    def test_no_other_requirement_is_touched(self):
        weak = "Example Corp explains how you can make requests about your personal data."
        d = doc(weak + " " + GAP + "To exercise your rights, write to privacy@example.com and we will answer. " + GAP)
        r = row(analyse(model_says(("rights_means", weak)), [d], None, False), "rights_means")
        assert r["quote"] == weak and "found_by" not in r and "preferred over" not in r["detail"]


# Part A: a weak generic sentence whose contact lies just beyond its own passage.
WEAK_FIRST = "Example Corp takes any grievance about the data it holds seriously."
SPACER = "Example Corp keeps the records it needs to run its services and to meet its legal duties. " * 4
NAMED_LATER = "Our Grievance Officer, Jane Doe, can be reached on +91 98765 43210 about any matter concerning your data."
A_TEXT = WEAK_FIRST + " " + SPACER + NAMED_LATER + " " + GAP


class TestEveryMatchIsConsidered:
    def test_the_first_match_alone_would_return_a_passage_with_no_contact_of_its_own(self):
        quote, _ = locate_by_pattern("grievance_contact", [doc(A_TEXT)])
        assert quote.startswith(WEAK_FIRST) and not analysis.has_contact(quote, allow_form=False)

    def test_the_named_passage_wins_over_the_weak_first_sentence(self):
        d = doc(A_TEXT)
        r = row(analyse(model_says(("grievance_contact", WEAK_FIRST)), [d], None, False), "grievance_contact")
        assert NAMED_LATER in r["quote"] and r["status"] == "verified" and r["found_by"] == "pattern" and verify_quote(r["quote"], d.text)
        assert r["contacts"]["phones"] == ["+91 98765 43210"] and "preferred over the model's passage" in r["detail"]

    def test_a_model_quote_with_its_own_email_is_unchanged(self):
        own = "Write to our Grievance Officer at privacy@example.com about any grievance."
        d = doc(own + " " + SPACER + NAMED_LATER + " " + GAP)
        r = row(analyse(model_says(("grievance_contact", own)), [d], None, False), "grievance_contact")
        assert r["quote"] == own and "found_by" not in r and "preferred over" not in r["detail"]

    def test_with_no_passage_that_holds_a_contact_the_model_quote_is_unchanged(self):
        d = doc(WEAK_FIRST + " " + SPACER + GAP)
        r = row(analyse(model_says(("grievance_contact", WEAK_FIRST)), [d], None, False), "grievance_contact")
        assert r["quote"] == WEAK_FIRST and r["status"] == "partial" and "found_by" not in r

# Part B: the analysis model cannot be reached, so only the fixed patterns run, and the degraded result is never stored or reused.
pa = analysis

B_FILLER = "Example Corp keeps the records it needs to run its services and to meet its legal duties. " * 6
NAMED = B_FILLER + "Our Grievance Officer, Jane Doe, can be reached at privacy@example.com about any grievance concerning your data. " + B_FILLER
PLAIN = B_FILLER + "Example Corp takes care of the data it holds on behalf of its customers. " * 4


class _Db:
    def __init__(self, rows):
        self.rows = rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, stmt):
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: self.rows))

    async def get(self, model, key):
        return SimpleNamespace(url="https://example.com/")


def _setup(monkeypatch, text):
    rows = [SimpleNamespace(url="https://example.com/privacy", policy_type="privacy_policy", extracted_text_ref=text)]
    monkeypatch.setattr(pa, "async_session_factory", lambda: _Db(rows))

    async def ev(scan_id, include_cookie_detail=False):
        return {"pages": [], "forms": [], "cookies": [], "trackers": [], "policies": [], "consent_signals": [], "third_party_services": []}

    monkeypatch.setattr(pa.scan_repository, "get_scan_evidence_summary_concurrent", ev)


async def _no_fetch(url):
    return None


async def _unreachable(system, user):
    raise TimeoutError("model unreachable")


def _row(out, rid):
    return next(r for r in out["requirements"] if r["id"] == rid)


def test_a_named_contact_is_found_by_pattern_when_the_model_is_unreachable(monkeypatch):
    _setup(monkeypatch, NAMED)
    out = asyncio.run(pa.run(uuid.uuid4(), extractor=_unreachable, fetcher=_no_fetch))
    r = _row(out, "grievance_contact")
    assert out["status"] == ANALYSED and r["status"] == VERIFIED and r["found_by"] == "pattern"
    assert "privacy@example.com" in r["quote"] and r["contacts"]["emails"] == ["privacy@example.com"]
    assert out["input_hash"] is None and out["model"]["unavailable"] is True and out["model"]["error_type"] == "TimeoutError"
    assert out["model"]["provider"] is None and out["model"]["model"] is None and out["model"]["reused"] is False
    assert out["model"]["tokens_in"] == 0 and out["model"]["tokens_out"] == 0
    assert any("TimeoutError" in n and "fixed patterns" in n and "not established" in n for n in out["notes"])


def test_nothing_found_stays_not_found_and_does_not_claim_the_model_read_it(monkeypatch):
    _setup(monkeypatch, PLAIN)
    out = asyncio.run(pa.run(uuid.uuid4(), extractor=_unreachable, fetcher=_no_fetch))
    assert out["status"] == ANALYSED and out["model"]["unavailable"] is True and out["documents"] and out["limitations"]
    missing = [r for r in out["requirements"] if r["status"] == NOT_FOUND]
    assert missing
    assert all("could not be reached" in r["detail"] and "not read by the model" in r["detail"] for r in missing)
    assert not any(r["detail"] == "Not found in the policy text." for r in out["requirements"])


def test_a_degraded_result_is_never_stored_and_has_no_hash_to_be_reused_by(monkeypatch):
    _setup(monkeypatch, NAMED)
    from app.llm import client

    monkeypatch.setattr(client, "resolve_reasoning_provider_and_model", lambda settings: ("provider-x", "model-x"))
    stored = []

    async def record(digest, result):
        stored.append((digest, result))

    monkeypatch.setattr(pa, "find_reusable", lambda *a: _none())
    monkeypatch.setattr(pa, "find_shared", lambda *a: _none())
    monkeypatch.setattr(pa, "store_shared", record)
    monkeypatch.setattr(pa, "_default_extractor", lambda scan_id, urls=None: _unreachable)
    out = asyncio.run(pa.run(uuid.uuid4(), fetcher=_no_fetch))     # the production path: a hash is worked out and the cache is consulted
    assert out["status"] == ANALYSED and out["model"]["unavailable"] is True and out["input_hash"] is None
    assert stored == []


async def _none():
    return None


def test_a_successful_model_run_behaves_as_before(monkeypatch):
    _setup(monkeypatch, PLAIN)

    async def extractor(system, user):
        return PolicyExtraction(results=[PolicyReqResult(id="retention", found=False, quote="", document=1)]), {"provider": "claude", "model": "m", "tokens_in": 10, "tokens_out": 5}

    out = asyncio.run(pa.run(uuid.uuid4(), extractor=extractor, fetcher=_no_fetch))
    assert out["status"] == ANALYSED and out["model"] == {"provider": "claude", "model": "m", "tokens_in": 10, "tokens_out": 5}
    assert "unavailable" not in out["model"] and out["notes"] == [] and out["input_hash"] is None
    assert _row(out, "retention")["detail"] == "Not found in the policy text."
