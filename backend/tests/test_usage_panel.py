"""The usage panel's data: services/usage_service.py and the two routes (GET .../{scan_id}/usage, GET .../usage/history).

Everything here is built from plain rows and stand-in providers: no network, no database. The rules under test are the ones the panel promises: usage moves as
calls are recorded, every figure is a provider-reported number or marked unavailable, a deterministic stage is zero LLM tokens, a call is attributed to a page only
when it read exactly one, failures keep what was recorded, and nothing is counted twice or mixed between scans.
"""

import os
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import jwt
import pytest
from fastapi.testclient import TestClient

from app.api.v1.routes import consent_scans
from app.db.repositories import scan_repository
from app.main import app
from app.services import usage_service as us

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
SCAN = {"id": uuid.uuid4(), "url": "https://shop.example/", "status": "completed", "created_at": NOW, "completed_at": NOW}
PAGES = ["https://shop.example/", "https://shop.example/privacy", "https://shop.example/contact"]


def stage(name, status="completed", **meta):
    return {"stage": name, "status": status, "started_at": NOW, "completed_at": NOW, "duration_ms": 1000, "error": None,
            "meta": {"llm_usage": None, "provider": None, "model": None, "embed_tokens": None, "embed_requests": None, "pages_scanned": None, "policy_model": None, **meta}}


def event(stage_name, inp=1000, out=200, *, model="claude-haiku-5-5", provider="claude", status="success", pages=(), reasoning=None, cache_read=0, cache_write=0, cost=None):
    return SimpleNamespace(stage=stage_name, agent="consent", provider=provider, model=model, status=status, attempts=1, input_tokens=inp, output_tokens=out, cache_read_tokens=cache_read,
                           cache_write_tokens=cache_write, reasoning_tokens=reasoning, duration_ms=2500, cost_usd=cost, page_urls=list(pages), request_ids=["req_1"], error_type=None, created_at=NOW)


PIPELINE = [stage("url_validation"), stage("website_scan", pages_scanned=3), stage("classification"), stage("data_structuring"), stage("policy_analysis"), stage("normalize_evidence"),
            stage("rules_check"), stage("rag_retrieval", embed_tokens=16, embed_requests=1), stage("llm_analysis"), stage("output_validation"), stage("findings_generated"),
            stage("human_review_gate"), stage("audit_saved")]


def usage(stages=None, events=(), scan=None):
    return us.build_usage({**SCAN, **(scan or {})}, PIPELINE if stages is None else stages, list(events))


def by_stage(u):
    return {s["stage"]: s for s in u["stages"]}


# ── live: the panel moves as each call is recorded ───────────────────────────────────────────────────────────────


def test_usage_grows_as_each_call_arrives_and_a_running_model_stage_waits_instead_of_showing_zero():
    running = [stage("url_validation"), stage("website_scan", pages_scanned=3), stage("policy_analysis", "running")]
    u0 = usage(running, [], {"status": "running"})
    assert u0["state"] == "running" and u0["current_stage"] == "Privacy policy analysis" and u0["totals"]["calls"] == 0
    assert by_stage(u0)["policy_analysis"]["input_tokens"] is None and "Waiting for the model" in by_stage(u0)["policy_analysis"]["note"]   # unknown, not 0
    done_policy = [*running[:2], stage("policy_analysis"), stage("llm_analysis", "running")]
    e1 = event("policy_analysis", 12106, 4989, reasoning=3875)
    u1 = usage(done_policy, [e1], {"status": "running"})
    assert u1["totals"]["calls"] == 1 and u1["totals"]["input_tokens"] == 12106 and u1["current_stage"] == "Compliance reasoning, findings and recommendations"
    u2 = usage([*done_policy[:3], stage("llm_analysis")], [e1, event("llm_analysis", 6048, 364, reasoning=354)], {"status": "completed"})
    assert u2["totals"]["calls"] == 2 and u2["totals"]["input_tokens"] == 18154 and u2["totals"]["output_tokens"] == 5353 and u2["state"] == "completed"


# ── totals, reasoning, cache ─────────────────────────────────────────────────────────────────────────────────────


def test_totals_are_the_providers_numbers_and_reasoning_is_shown_but_never_added_on_top():
    u = usage(events=[event("policy_analysis", 12106, 4989, reasoning=3875), event("llm_analysis", 6048, 364, reasoning=354)])
    t = u["totals"]
    assert (t["input_tokens"], t["output_tokens"], t["total_tokens"], t["reasoning_tokens"]) == (18154, 5353, 18154 + 5353, 4229)   # reasoning is inside output
    assert u["source"] == "measured"


def test_cache_tokens_are_counted_apart_and_priced_at_their_own_rates():
    u = usage(events=[event("llm_analysis", 1000, 100, cache_read=10_000, cache_write=2_000)])
    t = u["totals"]
    assert (t["cache_read_tokens"], t["cache_write_tokens"], t["total_tokens"]) == (10_000, 2_000, 13_100)
    c = u["cost"]
    assert c["cache_read"] == pytest.approx(10_000 * 0.01 / 1e6) and c["input"] == pytest.approx(1000 * 0.10 / 1e6) and c["output"] == pytest.approx(100 * 0.50 / 1e6)
    assert c["total"] == pytest.approx(c["input"] + c["cache_read"] + c["cache_write"] + c["output"])


def test_reasoning_is_unavailable_not_zero_when_no_call_reported_it():
    assert usage(events=[event("llm_analysis", reasoning=None)])["totals"]["reasoning_tokens"] is None


# ── stages ───────────────────────────────────────────────────────────────────────────────────────────────────────


def test_each_stage_that_ran_is_listed_with_its_own_usage_and_a_deterministic_stage_is_confirmed_zero():
    u = usage(events=[event("policy_analysis", 100, 10), event("llm_analysis", 200, 20), event("llm_analysis", 300, 30)])
    st = by_stage(u)
    assert (st["policy_analysis"]["calls"], st["policy_analysis"]["input_tokens"]) == (1, 100)
    assert (st["llm_analysis"]["calls"], st["llm_analysis"]["input_tokens"], st["llm_analysis"]["output_tokens"], st["llm_analysis"]["total_tokens"]) == (2, 500, 50, 550)
    for name in ("website_scan", "classification", "rules_check", "output_validation", "findings_generated", "audit_saved"):
        s = st[name]
        assert s["kind"] == "non_llm" and (s["calls"], s["input_tokens"], s["output_tokens"], s["total_tokens"]) == (0, 0, 0, 0), name
    assert [s["stage"] for s in u["stages"]][:2] == ["url_validation", "website_scan"]            # pipeline order


def test_a_stage_that_did_not_run_is_not_listed():
    u = usage(stages=[stage("url_validation"), stage("website_scan")], events=[])
    assert [s["stage"] for s in u["stages"]] == ["url_validation", "website_scan"]


def test_the_embedding_stage_is_not_an_llm_stage_and_reports_its_own_tokens_or_says_unavailable():
    emb = by_stage(usage(events=[event("llm_analysis")]))["rag_retrieval"]
    assert emb["kind"] == "embedding" and emb["calls"] == 0 and emb["total_tokens"] == 0 and emb["embedding_tokens"] == 16 and "16" in emb["note"]
    old = by_stage(usage(stages=[stage("rag_retrieval")], events=[event("llm_analysis")]))["rag_retrieval"]
    assert old["embedding_tokens"] is None and "not recorded" in old["note"]


def test_a_completed_model_stage_with_no_recorded_usage_is_unavailable_not_zero():
    s = by_stage(usage(stages=[stage("llm_analysis")], events=[]))["llm_analysis"]
    assert s["total_tokens"] is None and s["cost"] is None and "No usage was recorded" in s["note"]


def test_the_models_used_by_a_stage_are_named():
    s = by_stage(usage(events=[event("llm_analysis", provider="claude"), event("llm_analysis", provider="nvidia", model="nemotron")]))["llm_analysis"]
    assert s["models"] == ["claude/claude-haiku-5-5", "nvidia/nemotron"]


# ── retries, failures, duplicates ────────────────────────────────────────────────────────────────────────────────


def test_a_retry_is_a_second_billed_call_and_counts_once_each():
    u = usage(events=[event("llm_analysis", 6000, 300), event("llm_analysis", 6200, 900)])
    assert by_stage(u)["llm_analysis"]["calls"] == 2 and u["totals"]["input_tokens"] == 12200
    # the same stage records alongside do not add a second copy: when per-call events exist they are the only source
    both = usage(stages=[*PIPELINE[:-5], stage("llm_analysis", llm_usage={"usage_by_attempt": [{"input_tokens": 6000, "output_tokens": 300}]}, provider="claude", model="claude-haiku-5-5")],
                 events=[event("llm_analysis", 6000, 300)])
    assert both["totals"]["input_tokens"] == 6000 and both["source"] == "measured"


def test_a_failed_call_keeps_its_tokens_and_is_counted_as_failed():
    u = usage(events=[event("llm_analysis", 5000, 80, status="failed"), event("llm_analysis", 5000, 400)])
    assert u["totals"]["failed_calls"] == 1 and u["totals"]["input_tokens"] == 10_000 and by_stage(u)["llm_analysis"]["failed_calls"] == 1


def test_a_failed_scan_keeps_everything_recorded_before_the_failure():
    u = usage(stages=[stage("url_validation"), stage("website_scan"), stage("policy_analysis"), stage("llm_analysis", "failed")], events=[event("policy_analysis", 900, 90)], scan={"status": "failed"})
    assert u["state"] == "failed" and u["totals"]["input_tokens"] == 900 and by_stage(u)["llm_analysis"]["status"] == "failed"


# ── a scan from before per-call tracking ────────────────────────────────────────────────────────────────────────


def test_an_older_scan_reads_the_provider_numbers_its_stage_records_kept_and_says_so():
    stages = [stage("policy_analysis", policy_model={"provider": "claude", "model": "claude-haiku-5-5", "tokens_in": 7074, "tokens_out": 4456}),
              stage("llm_analysis", provider="claude", model="claude-haiku-5-5", llm_usage={"attempts": 1, "usage_by_attempt": [
                  {"input_tokens": 11841, "output_tokens": 3590, "output_tokens_details": {"thinking_tokens": 2304}}]})]
    u = usage(stages, [])
    assert u["source"] == "stage_records" and u["totals"]["input_tokens"] == 7074 + 11841 and u["totals"]["output_tokens"] == 4456 + 3590
    assert u["totals"]["reasoning_tokens"] == 2304                                            # only the call that reported it
    assert any("before per-call tracking" in n for n in u["notes"]) and "stage record" in by_stage(u)["llm_analysis"]["note"]
    assert u["cost"]["total"] == pytest.approx((7074 + 11841) * 0.10 / 1e6 + (4456 + 3590) * 0.50 / 1e6)


def test_a_scan_with_no_usage_anywhere_is_unavailable_and_nothing_is_invented():
    u = usage([stage("url_validation"), stage("website_scan"), stage("llm_analysis")], [])
    assert u["source"] == "none" and u["totals"]["calls"] == 0 and u["cost"]["total"] is None and u["cost"]["complete"] is False
    assert any("No model usage was recorded" in n for n in u["notes"]) and by_stage(u)["llm_analysis"]["total_tokens"] is None


def test_a_stage_record_with_no_usage_block_adds_nothing():
    assert us.events_from_stage_records([stage("llm_analysis", provider="claude", model="m"), stage("policy_analysis")]) == []


# ── cost ──────────────────────────────────────────────────────────────────────────────────────────────────────────


def test_a_model_without_a_verified_price_keeps_its_tokens_and_has_no_cost():
    u = usage(events=[event("llm_analysis", 1000, 100, provider="nvidia", model="nemotron"), event("policy_analysis", 2000, 200)])
    assert u["totals"]["input_tokens"] == 3000                                                   # tokens counted
    assert u["totals"]["unpriced_calls"] == 1 and u["cost"]["complete"] is False and any("no verified price" in n for n in u["notes"])
    assert u["cost"]["total"] == pytest.approx(2000 * 0.10 / 1e6 + 200 * 0.50 / 1e6)            # only the priced call is in the cost
    assert by_stage(u)["llm_analysis"]["cost"] is None and by_stage(u)["policy_analysis"]["cost"] is not None


def test_each_call_is_priced_by_its_own_model():
    u = usage(events=[event("llm_analysis", 50_000, 0, model="claude-haiku-5-5"), event("policy_analysis", 50_000, 0, model="claude-sonnet-5-5")])   # both under Haiku's 100K tier
    assert u["cost"]["total"] == pytest.approx(50_000 * 0.10 / 1e6 + 50_000 * 2.00 / 1e6)


def test_the_cost_names_its_currency_and_where_the_price_came_from():
    c = usage(events=[event("llm_analysis")])["cost"]
    assert c["currency"] == "USD" and "platform.claude.com" in c["price_source"] and c["complete"] is True


# ── pages ─────────────────────────────────────────────────────────────────────────────────────────────────────────


def test_a_call_that_read_exactly_one_page_is_attributed_to_it_and_one_that_read_several_is_shared():
    u = usage(events=[event("policy_analysis", 4000, 400, pages=[PAGES[1]]), event("llm_analysis", 6000, 300, pages=PAGES), event("llm_analysis", 10, 1, pages=[])])
    p = u["pages"]
    assert [(r["url"], r["calls"], r["input_tokens"], r["total_tokens"]) for r in p["attributed"]] == [(PAGES[1], 1, 4000, 4400)]
    assert len(p["shared"]) == 1 and p["shared"][0]["page_urls"] == sorted(PAGES) and p["shared"][0]["input_tokens"] == 6000 and "cannot be split" in p["shared"][0]["reason"]
    assert p["unattributed"]["calls"] == 1 and p["unattributed"]["input_tokens"] == 10
    total = sum(r["total_tokens"] for r in p["attributed"]) + sum(r["total_tokens"] for r in p["shared"]) + p["unattributed"]["total_tokens"]
    assert total == u["totals"]["total_tokens"]                                                  # every token is in exactly one place


def test_page_usage_is_never_invented_for_an_older_scan():
    stages = [stage("llm_analysis", provider="claude", model="claude-haiku-5-5", llm_usage={"usage_by_attempt": [{"input_tokens": 5000, "output_tokens": 100}]})]
    p = usage(stages, [])["pages"]
    assert p["attributed"] == [] and p["shared"] == [] and p["unattributed"]["input_tokens"] == 5000 and "not recorded" in p["unattributed"]["reason"]


# ── scans are separate ───────────────────────────────────────────────────────────────────────────────────────────


def test_two_scans_built_from_their_own_rows_never_share_numbers():
    a = us.build_usage({**SCAN, "id": uuid.uuid4()}, PIPELINE, [event("llm_analysis", 111, 1)])
    b = us.build_usage({**SCAN, "id": uuid.uuid4()}, PIPELINE, [event("llm_analysis", 222, 2), event("policy_analysis", 333, 3)])
    assert (a["totals"]["input_tokens"], b["totals"]["input_tokens"]) == (111, 555) and a["scan_id"] != b["scan_id"]


def test_the_loaders_filter_by_scan_id():
    sql = str(us._stage_query([SCAN["id"]]))
    assert "scan_id IN" in sql.replace("\n", " ")


# ── what it says about non-LLM work ──────────────────────────────────────────────────────────────────────────────


def test_it_says_what_is_not_an_llm_call_and_that_the_report_uses_none():
    notes = " ".join(usage(events=[event("llm_analysis")])["notes"])
    assert "browser navigation" in notes and "DPDP score" in notes and "Report and PDF generation use no LLM" in notes


def test_the_summary_line_carries_what_the_history_shows():
    s = us.summary_of(usage(events=[event("policy_analysis", 100, 10), event("llm_analysis", 200, 20)]))
    assert (s["calls"], s["input_tokens"], s["output_tokens"], s["total_tokens"], s["status"], s["source"], s["pages_scanned"], s["domain"]) == (2, 300, 30, 330, "completed", "measured", 3, "shop.example")
    assert s["cost"] == pytest.approx(300 * 0.10 / 1e6 + 30 * 0.50 / 1e6) and s["cost_complete"] and s["currency"] == "USD"


# ── the routes ───────────────────────────────────────────────────────────────────────────────────────────────────

client = TestClient(app)
ORG = uuid.uuid4()


def _headers(org=ORG):
    token = jwt.encode({"sub": str(uuid.uuid4()), "org_id": str(org), "aud": "authenticated", "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp())},
                       os.environ["SUPABASE_JWT_SECRET"], algorithm="HS256")
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def wired(monkeypatch):
    from app.db.session import get_db

    async def fake_db():
        yield None

    seen: dict = {}

    async def get_scan(db, scan_id, org_id):
        seen["org"] = org_id
        return SimpleNamespace(**SCAN) if seen.get("found", True) else None

    async def load_scan_usage(db, scan):
        return us.build_usage(SCAN, PIPELINE, [event("policy_analysis", 12106, 4989, reasoning=3875, pages=[PAGES[1]]), event("llm_analysis", 6048, 364, reasoning=354, pages=PAGES)])

    async def list_recent(db, org_id, limit):
        seen["limit"] = limit
        return [SimpleNamespace(**SCAN)]

    async def load_history(db, scans):
        return [us.summary_of(us.build_usage(SCAN, PIPELINE, [event("llm_analysis", 200, 20)]))]

    monkeypatch.setattr(scan_repository, "get_scan", get_scan)
    monkeypatch.setattr(scan_repository, "list_recent_scans", list_recent)
    monkeypatch.setattr(consent_scans.usage_service, "load_scan_usage", load_scan_usage)
    monkeypatch.setattr(consent_scans.usage_service, "load_history", load_history)
    app.dependency_overrides[get_db] = fake_db
    yield seen
    app.dependency_overrides.pop(get_db, None)


def test_the_usage_route_returns_the_scans_usage_for_its_own_organisation(wired):
    r = client.get(f"/api/v1/consent/scans/{SCAN['id']}/usage", headers=_headers())
    body = r.json()
    assert r.status_code == 200 and wired["org"] == ORG
    assert body["totals"]["input_tokens"] == 18154 and body["totals"]["reasoning_tokens"] == 4229 and body["source"] == "measured" and body["domain"] == "shop.example"
    assert [s["stage"] for s in body["stages"]][:2] == ["url_validation", "website_scan"] and body["stages"][1]["kind"] == "non_llm"
    assert body["pages"]["attributed"][0]["url"] == PAGES[1] and len(body["pages"]["shared"]) == 1


def test_another_organisations_scan_is_not_found(wired):
    wired["found"] = False
    assert client.get(f"/api/v1/consent/scans/{SCAN['id']}/usage", headers=_headers()).status_code == 404


def test_the_usage_routes_need_a_token_and_a_valid_id(wired):
    assert client.get(f"/api/v1/consent/scans/{SCAN['id']}/usage").status_code == 401
    assert client.get("/api/v1/consent/scans/usage/history").status_code == 401
    assert client.get("/api/v1/consent/scans/not-a-uuid/usage", headers=_headers()).status_code == 422


def test_the_history_route_is_not_swallowed_by_the_scan_id_routes_and_clamps_its_limit(wired):
    r = client.get("/api/v1/consent/scans/usage/history?limit=9999", headers=_headers())
    assert r.status_code == 200 and isinstance(r.json(), list) and r.json()[0]["total_tokens"] == 220 and wired["limit"] == 100
    assert client.get("/api/v1/consent/scans/usage/history?limit=0", headers=_headers()).status_code == 200 and wired["limit"] == 1


def test_the_response_holds_no_prompt_secret_or_request_id(wired):
    text = client.get(f"/api/v1/consent/scans/{SCAN['id']}/usage", headers=_headers()).text
    assert "req_1" not in text and "sk-" not in text and "api_key" not in text.lower() and "prompt" not in text.lower()


def test_a_scan_that_was_only_crawled_says_no_analysis_ran_rather_than_usage_was_lost():
    crawled = usage([stage("url_validation"), stage("website_scan"), stage("classification")], [])
    assert crawled["analysis_run"] is False and crawled["source"] == "none"
    assert any("No AI analysis was run" in n for n in crawled["notes"]) and not any("No model usage was recorded" in n for n in crawled["notes"])
    assert us.summary_of(crawled)["analysis_run"] is False
    ran = usage([stage("website_scan"), stage("llm_analysis")], [])
    assert ran["analysis_run"] is True and us.summary_of(ran)["analysis_run"] is True
