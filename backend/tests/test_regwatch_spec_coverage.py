"""The three requirements in the Agent 5 document that the build had not met.

Found by reading the functional draft back against the code rather than against my own
notes. Each was a stated requirement with no implementation behind it -- not a bug, and
not visible from any test, because nothing had ever asserted the requirement.

  section 3  "Previous Reviews" is a listed INPUT to the agent. Nothing read it.
  section 8  The compliance view shows "jurisdiction and source". The finding payload
             carried a source UUID and nothing else.
  section 8  The compliance view shows an "Audit timeline". The endpoint existed and
             the console never called it.
"""

import inspect
import uuid

import pytest

from app.agents.regwatch.schemas import watch
from app.agents.regwatch.services import assessment_service
from app.api.v1.routes import regwatch as routes
from app.db.repositories import regwatch_repository as repo

FRONTEND = "frontend/src/components/RegWatchConsole.tsx"


def _console() -> str:
    import pathlib

    return (pathlib.Path(__file__).resolve().parents[2] / FRONTEND).read_text(encoding="utf-8")


# ── Section 3: previous reviews are an input ────────────────────────────────────

def test_previous_human_decisions_are_read_at_assessment_time():
    """The failure this prevents: a lead dismisses a change from a regulator with a
    written reason; three weeks later the same source changes again and the queue
    shows no trace of that decision, so the same question is worked from scratch."""
    body = inspect.getsource(assessment_service.assess)
    assert "prior_decisions_note" in body


def test_the_prior_decision_quotes_the_reviewers_own_reason():
    body = inspect.getsource(assessment_service.prior_decisions_note)
    assert "approval.reason" in body
    assert "latest_approval" in body


def test_prior_decisions_are_context_and_never_a_decision():
    """A previous dismissal must not suppress the new finding, pre-set its relevance,
    or carry the old confidence forward. It is a thing to read."""
    body = inspect.getsource(assessment_service.prior_decisions_note)
    assert "context, not a decision" in body
    # It returns prose. It touches no field that drives the outcome.
    for field in ("finding.relevance", "finding.priority", "finding.status",
                  "requires_human_review"):
        assert field not in body


def test_only_findings_from_the_same_source_count_as_prior_context():
    """A decision about a different regulator says nothing about this one."""
    body = inspect.getsource(assessment_service.prior_decisions_note)
    assert "f.source_id == finding.source_id" in body
    assert "f.id != finding.id" in body


def test_a_source_reviewed_for_the_first_time_gets_no_note():
    body = inspect.getsource(assessment_service.prior_decisions_note)
    assert "if not decided:" in body
    assert "return None" in body


# ── Section 8: jurisdiction and source on the compliance view ───────────────────

def test_the_finding_payload_carries_the_source_not_just_its_id():
    body = inspect.getsource(routes._finding_response)
    assert '"source": source' in body
    assert '"source_id"' in body  # kept: the reference itself is still useful


def test_both_finding_endpoints_resolve_the_source():
    """A list row needs it as much as a detail view -- triage happens on the list."""
    for endpoint in (routes.list_findings, routes.get_finding):
        body = inspect.getsource(endpoint)
        assert "source_identity_map" in body, f"{endpoint.__name__} does not resolve the source"


def test_the_source_lookup_is_org_scoped():
    body = inspect.getsource(repo.source_identity_map)
    assert "RegWatchSource.org_id == org_id" in body


def test_the_source_lookup_is_one_query_not_one_per_finding():
    """A findings page references the same handful of sources repeatedly."""
    body = inspect.getsource(repo.source_identity_map)
    assert body.count("await db.execute") == 1


def test_the_console_shows_the_source_on_every_row_and_in_the_detail():
    console = _console()
    assert "f.source ? `${f.source.name}" in console
    assert "finding.source.authority" in console
    assert "finding.source.jurisdiction" in console


def test_a_removed_source_does_not_blank_the_row():
    """`source` is nullable in the payload, so the console has to say something."""
    console = _console()
    assert '"source removed"' in console


# ── Section 8 / 10: the audit timeline ──────────────────────────────────────────

def test_the_console_loads_the_audit_timeline_with_the_finding():
    """The endpoint and the client method both existed for weeks; no component ever
    called them, so the required timeline was simply absent from the screen."""
    console = _console()
    assert "regwatchApi.audit(" in console
    assert "setTimeline" in console


def test_the_timeline_is_rendered_not_merely_fetched():
    console = _console()
    assert "Audit timeline" in console
    assert "timeline.map(" in console
    assert "No audit entries recorded" in console


def test_the_timeline_says_whether_a_person_or_the_agent_acted():
    """Section 9 puts interpretation under human control; a timeline that does not
    distinguish the two cannot show that it was."""
    console = _console()
    assert "a person" in console and "the agent" in console
    assert "e.actor_user_id" in console


@pytest.mark.parametrize("action", [
    watch.AUDIT_FINDING_CREATED, watch.AUDIT_RELEVANCE_ASSESSED,
    watch.AUDIT_IMPACT_MAPPED, watch.AUDIT_REVIEW_REQUESTED,
    watch.AUDIT_APPROVED, watch.AUDIT_DISMISSED,
    watch.AUDIT_ACTION_CREATED, watch.AUDIT_ACTION_COMPLETED, watch.AUDIT_CLOSED,
])
def test_every_audit_action_a_finding_can_produce_has_a_readable_label(action):
    """The log stores machine actions; the timeline is read by compliance people."""
    console = _console()
    assert f'"{action}":' in console, f"{action} would render as a raw machine string"


# ── The document's own pipeline, end to end ─────────────────────────────────────

SPEC_STEPS = [
    ("1 Source Configuration", "app/agents/regwatch/services/source_service.py", "register_source"),
    ("2 Collect", "app/agents/regwatch/services/collection_service.py", "collect"),
    ("3 Normalize", "app/agents/regwatch/connectors/http_source.py", "normalize"),
    ("4 Detect Changes", "app/agents/regwatch/rules/change_detection.py", "detect"),
    ("5 Relevance Filter", "app/agents/regwatch/rules/relevance.py", "assess"),
    ("6 Interpret", "app/agents/regwatch/services/assessment_service.py", "_interpret"),
    ("7 Impact Mapping", "app/agents/regwatch/services/impact_service.py", "map_impact"),
    ("8 Risk / Priority", "app/agents/regwatch/rules/relevance.py", "priority_for"),
    ("9 Human Review", "app/agents/regwatch/services/review_service.py", "decide"),
    ("10 Action", "app/agents/regwatch/services/review_service.py", "open_actions"),
    ("11 Monitor", "app/agents/regwatch/services/action_sla_service.py", "sweep_overdue"),
    ("12 Audit", "app/db/repositories/regwatch_repository.py", "list_finding_audit"),
]


@pytest.mark.parametrize("step,path,function", SPEC_STEPS)
def test_every_step_of_the_documents_flow_has_executable_code(step, path, function):
    """Section 4 defines twelve steps. This is the cheapest possible guard against one
    of them being quietly removed."""
    import pathlib

    source = (pathlib.Path(__file__).resolve().parents[1] / path).read_text(encoding="utf-8")
    assert f"def {function}" in source, f"step {step} has no {function} in {path}"


def test_the_ai_layer_cannot_invent_a_requirement_or_a_citation():
    """Section 5: the AI explains and contextualises evidence; it must not invent legal
    requirements, citations, dates, jurisdictions or obligations."""
    body = inspect.getsource(assessment_service._interpret)
    # Citations are filtered against what was actually retrieved.
    assert "cid in allowed" in body
    # And prose with nothing behind it is discarded rather than shown.
    assert "if not cited:" in body
    prompt = assessment_service._SYSTEM_PROMPT
    assert "do NOT decide whether any obligation applies" in prompt
    assert "Never invent one" in prompt


def test_relationships_with_all_four_other_agents_are_mapped():
    """Section 12 names what each other agent contributes to impact mapping."""
    from app.agents.regwatch.services import impact_service

    kinds = {k for targets in impact_service._TOPIC_TARGETS.values() for k in targets}
    assert watch.TARGET_CONSENT_WEBSITE in kinds          # Agent 1
    assert watch.TARGET_ROPA_RECORD in kinds              # Agent 2
    assert watch.TARGET_DSR_CONFIG in kinds               # Agent 3
    assert watch.TARGET_INCIDENT in kinds                 # Agent 4


def test_nothing_customer_visible_bypasses_the_approval_gate():
    """Section 9: production-impacting actions must not bypass approval."""
    from app.agents.regwatch.services import review_service

    body = inspect.getsource(review_service.open_actions)
    assert "watch.APPROVED" in body
    assert "approve it first" in body


def _uuid():
    return uuid.uuid4()
