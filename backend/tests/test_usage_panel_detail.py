"""The usage panel's click-through detail: per-call rows, errors, what was saved, and a reused policy analysis shown as zero tokens (not as missing usage).

Real case (swaransoft.com, scan 0024ef1e, 28 Oct 2026): the policy analysis was reused from an earlier scan (tokens_in 0, tokens_out 0), so the scan has ONE
model call. The panel used to call the policy stage "no usage was recorded", which read as lost data when the database held exactly what happened."""

from app.services import usage_service as us
from tests.test_usage_panel import PIPELINE, SCAN, by_stage, event, stage, usage

REUSED = {"model": "claude-haiku-5-5", "provider": "claude", "reused": True, "tokens_in": 0, "tokens_out": 0}


def _pipeline_with_reused_policy():
    return [stage("policy_analysis", policy_model=REUSED, reused_from="f9f1c31d-037c-4e32-9c0f-6b4aea67193d") if s["stage"] == "policy_analysis" else s for s in PIPELINE]


def test_a_reused_policy_analysis_is_zero_tokens_with_its_source_not_unavailable():
    u = usage(_pipeline_with_reused_policy(), [event("llm_analysis", 6807, 226)])
    p = by_stage(u)["policy_analysis"]
    assert p["reused"] is True and p["reused_from_scan"] == "f9f1c31d-037c-4e32-9c0f-6b4aea67193d"
    assert p["total_tokens"] == 0 and p["calls"] == 0 and p["cost"] == 0.0
    assert "no model call" in p["note"] and "f9f1c31d" in p["note"]
    assert u["totals"]["calls"] == 1 and u["totals"]["total_tokens"] == 7033      # the screenshot's numbers


def test_a_policy_stage_that_really_called_the_model_is_not_marked_reused():
    p = by_stage(usage(None, [event("policy_analysis", 46333, 5523), event("llm_analysis")]))["policy_analysis"]
    assert p["reused"] is False and p["reused_from_scan"] is None and p["calls"] == 1


def test_every_recorded_call_is_listed_and_adds_up_to_the_totals():
    u = usage(None, [event("policy_analysis", 1000, 100), event("llm_analysis", 2000, 300, cache_read=50)])
    assert [c["stage"] for c in u["calls"]] == ["policy_analysis", "llm_analysis"]
    assert sum(c["total_tokens"] for c in u["calls"]) == u["totals"]["total_tokens"]
    c = u["calls"][1]
    assert (c["model"], c["attempts"], c["duration_ms"], c["cache_read_tokens"]) == ("claude-haiku-5-5", 1, 2500, 50)
    assert "request_ids" not in c                      # the provider's request ids stay out of the response


def test_no_calls_are_invented_for_a_scan_without_usage():
    assert usage(None, [])["calls"] == []


def test_errors_list_the_scan_error_failed_stages_and_failed_calls():
    stages = [stage("website_scan"), {**stage("llm_analysis", "failed"), "error": "Schema validation failed"}]
    failed = event("llm_analysis", status="failed")
    failed.error_type = "TimeoutError"
    u = usage(stages, [failed], {"status": "failed", "error": "The analysis did not finish"})
    where = [(e["where"], e["message"]) for e in u["errors"]]
    assert ("Scan", "The analysis did not finish") in where
    assert any(w == "Compliance reasoning, findings and recommendations" and "Schema validation failed" in m for w, m in where)
    assert any("TimeoutError" in m for _, m in where)


def test_a_clean_scan_has_no_errors():
    assert usage(None, [event("llm_analysis")])["errors"] == []


def test_results_say_what_was_saved_and_nothing_more():
    full = usage(None, [event("llm_analysis")])["results"]
    assert full == {"evidence": True, "analysis": True, "findings": True, "audit": True}
    crawl_only = usage([stage("url_validation"), stage("website_scan")], [])["results"]
    assert crawl_only == {"evidence": True, "analysis": False, "findings": False, "audit": False}


def test_the_history_line_carries_errors_results_and_the_reuse_flag():
    line = us.summary_of(usage(_pipeline_with_reused_policy(), [event("llm_analysis", 6807, 226)]))
    assert line["reused_analysis"] is True and line["results"]["audit"] is True and line["errors"] == []
    assert line["scan_id"] == str(SCAN["id"]) and line["url"] == SCAN["url"] and line["total_tokens"] == 7033
