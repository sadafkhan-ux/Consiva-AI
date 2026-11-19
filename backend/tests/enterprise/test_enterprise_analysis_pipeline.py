"""The pipeline end to end with an in-memory repo and a scripted model: persistence before calls, resumption, failures, statuses, and what reaches the engine."""
import pytest

from app.enterprise.analysis import consolidate as co
from app.enterprise.analysis import constants as K
from app.enterprise.analysis.pipeline import run_text_analysis
from app.enterprise.scoring import engine as eng
from app.enterprise.scoring.engine import HUMAN_REVIEW, MET, NOT_ASSESSED, NOT_MET

from .test_enterprise_analysis_support import AS_OF, ORG, POLICY, SCAN, FakeLLM, FakeRepo, ev, site


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(K, "LLM_BACKOFF_S", (0.0,))


async def go(repo, llm, **kw):
    return await run_text_analysis(SCAN, ORG, llm=llm, as_of=AS_OF, repo=repo, **kw)


def fresh(**kw):
    rows, fr = site(**kw)
    return FakeRepo(rows, fr)


async def test_a_full_run_completes_and_assesses_the_text_parameters():
    repo, llm = fresh(), FakeLLM()
    out = await go(repo, llm)
    assert out.status == "completed" and out.chunks_failed == 0 and out.chunks_pending == 0 and out.chunks_done >= 4
    a = repo.assessments()
    assert a["N1"].state == MET and a["N6"].state == MET and a["G2"].state == MET and a["G4"].state == MET and a["N7"].state == MET
    assert a["G1"].state == MET and a["G3"].state == MET and a["N5"].state == MET
    assert a["K1"].state == "not_applicable" and a["S1"].state == "not_applicable" and a["E1"].state == "not_applicable"      # method version 2: nothing shows these apply
    assert out.parameters_assessed == 38 and out.facts_kept >= 15
    assert [s for s, _ in repo.scan_history] == ["running", "completed"]
    assert repo.scan_history[0][1]["started"] is True and repo.scan_history[-1][1]["completed"] is True


async def test_the_blog_article_is_never_read_as_the_policy_and_is_not_analysed():
    repo = fresh()
    out = await go(repo, FakeLLM())
    assert out.pages_by_category["blog_article"] == 1 and out.pages_by_category["privacy_policy"] == 1
    assert not any("blog" in k[1] for k in repo.chunks if k[0] == "page")
    assert repo.frontier_status["f4"] == "not_applicable" and repo.frontier_status["f5"] == "not_applicable"


async def test_frontier_status_is_done_for_analysed_pages():
    repo = fresh()
    await go(repo, FakeLLM())
    assert repo.frontier_status["f2"] == "done" and repo.frontier_status["f3"] == "done"        # the policy, and the contact page with its form
    assert repo.frontier_status["f1"] == "not_applicable"


async def test_assessors_and_provider_are_recorded_separately():
    repo = fresh()
    out = await go(repo, FakeLLM())
    kinds = {assessor: (p, m) for assessor, _, p, m in repo.saved}
    assert kinds["llm"] == ("claude", "claude-test") and kinds["rule:text"] == (None, None)
    assert {a.parameter_id for _, items, _, _ in repo.saved for a in items} == {x.parameter_id for x in out.assessments}
    assert "N1" in {a.parameter_id for assessor, items, _, _ in repo.saved if assessor == "rule:text" for a in items}
    assert "G2" in {a.parameter_id for assessor, items, _, _ in repo.saved if assessor == "llm" for a in items}


async def test_every_chunk_is_persisted_pending_before_the_model_is_called():
    repo = fresh()
    seen = []

    class Probe(FakeLLM):
        async def __call__(self, system, user, schema):
            if schema.__name__ == "PageExtraction":
                seen.append({k: c.status for k, c in repo.chunks.items() if k[0] == "page"})
            return await super().__call__(system, user, schema)

    await go(repo, Probe())
    assert seen and all(len(s) == len(seen[0]) for s in seen)          # the whole plan was on record from the first call
    assert all("pending" in s.values() for s in seen[:1])


async def test_invented_quotes_are_discarded_and_counted_in_the_outcome():
    out = await go(fresh(), FakeLLM(invent=True))
    assert out.facts_discarded >= 1
    assert not any("ISO 27001" in str(a.note) for a in out.assessments)


async def test_a_total_model_failure_fails_the_scan_and_never_becomes_a_compliance_failure():
    repo = fresh()
    out = await go(repo, FakeLLM(fail_if=lambda *a: True))
    assert out.status == "failed" and out.chunks_done == 0 and out.chunks_failed >= 1 and out.error
    a = repo.assessments()
    for pid in co.LLM_PARAMS:
        assert a[pid].state != NOT_MET, pid
    assert a["G2"].state == NOT_ASSESSED and a["N2"].state == NOT_ASSESSED and a["N6"].state == NOT_ASSESSED
    assert a["N1"].state == MET                                              # what does not need the model is still assessed
    assert [s for s, _ in repo.scan_history] == ["running", "failed"]
    assert repo.frontier_status["f2"] == "failed"
    assert all(c.status == "failed" and c.error and c.attempts == K.LLM_ATTEMPTS for k, c in repo.chunks.items() if k[0] == "page")


async def test_malformed_json_twice_fails_the_chunk_after_the_bounded_retries():
    repo = fresh()
    out = await go(repo, FakeLLM(malformed_first=10_000))
    assert out.status == "failed" and out.chunks_done == 0


async def test_one_failed_chunk_makes_the_scan_partial_and_unread_absences_not_assessed(monkeypatch):
    monkeypatch.setattr(K, "CHUNK_MAX_CHARS", 700)
    monkeypatch.setattr(K, "CHUNK_OVERLAP_CHARS", 60)
    repo = fresh()
    llm = FakeLLM(fail_if=lambda name, system, user: name == "PageExtraction" and "Security" in user and "privacy-policy" in user)
    out = await go(repo, llm)
    n_policy = sum(1 for k in repo.chunks if k[0] == "page" and k[1].endswith("/privacy-policy"))
    assert n_policy >= 3
    assert out.status == "partial" and out.chunks_failed >= 1 and out.chunks_done >= 1
    a = repo.assessments()
    assert a["G7"].state in (NOT_ASSESSED, MET) and a["G7"].state != NOT_MET
    assert a["G2"].state == MET                                              # found in a chunk that was read
    assert repo.frontier_status["f2"] == "failed"
    assert [s for s, _ in repo.scan_history][-1] == "partial" and "unit(s) of analysis failed" in repo.scan_history[-1][1]["error"]


async def test_resume_skips_chunks_that_are_done_and_finishes_the_rest(monkeypatch):
    monkeypatch.setattr(K, "CHUNK_MAX_CHARS", 700)
    monkeypatch.setattr(K, "CHUNK_OVERLAP_CHARS", 60)
    repo = fresh()
    first = FakeLLM(fail_if=lambda name, system, user: name == "PageExtraction" and "Security" in user and "privacy-policy" in user)
    out1 = await go(repo, first)
    assert out1.status == "partial"
    done_before = {k for k, c in repo.chunks.items() if k[0] == "page" and c.status == "done"}
    assert done_before
    second = FakeLLM()
    out2 = await go(repo, second)
    reread = [c for c in second.calls if c[0] == "PageExtraction"]
    total = len([k for k in repo.chunks if k[0] == "page"])
    assert 1 <= len(reread) < total                                          # only the chunks that had not finished were read again
    assert out2.status == "completed" and out2.chunks_failed == 0
    assert done_before <= {k for k, c in repo.chunks.items() if c.status == "done"}
    assert repo.assessments()["G7"].state in (MET, "partly_met")         # the chunk that failed first time is now read: its security statement counts


async def test_a_second_run_with_everything_done_calls_the_model_for_nothing_extra():
    repo = fresh()
    await go(repo, FakeLLM())
    again = FakeLLM()
    out = await go(repo, again)
    assert out.status == "completed" and again.calls == []                # extraction, interpretation and cross-page results are all reused


async def test_changed_evidence_replans_the_chunks_and_reads_them_again():
    repo = fresh()
    await go(repo, FakeLLM())
    repo.evidence = [r for r in repo.evidence if not (r.kind == "page_text" and r.source_url.endswith("privacy-policy"))] + [
        ev("page_text", "https://acme.test/privacy-policy", POLICY + "\nWe also use cookies.\n", frontier="f2")]
    again = FakeLLM()
    out = await go(repo, again)
    assert any(c[0] == "PageExtraction" for c in again.calls) and out.status == "completed"


async def test_no_text_at_all_is_a_failed_analysis_with_a_reason():
    repo = FakeRepo([], [])
    out = await go(repo, FakeLLM())
    assert out.status == "failed" and "no page or document text" in out.error
    assert repo.assessments()["N1"].state == NOT_ASSESSED


async def test_a_site_with_no_policy_has_n1_not_met_and_the_scan_still_completes():
    rows = [ev("page_text", "https://x.test/", "We sell shoes. Come and visit our store.", frontier="a"), ev("page_html", "https://x.test/", "<title>Shoes</title>")]
    repo = FakeRepo(rows, [])
    out = await go(repo, FakeLLM())
    assert out.status == "completed" and repo.assessments()["N1"].state == NOT_MET


async def test_an_ambiguous_page_is_settled_by_the_model_and_a_failure_leaves_it_unsettled():
    rows = [ev("page_text", "https://x.test/", "We sell shoes."), ev("page_html", "https://x.test/legal/info", "<title>Privacy Policy</title><h1>Privacy Policy</h1>"),
            ev("page_text", "https://x.test/legal/info", POLICY)]
    # classified ambiguous? make sure by checking the pipeline's reading
    repo = FakeRepo(rows, [])
    out = await go(repo, FakeLLM(page_kind={"https://x.test/legal/info": {"category": "privacy_policy", "confidence": "high"}}))
    assert out.pages_by_category.get("privacy_policy") == 1 and repo.assessments()["N1"].state == MET

    repo2 = FakeRepo(rows, [])
    out2 = await go(repo2, FakeLLM(fail_if=lambda name, system, user: name == "PageKind"))
    assert out2.pages_by_category.get("privacy_policy", 0) == 0
    assert repo2.assessments()["N1"].state == NOT_ASSESSED               # an unsettled page that may be the policy is never a finding


async def test_low_confidence_readings_flow_to_human_review_in_the_engine():
    repo = fresh()
    out = await go(repo, FakeLLM(verdicts={"N2": ("yes", "low"), "N3": ("yes", "low")}))
    a = repo.assessments()
    assert a["N2"].confidence == "low"
    res = eng.score(out.assessments, as_of=AS_OF)
    states = {x.parameter_id: x.state for x in res.lines}
    assert states["N2"] in (MET, "partly_met") and states["N3"] in (MET, "partly_met") and {"N2", "N3"} <= set(res.needs_attention["review_recommended"])     # the result stands, marked for a reviewer


async def test_the_assessments_are_accepted_by_the_scoring_engine_and_no_unassessed_parameter_is_invented():
    repo = fresh()
    out = await go(repo, FakeLLM())
    res = eng.score(out.assessments, as_of=AS_OF)
    assert len(res.lines) == 51
    assert {a.parameter_id for a in out.assessments}.isdisjoint({"TS1", "C4", "C8"})
    assert all(a.evidence_refs for a in out.assessments if a.state in (MET, "partly_met", NOT_MET))
    assert all(r.startswith("ev:e") for a in out.assessments for r in a.evidence_refs)


async def test_the_outcome_reports_observations_and_the_final_record_is_stored():
    repo = fresh()
    out = await go(repo, FakeLLM())
    assert out.observations["undisclosed_trackers"] == [] and ("final", "*", 0) in repo.chunks
    assert repo.chunks[("final", "*", 0)].result["states"]["N1"] == MET
