"""DSR case state machine (prompt §12).

The point of these tests is not that the transition table has the entries someone
typed into it -- it is that the two structural guarantees hold no matter how the
table is later edited: execution is only reachable through approval, and completion
is only reachable through a response.
"""

import pathlib
import re

import pytest

from app.agents.dsr.errors import InvalidCaseTransitionError
from app.agents.dsr.schemas import case
from app.agents.dsr.services import lifecycle

MIGRATION = pathlib.Path(__file__).parent.parent / "migrations" / "0011_dsr_agent.sql"


def _check_values(table: str, column: str) -> set[str]:
    """Pull a CHECK (col in (...)) list straight out of the migration, so the Python
    constants and the database constraint cannot drift apart unnoticed."""
    sql = MIGRATION.read_text(encoding="utf-8")
    table_body = re.search(rf"create table if not exists {table} \((.*?)\n\);", sql, re.DOTALL)
    assert table_body, f"{table} not found in the migration"
    check = re.search(rf"{column}\s+in \(([^)]*)\)", table_body.group(1), re.DOTALL)
    assert check, f"no CHECK on {table}.{column}"
    return set(re.findall(r"'([a-z_]+)'", check.group(1)))


# ── The two structural guarantees ────────────────────────────────────────────────

def test_executing_is_only_reachable_from_approved():
    """§23: approval is not a formality execution may skip when the answer looks
    obvious. It is the only door into EXECUTING."""
    sources = [s for s in case.ALL_STATUSES if lifecycle.can_transition(s, case.EXECUTING)]
    assert sources == [case.APPROVED]


def test_completed_is_only_reachable_from_response_pending():
    """A case is finished when the requester has been answered, not when the
    database write succeeded."""
    sources = [s for s in case.ALL_STATUSES if lifecycle.can_transition(s, case.COMPLETED)]
    assert sources == [case.RESPONSE_PENDING]


@pytest.mark.parametrize(
    "from_status,to_status",
    [
        # The example the prompt calls out by name.
        (case.RECEIVED, case.EXECUTING),
        # Every other shortcut around the approval gate.
        (case.SEARCH_COMPLETED, case.EXECUTING),
        (case.REVIEW_REQUIRED, case.EXECUTING),
        (case.APPROVAL_REQUIRED, case.EXECUTING),
        (case.IDENTITY_VERIFIED, case.EXECUTING),
        # Searching before identity is established.
        (case.RECEIVED, case.SEARCHING),
        (case.IDENTITY_PENDING, case.SEARCHING),
        # Declaring victory early.
        (case.RECEIVED, case.COMPLETED),
        (case.EXECUTING, case.COMPLETED),
        (case.SEARCH_COMPLETED, case.COMPLETED),
    ],
)
def test_illegal_transitions_are_refused(from_status, to_status):
    assert not lifecycle.can_transition(from_status, to_status)
    with pytest.raises(InvalidCaseTransitionError):
        lifecycle.assert_transition(from_status, to_status)


def test_search_requires_identity_or_classification_first():
    """SEARCHING is reachable only from a state where identity has been dealt with
    (verified) or the case has been classified -- never straight from intake."""
    sources = {s for s in case.ALL_STATUSES if lifecycle.can_transition(s, case.SEARCHING)}
    assert case.RECEIVED not in sources
    assert case.IDENTITY_VERIFIED in sources


# ── Terminal states ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("status", sorted(case.TERMINAL_STATUSES))
def test_terminal_statuses_go_nowhere(status):
    assert lifecycle.allowed_transitions(status) == frozenset()
    assert lifecycle.is_terminal(status)
    with pytest.raises(InvalidCaseTransitionError):
        lifecycle.assert_transition(status, case.SEARCHING)


def test_failed_and_escalated_are_recoverable():
    """A connector outage must not permanently kill a case -- a human can put it
    back into the flow, which is why neither is terminal."""
    assert not lifecycle.is_terminal(case.FAILED)
    assert not lifecycle.is_terminal(case.ESCALATED)
    assert lifecycle.can_transition(case.FAILED, case.SEARCHING)
    assert lifecycle.can_transition(case.ESCALATED, case.REVIEW_REQUIRED)


@pytest.mark.parametrize("status", sorted(set(case.HAPPY_PATH_STATUSES) - {case.COMPLETED}))
def test_every_active_status_can_fail_or_be_cancelled(status):
    """§47: a case must always be able to reach an explicit end state. A status from
    which nothing could fail would be a place a case could get stuck silently."""
    assert lifecycle.can_transition(status, case.FAILED)
    assert lifecycle.can_transition(status, case.CANCELLED)


# ── Vocabulary agrees with the database ──────────────────────────────────────────

def test_status_constants_match_the_migration_check_constraint():
    assert _check_values("dsr_requests", "status") == set(case.ALL_STATUSES)


def test_request_type_constants_match_the_migration():
    assert _check_values("dsr_requests", "request_type") == set(case.REQUEST_TYPES)


def test_identity_status_constants_match_the_migration():
    assert _check_values("dsr_identity_verifications", "status") == set(case.IDV_STATUSES)


def test_operation_constants_match_the_migration():
    assert _check_values("dsr_actions", "operation") == set(case.OPERATIONS)


def test_unclassified_is_never_a_classification_outcome():
    """A classifier that cannot decide returns `other` with low confidence, which
    routes to human review -- it never leaves the case looking unprocessed."""
    assert case.UNCLASSIFIED not in case.CLASSIFIABLE_TYPES
    assert case.UNCLASSIFIED in case.REQUEST_TYPES


def test_error_code_is_required_where_a_case_stops_badly():
    for status in (case.FAILED, case.REJECTED, case.EXPIRED, case.PARTIALLY_COMPLETED):
        assert lifecycle.requires_error_code(status)
    # Escalation is a routing decision by a human, not a system failure.
    assert not lifecycle.requires_error_code(case.ESCALATED)
    assert not lifecycle.requires_error_code(case.COMPLETED)
