"""Findings, coverage completion state, change detection helpers and the recalculation that keeps the score, the findings and the parameters in step."""
import uuid
from datetime import date
from types import SimpleNamespace

import pytest

from app.enterprise import changes, findings, pipeline
from app.enterprise.coverage import completion_state
from app.enterprise.scoring import catalogue as cat
from app.enterprise.scoring.engine import HUMAN_REVIEW, MET, NEEDS_RECORDS, NOT_APPLICABLE, NOT_ASSESSED, NOT_MET, PARTLY, Assessment

AFTER = date(2027, 6, 1)


# ---- findings ------------------------------------------------------------------------------------------------------------------------------------------------------

def test_every_parameter_has_a_recommendation():
    assert set(findings.RECOMMENDATIONS) == {p.id for p in cat.PARAMETERS}
    assert all(len(r) > 30 for r in findings.RECOMMENDATIONS.values())


@pytest.mark.parametrize("pid,state,expected", [
    ("C4", NOT_MET, "high"), ("C4", PARTLY, "medium"), ("N1", NOT_MET, "high"), ("C1", NOT_MET, "medium"), ("C1", PARTLY, "low"),
    ("C12", NOT_MET, "low"), ("TS4", NOT_MET, "low"), ("TS4", PARTLY, "low"),
])
def test_severity_follows_the_parameters_points_and_kind(pid, state, expected):
    assert findings.severity_for(cat.BY_ID[pid], state) == expected


@pytest.mark.parametrize("state", [MET, NOT_ASSESSED, NOT_APPLICABLE])
def test_met_not_assessed_and_not_applicable_raise_no_finding(state):
    assert findings.draft_for(cat.BY_ID["C4"], state, "x", None, [], []) is None


def test_a_finding_carries_law_evidence_urls_and_a_recommendation():
    urls = [f"https://x.test/{i}" for i in range(120)]
    d = findings.draft_for(cat.BY_ID["C4"], NOT_MET, "2 pages contacted Google Analytics", "high", ["ev:1"], urls)
    assert d.finding_key == "C4" and d.legal_refs == ["s.6(1)"] and d.evidence_refs == ["ev:1"]
    assert d.affected_count == 120 and len(d.affected_urls) == findings.MAX_LISTED_URLS          # the true count, a capped list
    assert d.penalty_ceiling_cr == 50 and d.schedule_item == 7 and "consent mode" in d.recommendation
    assert d.observed == "2 pages contacted Google Analytics" and "not met" in d.reason.lower()


def test_a_supporting_signal_is_never_given_a_penalty_ceiling_or_called_a_duty():
    d = findings.draft_for(cat.BY_ID["TS4"], PARTLY, "3 of 5 headers", None, [], [])
    assert d.penalty_ceiling_cr is None and d.schedule_item is None and "not a duty" in d.expected


def test_a_fail_that_could_not_be_verified_says_so_and_is_marked_for_a_person():
    h = findings.draft_for(cat.BY_ID["C5"], NOT_MET, "could not be verified: Reject could not be given", None, [], [], on_evidence=False)
    assert h.title.startswith("Fail:") and h.human_review is True and "could not be verified" in h.reason
    low = findings.draft_for(cat.BY_ID["C7"], NOT_MET, "no reopen control found", "low", ["ev:9"], ["https://x.test/"])
    assert low.human_review is True and low.severity == "high"                     # C7 is 3 points; a low-confidence reading is flagged for a person


def test_e1_never_raises_a_finding():
    assert findings.draft_for(cat.BY_ID["E1"], NOT_MET, "x", None, [], []) is None


@pytest.mark.asyncio
async def test_findings_are_ordered_most_serious_first_and_one_per_parameter(monkeypatch):
    async def no_urls(db, refs):
        return ["https://x.test/a"]
    monkeypatch.setattr(findings, "_urls_for", no_urls)
    rows = [SimpleNamespace(parameter_id=p, state=s, note="n", confidence=None, evidence_refs=["ev:1"]) for p, s in
            [("TS4", PARTLY), ("C1", NOT_MET), ("C4", NOT_MET), ("N1", NOT_MET), ("C5", MET), ("X1", NOT_ASSESSED)]]
    drafts = await findings.build_findings(None, rows)
    assert [d.parameter_id for d in drafts] == ["C4", "N1", "C1", "TS4"]


@pytest.mark.asyncio
async def test_with_the_score_every_fail_raises_its_finding_including_what_could_not_be_verified(monkeypatch):
    from app.enterprise.scoring.engine import score
    async def no_urls(db, refs):
        return []
    monkeypatch.setattr(findings, "_urls_for", no_urls)
    current = [Assessment(p.id, MET, (f"ev:{p.id}",)) for p in cat.PARAMETERS if p.scored and p.id not in ("X1", "K1")]
    current += [Assessment("X1", NOT_ASSESSED, (), None, "no cookie policy was found"), Assessment("K1", NOT_APPLICABLE, (), None, "no child-directed page")]
    rows = [SimpleNamespace(parameter_id=a.parameter_id, state=a.state, note=a.note, confidence=None, evidence_refs=list(a.evidence_refs)) for a in current]
    drafts = await findings.build_findings(None, rows, score(current, as_of=AFTER))
    assert [d.parameter_id for d in drafts] == ["X1"]                      # K1 does not apply: a Pass, no finding; E1 (no points) raises none
    assert drafts[0].human_review is True and "could not be verified" in drafts[0].observed


# ---- the one word for where a scan stands ----------------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("crawl,analysis,kw,expected", [
    ("queued", "not_started", {}, "queued"),
    ("running", "not_started", {}, "crawling"),
    ("completed", "not_started", {}, "crawled"),
    ("completed", "running", {}, "analysing"),
    ("completed", "completed", {}, "complete"),
    ("completed", "completed", {"awaiting_analysis": 3}, "partial"),            # pages still outstanding: never "complete"
    ("completed", "completed", {"failed_chunks": 1}, "partial"),
    ("completed", "completed", {"pending_chunks": 2}, "partial"),
    ("completed", "partial", {}, "partial"),
    ("completed", "failed", {}, "failed"),
    ("completed", "completed", {"pending_urls": 4}, "partial"),                 # the crawl stopped with URLs still waiting: partial, never complete
    ("running", "not_started", {"pending_urls": 4}, "crawling"),
    ("failed", "not_started", {}, "failed"),
    ("cancelled", "not_started", {}, "cancelled"),
])
def test_completion_state(crawl, analysis, kw, expected):
    args = {"pending_urls": 0, "failed_chunks": 0, "pending_chunks": 0, "awaiting_analysis": 0, **kw}
    assert completion_state(crawl, analysis, **args) == expected


# ---- change detection ----------------------------------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("url,expected", [
    ("https://x.test/privacy", True), ("https://x.test/legal/cookie-policy", True), ("https://x.test/terms", True),
    ("https://x.test/blog/dpdp-act-2023-compliance-guide", False),          # an article that mentions the law is not the policy
    ("https://x.test/news/privacy-update", False), ("https://x.test/about", False), ("https://x.test/", False),
])
def test_a_policy_is_recognised_by_address_and_an_article_never_is(url, expected):
    assert changes.is_policy_url(url) is expected


# ---- recalculation keeps score, findings and parameters in step ------------------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_recalculate_scores_the_current_assessments_and_rewrites_the_findings(monkeypatch):
    current = [Assessment(p.id, MET, (f"ev:{p.id}",)) for p in cat.PARAMETERS if p.scored]
    current[[a.parameter_id for a in current].index("C4")] = Assessment("C4", NOT_MET, ("ev:C4",))
    rows = [SimpleNamespace(parameter_id=a.parameter_id, state=a.state, note="", confidence=None, evidence_refs=list(a.evidence_refs)) for a in current]
    saved = {}

    async def load_current(db, scan_id): return current
    async def load_rows(db, scan_id): return rows
    async def save_score(db, scan_id, result): saved["score"] = result; return uuid.uuid4()
    async def replace(db, scan_id, drafts): saved["findings"] = drafts; return len(drafts)
    async def audit(db, scan_id, actor, event, detail=None): saved.setdefault("audit", []).append((actor, event, detail))
    async def urls(db, refs): return []
    monkeypatch.setattr(pipeline.store, "load_current", load_current)
    monkeypatch.setattr(pipeline.store, "load_current_rows", load_rows)
    monkeypatch.setattr(pipeline.store, "save_score", save_score)
    monkeypatch.setattr(pipeline.store, "replace_findings", replace)
    monkeypatch.setattr(pipeline.store, "audit", audit)
    monkeypatch.setattr(findings, "_urls_for", urls)

    result = await pipeline.recalculate(None, uuid.uuid4(), AFTER, actor="reviewer-1")
    assert result.score == 96 and saved["score"] is result                                  # C4 is 4 points of 100
    assert [d.parameter_id for d in saved["findings"]] == ["C4"] and saved["findings"][0].severity == "high"
    actor, event, detail = saved["audit"][0]
    assert (actor, event) == ("reviewer-1", "score_calculated") and detail["score"] == 96 and detail["findings"] == 1


# ---- the pages a privacy assessment needs are read first, and absence is not a finding when the crawl was cut short -------------------------------------------------------------

from app.enterprise.crawl.scope import legal_priority


@pytest.mark.parametrize("url,expected", [
    # owner's sequence (19 Nov 2026): privacy policies 0, the other legal pages 1, every other page 2 (the homepage is read before all of them by the runner)
    ("https://x.test/privacy", 0), ("https://x.test/privacy-policy", 0), ("https://x.test/data-protection", 0),
    ("https://x.test/legal/cookie-policy", 1), ("https://x.test/terms-and-conditions", 1), ("https://x.test/contact", 1), ("https://x.test/grievance-redressal", 1),
    ("https://x.test/", 2), ("https://x.test/products/widget", 2),
    ("https://x.test/blog/dpdp-act-2023-compliance-guide", 2),        # an article about the law is not the policy: no priority
    ("https://x.test/news/privacy-update", 2),
])
def test_privacy_pages_are_claimed_first_then_legal_pages_and_an_article_never_is(url, expected):
    assert legal_priority(url) == expected


def test_a_crawl_stopped_by_a_limit_is_partial_even_with_nothing_pending():
    assert completion_state("completed", "completed", pending_urls=0, failed_chunks=0, pending_chunks=0, awaiting_analysis=0, stop_reason="url_limit") == "partial"
    assert completion_state("completed", "completed", pending_urls=0, failed_chunks=0, pending_chunks=0, awaiting_analysis=0, stop_reason="time_limit") == "partial"
    assert completion_state("completed", "completed", pending_urls=0, failed_chunks=0, pending_chunks=0, awaiting_analysis=0, stop_reason=None) == "complete"
    assert completion_state("completed", "completed", pending_urls=0, failed_chunks=0, pending_chunks=0, awaiting_analysis=0, stop_reason="cancelled") == "complete"


class _Db:
    """A database stand-in answering the guard's two queries."""
    def __init__(self, stop_reason, pending):
        self.stop_reason, self.pending = stop_reason, pending

    async def execute(self, q, params=None):
        return SimpleNamespace(first=lambda: SimpleNamespace(stop_reason=self.stop_reason))

    async def scalar(self, q, params=None):
        return self.pending


@pytest.mark.asyncio
@pytest.mark.parametrize("stop,pending,changed", [("url_limit", 0, True), ("time_limit", 0, True), (None, 12, True), (None, 0, False), ("cancelled", 0, False)])
async def test_no_privacy_notice_found_is_not_a_finding_when_the_crawl_was_cut_short(monkeypatch, stop, pending, changed):
    saved = []

    async def load_current(db, scan_id): return [Assessment("N1", NOT_MET, ("ev:1",), "medium", "25 crawled pages were checked and none is a privacy notice")]
    async def save(db, scan_id, assessments, *, assessor, provider=None, model=None): saved.append((assessments, assessor)); return len(assessments)
    monkeypatch.setattr(pipeline.store, "load_current", load_current)
    monkeypatch.setattr(pipeline.store, "save_assessments", save)
    assert await pipeline.apply_crawl_completeness_guard(_Db(stop, pending), uuid.uuid4()) is changed
    if changed:
        (a,), who = saved[0]
        assert a.parameter_id == "N1" and a.state == NOT_ASSESSED and who == "rule:coverage" and "may exist on a page that was not read" in a.note
    else:
        assert saved == []


@pytest.mark.asyncio
async def test_the_guard_leaves_a_notice_that_was_found_alone(monkeypatch):
    async def load_current(db, scan_id): return [Assessment("N1", MET, ("ev:1",), "high", "a privacy notice was found")]
    monkeypatch.setattr(pipeline.store, "load_current", load_current)
    assert await pipeline.apply_crawl_completeness_guard(_Db("url_limit", 0), uuid.uuid4()) is False


from app.enterprise.crawl.frontier import record_cap


@pytest.mark.parametrize("pages,recorded", [(0, 0), (25, 10_000), (500, 10_000), (1000, 20_000), (100_000, 2_000_000)])
def test_every_url_found_is_recorded_far_beyond_the_page_limit(pages, recorded):
    assert record_cap(pages) == recorded
