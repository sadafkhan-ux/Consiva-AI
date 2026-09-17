"""Agent 5's finding lifecycle, and the vocabulary it shares with the database.

The graph checks here are the generic ones — every state reachable, no dead ends, no
orphans — because that is the class of bug that bit Agent 3 (a missing
SEARCH_COMPLETED -> APPROVED edge crashed a worker) and Agent 4 (a missing
INVESTIGATING -> RESPONSE_PENDING edge marked completed analyses FAILED). Both were
found by running the thing, not by a test asserting the transitions somebody thought
of, so these ask the questions that do not depend on anyone's imagination.
"""

import itertools
import re
from pathlib import Path

import pytest

from app.agents.regwatch.errors import InvalidWatchTransitionError, WatchNotReadyError
from app.agents.regwatch.schemas import watch
from app.agents.regwatch.services import lifecycle

BACKEND = Path(__file__).resolve().parents[1]
MIGRATION = (BACKEND / "migrations" / "0019_regwatch_agent.sql").read_text(encoding="utf-8")


def _walk(graph, start):
    seen, stack = set(), [start]
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        stack.extend(graph.get(node, frozenset()))
    return seen


GRAPH = {s: lifecycle.allowed_transitions(s) for s in watch.ALL_STATUSES}
TERMINAL = {s for s in watch.ALL_STATUSES if lifecycle.is_terminal(s)}


# ── The generic graph properties ────────────────────────────────────────────────

def test_every_status_is_reachable_from_the_start():
    reachable = _walk(GRAPH, watch.DETECTED)
    unreachable = sorted(set(GRAPH) - reachable)
    assert not unreachable, f"no path from {watch.DETECTED} to: {unreachable}"


def test_no_status_is_a_dead_end():
    """A finding that can enter a state and never leave it, without that state being
    terminal, is a compliance record stuck forever."""
    trapped = sorted(
        s for s in GRAPH
        if s not in TERMINAL and not (_walk(GRAPH, s) & TERMINAL)
    )
    assert not trapped, f"cannot reach any terminal state from: {trapped}"


def test_no_status_has_no_way_out_unless_it_is_terminal():
    stuck = sorted(s for s in GRAPH if s not in TERMINAL and not GRAPH[s])
    assert not stuck, f"no outgoing edges but not terminal: {stuck}"


def test_no_status_is_declared_but_unused():
    in_graph = set(GRAPH) | {d for edges in GRAPH.values() for d in edges}
    orphans = sorted(set(watch.ALL_STATUSES) - in_graph)
    assert not orphans, f"in the vocabulary but in no edge: {orphans}"


def test_the_code_vocabulary_matches_the_database_constraint():
    """A status the code can write that the database rejects is a write that fails
    only against a real database."""
    constrained = set(re.findall(
        r"regwatch_findings_status_check\s*\n?\s*check \(status in \(([^)]*)\)\)",
        MIGRATION, re.DOTALL,
    )[0].replace("'", "").replace("\n", "").replace(" ", "").split(","))
    assert constrained == set(watch.ALL_STATUSES), (
        f"code has {sorted(set(watch.ALL_STATUSES) - constrained)} the database rejects; "
        f"database has {sorted(constrained - set(watch.ALL_STATUSES))} the code never writes"
    )


# ── The invariants that carry meaning ───────────────────────────────────────────

def test_follow_up_work_only_exists_because_somebody_approved_it():
    into_action = [s for s in GRAPH if watch.ACTION_OPEN in GRAPH[s]]
    assert into_action == [watch.APPROVED], (
        f"ACTION_OPEN is reachable from {into_action}; work that appears without an "
        "approval is work nobody asked for"
    )


def test_closing_requires_approval_first():
    into_closed = sorted(s for s in GRAPH if watch.CLOSED in GRAPH[s])
    assert into_closed == sorted([watch.ACTION_OPEN, watch.APPROVED]), into_closed
    assert not lifecycle.can_transition(watch.DETECTED, watch.CLOSED)
    assert not lifecycle.can_transition(watch.REVIEW_REQUIRED, watch.CLOSED)


def test_a_decision_is_final():
    """Dismissed and closed are both terminal. A later change raises a new finding
    rather than reviving a decision somebody already made."""
    for decided in (watch.DISMISSED, watch.CLOSED):
        assert lifecycle.is_terminal(decided)
        assert lifecycle.allowed_transitions(decided) == frozenset()


def test_failed_is_not_terminal():
    """A collection that broke can be retried; it must not leave a dead row."""
    assert not lifecycle.is_terminal(watch.FAILED)
    assert lifecycle.allowed_transitions(watch.FAILED)


def test_failing_requires_a_reason():
    assert lifecycle.requires_error_code(watch.FAILED)
    for other in (watch.APPROVED, watch.CLOSED, watch.DISMISSED, watch.REVIEW_REQUIRED):
        assert not lifecycle.requires_error_code(other)


# ── Superseding: a re-check must not overwrite a person ─────────────────────────

def test_only_undecided_findings_can_be_superseded():
    """A source that changes again before anyone reviewed the last change should not
    leave two live findings. But once a human has approved or dismissed, their
    decision stands — the next poll does not quietly retire it."""
    for undecided in (watch.DETECTED, watch.ASSESSING, watch.REVIEW_REQUIRED):
        assert lifecycle.can_be_superseded(undecided), undecided
    for decided in (watch.APPROVED, watch.ACTION_OPEN, watch.CLOSED, watch.DISMISSED):
        assert not lifecycle.can_be_superseded(decided), (
            f"{decided} can be superseded; a re-check would erase a human decision"
        )


def test_superseded_is_only_reachable_from_undecided_states():
    into_superseded = {s for s in GRAPH if watch.SUPERSEDED in GRAPH[s]}
    assert into_superseded <= lifecycle.SUPERSEDABLE, (
        f"SUPERSEDED reachable from {sorted(into_superseded - lifecycle.SUPERSEDABLE)}, "
        "which a human has already decided"
    )


# ── The gate ────────────────────────────────────────────────────────────────────

def test_a_finding_is_decided_only_from_review_required():
    lifecycle.assert_reviewable(watch.REVIEW_REQUIRED)
    for wrong in (watch.DETECTED, watch.ASSESSING, watch.APPROVED, watch.CLOSED):
        with pytest.raises(WatchNotReadyError):
            lifecycle.assert_reviewable(wrong)


def test_assert_transition_refuses_an_unknown_status():
    with pytest.raises(InvalidWatchTransitionError):
        lifecycle.assert_transition(watch.DETECTED, "somewhere_else")


def test_assert_transition_refuses_a_move_to_the_same_status():
    with pytest.raises(InvalidWatchTransitionError, match="already"):
        lifecycle.assert_transition(watch.ASSESSING, watch.ASSESSING)


@pytest.mark.parametrize(
    ("origin", "destination"),
    [(a, b) for a, b in itertools.product(sorted(watch.ALL_STATUSES), repeat=2)
     if b not in lifecycle.allowed_transitions(a) and a != b and b in watch.ALL_STATUSES][:40],
)
def test_every_illegal_move_is_refused(origin, destination):
    with pytest.raises(InvalidWatchTransitionError):
        lifecycle.assert_transition(origin, destination)


# ── The confidence vocabulary, shared with Agent 4 ──────────────────────────────

def test_confirmed_is_not_machine_assertable():
    """Deciding a regulation definitely applies is a position the organisation takes,
    with a name against it. No rule or model reaches it."""
    assert watch.CONFIRMED not in watch.MACHINE_ASSERTABLE_CONFIDENCE
    assert watch.MACHINE_ASSERTABLE_CONFIDENCE == frozenset(
        {watch.PROBABLE, watch.POSSIBLE, watch.UNKNOWN}
    )


def test_the_confidence_vocabulary_is_agent_4s_exactly():
    """Same question shape, same answers. Divergence here would mean two vocabularies
    a reviewer has to hold in their head at once."""
    from app.agents.breach.schemas import incident

    assert watch.CONFIDENCE_LEVELS == incident.CONFIDENCE_LEVELS
    assert watch.CONFIDENCE_ORDER == incident.CONFIDENCE_ORDER
    assert watch.MACHINE_ASSERTABLE_CONFIDENCE == incident.MACHINE_ASSERTABLE_CONFIDENCE


def test_at_least_orders_confidence_correctly():
    assert watch.at_least(watch.CONFIRMED, watch.PROBABLE)
    assert watch.at_least(watch.PROBABLE, watch.PROBABLE)
    assert not watch.at_least(watch.POSSIBLE, watch.PROBABLE)
    assert not watch.at_least(watch.UNKNOWN, watch.POSSIBLE)


# ── Relevance and collection: the honesty defaults ──────────────────────────────

def test_relevance_has_a_third_value_for_not_knowing():
    """Two values would force every unmatched change to be called irrelevant."""
    assert watch.UNDETERMINED in watch.RELEVANCE_VALUES
    assert len(watch.RELEVANCE_VALUES) == 3


def test_a_source_that_failed_collection_is_not_current():
    """The guardrail the spec ends on: the system must not report a source as current
    when collection failed."""
    assert watch.COLLECTION_FAILED in watch.COLLECTION_NOT_CURRENT
    assert watch.COLLECTION_PENDING in watch.COLLECTION_NOT_CURRENT
    assert watch.COLLECTION_SKIPPED in watch.COLLECTION_NOT_CURRENT
    assert watch.COLLECTION_COLLECTED not in watch.COLLECTION_NOT_CURRENT


def test_an_unreachable_source_still_raises_a_finding():
    """Silence about a source nobody could read is the failure mode this agent is
    supposed to prevent."""
    assert watch.CHANGE_UNREACHABLE in watch.CHANGE_KINDS_RAISING_A_FINDING
    assert watch.CHANGE_NONE not in watch.CHANGE_KINDS_RAISING_A_FINDING


# ── Database-level guarantees written into the migration ────────────────────────

def test_the_baseline_cannot_advance_without_a_named_person():
    """If the agent moved its own baseline, a change would be reported once and then
    silently absorbed."""
    assert re.search(r"approved_by_user_id\s+uuid\s+not null", MIGRATION), (
        "regwatch_baselines.approved_by_user_id is no longer NOT NULL; the agent can "
        "now advance its own baseline and absorb a change nobody saw"
    )


def test_a_collected_row_must_carry_content_and_a_failed_row_a_reason():
    assert "regwatch_collections_collected_has_content" in MIGRATION
    assert "regwatch_collections_failed_has_reason" in MIGRATION


def test_collections_and_approvals_are_append_only():
    """Evidence that can be rewritten makes every change record downstream of it
    unfalsifiable."""
    assert "regwatch_approvals" in MIGRATION
    triggers = re.findall(r"array\['regwatch_approvals', 'regwatch_collections'\]", MIGRATION)
    assert triggers, "the append-only trigger list no longer covers both tables"


def test_there_is_no_job_type_that_performs_regulatory_work():
    """An action is carried out by a person and attested to, as Agent 4's containment
    is. A queued job claiming to do it would be a button that pretends."""
    # Parse the CHECK clause, not the whole file. Two traps here, both hit: the
    # comment above the constraint names the job type that deliberately does not
    # exist, and the comments INSIDE it contain parentheses -- `-- Agent 1 (Consent)`
    # -- which end a naive `[^)]*` capture early.
    block = MIGRATION.split("add constraint agent_jobs_job_type_check")[-1]
    block = " ".join(line.split("--")[0] for line in block.splitlines())
    allowed = set(re.findall(r"'([a-z_]+)'", block.split(");")[0]))

    assert {"regwatch_collect", "regwatch_assess"} <= allowed, allowed
    stray = {j for j in allowed if j.startswith("regwatch_")} - {
        "regwatch_collect", "regwatch_assess"}
    assert not stray, f"a regwatch job type that performs work rather than observes: {stray}"
    # And every other agent's job types survived the widening.
    assert {"scan", "analyze", "ropa_discovery", "dsr_search", "dsr_execute",
            "incident_analysis"} <= allowed, allowed
