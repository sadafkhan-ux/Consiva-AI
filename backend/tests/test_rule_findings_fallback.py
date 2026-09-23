"""A failed analysis must not silently produce zero findings.

From a real validation run against hubspot.com, which is what this file exists to
prevent recurring:

    25 pages crawled, Accept and Reject both clicked and confirmed
    840 trackers, 124 cookies, 50 forms, 175 policies persisted
    3 rules matched in 1ms -- including post-reject tracking
    RAG retrieved real DPDP Act and IT Act citations in 370ms
    llm_analysis ran 9m10s, timed out
    findings: []

Everything needed to tell the customer their site tracks visitors after they press
Reject was in the database, and the report said nothing at all -- because
`validate_output` routed a failed analysis straight past findings creation to the
audit log.

The model never detected that violation. The rules did. The model writes the prose.
Losing the model should cost the prose, not the finding.
"""

import inspect

import pytest

from app.agents.consent_agent import graph as graph_module
from app.agents.consent_agent.nodes import create_rule_findings as fallback


def _rule(**over):
    base = {
        "rule_id": "R-003",
        "rule_version": "1.0",
        "category": "marketing",
        "risk_level": "high",
        "summary": "55 analytics/marketing scripts continued firing after the visitor "
                   "clicked Reject.",
        "evidence_ids": ["tracker-1", "tracker-2"],
        "confidence": "high",
    }
    base.update(over)
    return base


# ── The routing gap itself ──────────────────────────────────────────────────────

def test_a_failed_analysis_no_longer_routes_past_findings_creation():
    """This is the whole bug in one assertion."""
    assert graph_module._VALIDATION_ROUTES["failed"] == "create_rule_findings"
    assert graph_module._VALIDATION_ROUTES["failed"] != "write_audit_log"


def test_the_fallback_node_is_in_the_graph_and_reaches_human_review():
    built = graph_module.build_graph()
    assert "create_rule_findings" in built.nodes
    body = inspect.getsource(graph_module.build_graph)
    assert 'graph.add_edge("create_rule_findings", "human_review_gate")' in body


def test_the_success_path_is_unchanged():
    """The two finding-creating nodes must never both run for one scan."""
    assert graph_module._VALIDATION_ROUTES["valid"] == "create_findings"
    assert graph_module._VALIDATION_ROUTES["retry"] == "llm_reasoning"


# ── What a rule-derived finding says, and does not say ──────────────────────────

def test_a_rule_match_becomes_a_finding_carrying_the_rules_own_summary():
    finding = fallback._as_finding(_rule(), "Request timed out.")
    assert "continued firing after the visitor clicked Reject" in finding.finding
    assert finding.category == "marketing"
    assert finding.risk_level == "high"
    assert finding.evidence == ["tracker-1", "tracker-2"]


def test_it_says_in_its_own_text_that_the_narrative_is_missing():
    """A reader must not mistake this for the product's normal, explained output."""
    finding = fallback._as_finding(_rule(), "Request timed out.")
    assert "could not be produced" in finding.finding
    assert "Request timed out." in finding.finding
    assert "has not been written up" in finding.finding


def test_it_cites_nothing():
    """Citations are resolved from what the model cited, and it cited nothing.
    Attaching the retrieved chunks anyway would present regulatory references as
    though something had applied them to this finding."""
    finding = fallback._as_finding(_rule(), "timeout")
    assert finding.dpdp_reference == []


@pytest.mark.parametrize("confidence", ["high", "low"])
def test_every_rule_finding_requires_human_review_whatever_the_rule_thought(confidence):
    """A rule confident enough to skip review when a narrative was coming with it is
    not confident enough to skip review when nothing explains it."""
    finding = fallback._as_finding(_rule(confidence=confidence), "timeout")
    assert finding.requires_human_review is True


@pytest.mark.parametrize("risk,expected", [
    ("high", "high"), ("medium", "medium"), ("low", "low"), ("unrecognised", "medium"),
])
def test_priority_follows_the_rules_risk_with_a_safe_default(risk, expected):
    assert fallback._as_finding(_rule(risk_level=risk), "x").priority == expected


def test_a_rule_with_no_summary_still_produces_readable_text():
    finding = fallback._as_finding(_rule(summary=""), "timeout")
    assert "A compliance rule matched this scan." in finding.finding


def test_the_recommendation_does_not_ask_for_a_rescan():
    """The evidence is already collected. Telling somebody to re-crawl a 25-page site
    because a model call timed out wastes eleven minutes to fix nothing."""
    finding = fallback._as_finding(_rule(), "timeout")
    assert "does not need re-scanning" in finding.recommendation


# ── What the audit record has to show ───────────────────────────────────────────

def test_the_stage_records_that_these_are_not_normal_findings():
    body = inspect.getsource(fallback.create_rule_findings)
    assert 'meta["source"] = "rules"' in body
    assert 'meta["narrative_missing"] = True' in body
    assert 'meta["reason"] = reason' in body
    assert 'meta["rule_ids"]' in body


def test_it_writes_under_its_own_stage_name():
    """So the audit trail distinguishes a fallback run from a normal one rather than
    both appearing as `findings_generated`."""
    body = inspect.getsource(fallback.create_rule_findings)
    assert '"rule_findings_generated"' in body


def test_no_findings_are_invented_when_no_rule_matched():
    """A scan where nothing matched should still produce nothing. The fallback exists
    to stop findings being lost, not to manufacture them."""
    body = inspect.getsource(fallback.create_rule_findings)
    assert "state.rule_findings or []" in body
