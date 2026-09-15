"""The incident state machine (prompt §32) and the incident vocabulary.

The point is not that the transition table contains what somebody typed into it. It
is that the structural guarantees hold however the table is later edited: containment
only through approval, closure only through a closure review, and uncertainty that
cannot quietly become certainty.
"""

import pathlib
import re

import pytest

from app.agents.breach.errors import InvalidIncidentTransitionError
from app.agents.breach.schemas import incident
from app.agents.breach.services import lifecycle

MIGRATION = pathlib.Path(__file__).parent.parent / "migrations" / "0015_breach_agent.sql"


def _check_values(table: str, column: str) -> set[str]:
    """Pull a CHECK (col in (...)) list out of the migration so the Python constants
    and the database constraint cannot drift apart unnoticed."""
    sql = MIGRATION.read_text(encoding="utf-8")
    body = re.search(rf"create table if not exists {table} \((.*?)\n\);", sql, re.DOTALL)
    assert body, f"{table} not found in the migration"
    check = re.search(rf"{column}\s+text[^,]*?in \(([^)]*)\)", body.group(1), re.DOTALL)
    assert check, f"no CHECK on {table}.{column}"
    return set(re.findall(r"'([a-z_]+)'", check.group(1)))


# ── The structural guarantees ────────────────────────────────────────────────────

def test_closed_is_only_reachable_from_closure_review():
    """§32 calls this out by name: REPORTED -> CLOSED must not happen. An incident is
    finished when somebody has reviewed the whole thing, not when the fire is out."""
    sources = [s for s in incident.ALL_STATUSES if lifecycle.can_transition(s, incident.CLOSED)]
    assert sources == [incident.CLOSURE_REVIEW]


def test_responding_is_only_reachable_from_approved():
    """Containment is disruptive and sometimes irreversible. Approval is the only
    door in."""
    sources = [s for s in incident.ALL_STATUSES if lifecycle.can_transition(s, incident.RESPONDING)]
    assert sources == [incident.APPROVED]


def test_verifying_is_only_reachable_from_responding():
    """You cannot verify a containment that was never performed."""
    sources = [s for s in incident.ALL_STATUSES if lifecycle.can_transition(s, incident.VERIFYING)]
    assert sources == [incident.RESPONDING]


@pytest.mark.parametrize(
    "from_status,to_status",
    [
        # The example §32 names.
        (incident.REPORTED, incident.CLOSED),
        # Every other route around the closure review.
        (incident.INVESTIGATING, incident.CLOSED),
        (incident.RESPONDING, incident.CLOSED),
        (incident.VERIFYING, incident.CLOSED),
        (incident.COMMUNICATION_PENDING, incident.CLOSED),
        # Around the approval gate.
        (incident.REPORTED, incident.RESPONDING),
        (incident.RISK_ASSESSMENT, incident.RESPONDING),
        (incident.RESPONSE_PENDING, incident.RESPONDING),
        (incident.APPROVAL_REQUIRED, incident.RESPONDING),
        # Claiming verification without acting.
        (incident.APPROVED, incident.VERIFYING),
        (incident.RESPONSE_PENDING, incident.VERIFYING),
    ],
)
def test_illegal_transitions_are_refused(from_status, to_status):
    assert not lifecycle.can_transition(from_status, to_status)
    with pytest.raises(InvalidIncidentTransitionError):
        lifecycle.assert_transition(from_status, to_status)


def test_communication_cannot_be_skipped_on_the_happy_path():
    """An incident that reached containment without anyone considering who needed
    telling is the failure this agent exists to prevent, so the route from
    verification to closure runs through COMMUNICATION_PENDING."""
    assert not lifecycle.can_transition(incident.VERIFYING, incident.CLOSURE_REVIEW)
    assert lifecycle.can_transition(incident.VERIFYING, incident.COMMUNICATION_PENDING)
    assert lifecycle.can_transition(incident.COMMUNICATION_PENDING, incident.CLOSURE_REVIEW)


# ── Investigation is not a pipeline ──────────────────────────────────────────────

def test_investigation_states_can_move_between_each_other():
    """Evidence does not arrive in the order a diagram would like. Impact assessment
    routinely sends you back to the logs, and risk assessment back to impact."""
    assert lifecycle.can_transition(incident.IMPACT_ASSESSMENT, incident.INVESTIGATING)
    assert lifecycle.can_transition(incident.RISK_ASSESSMENT, incident.IMPACT_ASSESSMENT)
    assert lifecycle.can_transition(incident.REVIEW_REQUIRED, incident.INVESTIGATING)


def test_verification_can_send_an_incident_back():
    """A containment that did not hold, or that uncovered a second affected system."""
    assert lifecycle.can_transition(incident.VERIFYING, incident.RESPONSE_PENDING)
    assert lifecycle.can_transition(incident.VERIFYING, incident.INVESTIGATING)


def test_replanning_can_pull_an_incident_back_out_of_approved():
    """An approval authorizes specific actions, not an incident, so superseding a
    plan must be able to void it."""
    assert lifecycle.can_transition(incident.APPROVED, incident.APPROVAL_REQUIRED)
    assert lifecycle.can_transition(incident.APPROVED, incident.RESPONSE_PENDING)


def test_a_closure_review_can_reopen_an_investigation():
    assert lifecycle.can_transition(incident.CLOSURE_REVIEW, incident.INVESTIGATING)


# ── Terminal states ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("status", sorted(incident.TERMINAL_STATUSES))
def test_terminal_statuses_go_nowhere(status):
    assert lifecycle.allowed_transitions(status) == frozenset()
    assert lifecycle.is_terminal(status)
    with pytest.raises(InvalidIncidentTransitionError):
        lifecycle.assert_transition(status, incident.INVESTIGATING)


def test_rejected_is_terminal_and_is_a_real_outcome():
    """An alert investigated and found not to be an incident is a conclusion, not a
    failure -- so it is terminal and needs no error code (§44 scenario 4)."""
    assert lifecycle.is_terminal(incident.REJECTED)
    assert not lifecycle.requires_error_code(incident.REJECTED)


def test_failed_and_escalated_are_recoverable():
    assert not lifecycle.is_terminal(incident.FAILED)
    assert not lifecycle.is_terminal(incident.ESCALATED)
    assert lifecycle.can_transition(incident.FAILED, incident.INVESTIGATING)
    assert lifecycle.can_transition(incident.ESCALATED, incident.REVIEW_REQUIRED)


@pytest.mark.parametrize(
    "status", sorted(set(incident.HAPPY_PATH_STATUSES) - {incident.CLOSED})
)
def test_every_active_status_can_fail_or_be_cancelled(status):
    """A status from which nothing could go wrong would be a place an incident could
    get stuck silently."""
    assert lifecycle.can_transition(status, incident.FAILED)
    assert lifecycle.can_transition(status, incident.CANCELLED)


# ── Confidence: uncertainty cannot become certainty on its own ───────────────────

def test_only_a_human_may_assert_confirmed():
    """§12: do not convert uncertainty into confirmed facts. No rule, model or
    heuristic may promote a finding to confirmed -- that is a statement an
    organisation makes with a name against it."""
    assert incident.CONFIRMED not in incident.MACHINE_ASSERTABLE_CONFIDENCE
    assert incident.PROBABLE in incident.MACHINE_ASSERTABLE_CONFIDENCE
    assert incident.POSSIBLE in incident.MACHINE_ASSERTABLE_CONFIDENCE
    assert incident.UNKNOWN in incident.MACHINE_ASSERTABLE_CONFIDENCE


def test_confidence_is_ordered_so_comparisons_are_not_hard_coded():
    assert incident.at_least(incident.CONFIRMED, incident.PROBABLE)
    assert incident.at_least(incident.PROBABLE, incident.PROBABLE)
    assert not incident.at_least(incident.POSSIBLE, incident.PROBABLE)
    assert not incident.at_least(incident.UNKNOWN, incident.POSSIBLE)


def test_an_incident_starts_knowing_nothing():
    """The default for both substantive questions is `unknown`, not `no`. "We have no
    evidence of personal data" and "no personal data was involved" are different
    claims and only one of them is true at intake."""
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "personal_data_involved text not null default 'unknown'" in sql
    assert "breach_confirmed       text not null default 'unknown'" in sql


# ── Vocabulary agrees with the database ──────────────────────────────────────────

def test_status_constants_match_the_migration():
    assert _check_values("incident_cases", "status") == set(incident.ALL_STATUSES)


def test_incident_type_constants_match_the_migration():
    assert _check_values("incident_cases", "incident_type") == set(incident.INCIDENT_TYPES)


def test_source_constants_match_the_migration():
    assert _check_values("incident_cases", "source") == set(incident.INCIDENT_SOURCES)


def test_evidence_kind_constants_match_the_migration():
    assert _check_values("incident_evidence", "kind") == set(incident.EVIDENCE_KINDS)


def test_action_kind_constants_match_the_migration():
    assert _check_values("incident_actions", "action_kind") == set(incident.ACTION_KINDS)


def test_audience_constants_match_the_migration():
    assert _check_values("incident_communications", "audience") == set(
        incident.COMMUNICATION_AUDIENCES
    )


def test_unclassified_is_never_a_classification_outcome():
    assert incident.TYPE_UNCLASSIFIED not in incident.CLASSIFIABLE_TYPES
    assert incident.TYPE_UNCLASSIFIED in incident.INCIDENT_TYPES


def test_error_code_is_required_only_where_something_actually_broke():
    assert lifecycle.requires_error_code(incident.FAILED)
    assert lifecycle.requires_error_code(incident.PARTIALLY_COMPLETED)
    # Neither of these is a system failure.
    assert not lifecycle.requires_error_code(incident.REJECTED)
    assert not lifecycle.requires_error_code(incident.ESCALATED)
    assert not lifecycle.requires_error_code(incident.CLOSED)


# ── Tenant isolation is not optional ─────────────────────────────────────────────

def test_every_incident_table_has_rls_and_a_tenant_policy():
    from app.db import models

    sql = MIGRATION.read_text(encoding="utf-8")
    tables = {t for t in models.Base.metadata.tables if t.startswith("incident_")}
    assert len(tables) == 12

    rls = set(re.findall(r"alter table (\w+)\s+enable row level security", sql))
    assert tables <= rls, f"no RLS on {sorted(tables - rls)}"

    loop = re.search(r"foreach t in array array\[(.*?)\]", sql, re.DOTALL)
    assert loop
    policied = set(re.findall(r"'(\w+)'", loop.group(1)))
    assert tables <= policied, f"no tenant_isolation policy for {sorted(tables - policied)}"


def test_every_incident_table_carries_an_org_id():
    from app.db import models

    for name, table in models.Base.metadata.tables.items():
        if name.startswith("incident_"):
            assert "org_id" in table.columns, f"{name} cannot be tenant-scoped"


def test_evidence_and_approvals_are_append_only():
    """The two records an investigation's credibility rests on. If either can be
    rewritten after the fact, the incident report is a story rather than a finding."""
    sql = MIGRATION.read_text(encoding="utf-8")
    for table in ("incident_evidence", "incident_approvals"):
        assert f"create trigger {table}_append_only" in sql
    # Executions are deliberately NOT append-only: a row legitimately moves
    # pending -> running -> verified in place and IS the idempotency ledger.
    assert "create trigger incident_executions_append_only" not in sql
