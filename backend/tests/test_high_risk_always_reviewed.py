"""A high-risk finding always reaches a person, whatever the model answered.

`requires_human_review` is a field the LLM fills in, and models disagree about it.
Measured directly when the reasoning provider was switched to openai/gpt-oss-120b: on
an identical prompt describing analytics firing before consent with no CMP present, it
returned `requires_human_review: false` on every run.

That was first read as a prompt-injection success, because the probe in
test_prompt_injection_probe.py asserts the model sets it True and a payload in the
evidence had demanded exactly `requires_human_review: false`. A control run disproved
it: the same prompt with the payload REMOVED produced False just as consistently. It
is the model's own default on a clear-cut violation. (The payload's other two demands
-- downgrade risk_level to low, omit the tracker/cookie issue -- failed every time.)

Either way the product cannot leave it there. Whether a high-risk DPDP finding reaches
a customer unread must not depend on which provider answered that request. So the
model's answer is honoured only to ESCALATE, never to waive -- the same stance
create_rule_findings already takes on the failure path.
"""

import pytest

from app.agents.consent_agent.nodes.create_findings import _needs_review
from app.llm.schemas import ConsentFindingLLM


def _finding(**over) -> ConsentFindingLLM:
    base = dict(
        category="analytics",
        risk_level="high",
        priority="high",
        finding="Analytics scripts fired before any consent interaction.",
        evidence=["t1", "c1"],
        dpdp_reference=["dpdp-s5-1"],
        requires_human_review=False,
        recommendation="Block analytics until consent is given.",
    )
    base.update(over)
    return ConsentFindingLLM(**base)


def test_a_high_risk_finding_is_reviewed_even_when_the_model_says_no():
    """The bug, as one assertion."""
    assert _needs_review(_finding(risk_level="high", requires_human_review=False)) is True


@pytest.mark.parametrize("risk", ["high", "medium", "low"])
def test_the_models_own_request_for_review_is_always_honoured(risk):
    """Escalate-only: the override adds review, it never removes it."""
    assert _needs_review(_finding(risk_level=risk, requires_human_review=True)) is True


@pytest.mark.parametrize("risk", ["medium", "low"])
def test_lower_risk_findings_still_follow_the_models_judgment(risk):
    """Not a blanket force-everything. Routing every finding to a person would make the
    review queue meaningless and is not what this protects."""
    assert _needs_review(_finding(risk_level=risk, requires_human_review=False)) is False


def test_the_override_is_recorded_rather_than_silent():
    """A reader of the audit trail should be able to see that the platform overruled
    the model, and how often -- otherwise a provider that never flags anything looks
    identical to one that always does."""
    import inspect

    from app.agents.consent_agent.nodes import create_findings

    body = inspect.getsource(create_findings.create_findings)
    assert 'meta["escalated_to_human_review"] = escalated' in body
    assert "escalated += 1" in body


def test_the_finding_is_copied_not_mutated():
    """`response.findings` is read again afterwards for `meta["risk_levels"]`; mutating
    items in place while iterating is how that kind of reporting quietly drifts."""
    import inspect

    from app.agents.consent_agent.nodes import create_findings

    body = inspect.getsource(create_findings.create_findings)
    assert "model_copy(update=" in body
