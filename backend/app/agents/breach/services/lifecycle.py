"""The incident state machine (prompt §32).

Pure: a status and a target in, a yes or no out, no database and no side effects. One
place defines what "REPORTED -> CLOSED must not happen" means.

THE INVARIANTS THIS EXISTS TO HOLD
----------------------------------
  * CLOSED is reachable only from CLOSURE_REVIEW. An incident is not finished because
    the fire is out; it is finished when somebody has looked at the whole thing and
    said so. This is the guard §32 calls out by name.

  * RESPONDING is reachable only from APPROVED. Containment actions are disruptive and
    sometimes irreversible -- disabling an account, isolating a service. Approval is
    the only door in.

  * COMMUNICATION_PENDING cannot be skipped on the way to closure. An incident that
    reached containment without anyone considering who needed telling is exactly the
    failure this agent exists to prevent, so the happy path runs through it.

WHY SO MANY BACKWARD EDGES
--------------------------
Incident response is not a pipeline. New evidence arrives after the risk assessment;
a containment action reveals a second affected system; a reviewer sends a finding back
for more work. An agent that could only move forward would force people to lie about
where they were, so investigation states are mutually reachable and later states can
fall back into them.
"""

from __future__ import annotations

from app.agents.breach.errors import InvalidIncidentTransitionError
from app.agents.breach.schemas import incident

__all__ = [
    "allowed_transitions",
    "assert_transition",
    "can_transition",
    "is_terminal",
    "requires_error_code",
]

# Read each line as "from X you may go to ...".
_TRANSITIONS: dict[str, frozenset[str]] = {
    incident.REPORTED: frozenset({incident.VALIDATING, incident.INVESTIGATING}),
    # Validation can reject outright: an alert that turns out to be a test, a
    # duplicate, or nothing at all. That is a real outcome, not a failure.
    incident.VALIDATING: frozenset({incident.INVESTIGATING, incident.REJECTED}),
    # The investigation states are mutually reachable. Evidence does not arrive in the
    # order a diagram would like, and impact assessment routinely sends you back to
    # the logs.
    incident.INVESTIGATING: frozenset({
        incident.IMPACT_ASSESSMENT, incident.RISK_ASSESSMENT, incident.REVIEW_REQUIRED,
        incident.REJECTED,
    }),
    incident.IMPACT_ASSESSMENT: frozenset({
        incident.INVESTIGATING, incident.RISK_ASSESSMENT, incident.REVIEW_REQUIRED,
    }),
    incident.RISK_ASSESSMENT: frozenset({
        incident.INVESTIGATING, incident.IMPACT_ASSESSMENT, incident.REVIEW_REQUIRED,
        incident.RESPONSE_PENDING,
    }),
    incident.REVIEW_REQUIRED: frozenset({
        incident.INVESTIGATING, incident.IMPACT_ASSESSMENT, incident.RISK_ASSESSMENT,
        incident.RESPONSE_PENDING, incident.REJECTED, incident.CLOSURE_REVIEW,
    }),
    # A plan exists. Either it needs approving, or it contains nothing that does.
    incident.RESPONSE_PENDING: frozenset({
        incident.APPROVAL_REQUIRED, incident.APPROVED, incident.REVIEW_REQUIRED,
    }),
    incident.APPROVAL_REQUIRED: frozenset({
        incident.APPROVED, incident.REVIEW_REQUIRED, incident.RESPONSE_PENDING,
    }),
    # The only edge into RESPONDING. Re-planning can also pull an incident back out of
    # APPROVED, because an approval authorizes specific actions rather than a case.
    incident.APPROVED: frozenset({
        incident.RESPONDING, incident.APPROVAL_REQUIRED, incident.RESPONSE_PENDING,
    }),
    incident.RESPONDING: frozenset({incident.VERIFYING}),
    # Verification can send you back: a containment that did not hold, or that
    # uncovered something new.
    incident.VERIFYING: frozenset({
        incident.COMMUNICATION_PENDING, incident.RESPONSE_PENDING, incident.INVESTIGATING,
    }),
    incident.COMMUNICATION_PENDING: frozenset({
        incident.CLOSURE_REVIEW, incident.REVIEW_REQUIRED,
    }),
    # The only edge into CLOSED.
    incident.CLOSURE_REVIEW: frozenset({
        incident.CLOSED, incident.INVESTIGATING, incident.REVIEW_REQUIRED,
    }),
    incident.CLOSED: frozenset(),
    # Exception states. ESCALATED and FAILED are recoverable -- an escalation is a
    # routing decision, and a failed investigation step can be retried.
    incident.FAILED: frozenset({
        incident.INVESTIGATING, incident.REVIEW_REQUIRED, incident.ESCALATED,
    }),
    incident.ESCALATED: frozenset({
        incident.INVESTIGATING, incident.REVIEW_REQUIRED, incident.RESPONSE_PENDING,
        incident.CLOSURE_REVIEW,
    }),
    incident.REJECTED: frozenset(),
    incident.PARTIALLY_COMPLETED: frozenset(),
    incident.CANCELLED: frozenset(),
}

# Reachable from any non-terminal status rather than repeated in every row. Something
# can go wrong, be escalated, or be cancelled at any point in an incident.
_ALWAYS_REACHABLE = frozenset({
    incident.FAILED, incident.ESCALATED, incident.CANCELLED,
    incident.PARTIALLY_COMPLETED,
})


def allowed_transitions(from_status: str) -> frozenset[str]:
    """Every status reachable from `from_status`. Empty for a terminal status."""
    if from_status in incident.TERMINAL_STATUSES:
        return frozenset()
    return _TRANSITIONS.get(from_status, frozenset()) | _ALWAYS_REACHABLE


def can_transition(from_status: str, to_status: str) -> bool:
    return to_status in allowed_transitions(from_status)


def assert_transition(from_status: str, to_status: str) -> None:
    """Raise unless the move is legal. The single enforcement point for §32."""
    if to_status not in incident.ALL_STATUSES:
        raise InvalidIncidentTransitionError(f"{to_status!r} is not an incident status")
    if from_status in incident.TERMINAL_STATUSES:
        raise InvalidIncidentTransitionError(
            f"incident is {from_status} (terminal); it cannot move to {to_status}"
        )
    if not can_transition(from_status, to_status):
        raise InvalidIncidentTransitionError(
            f"cannot move an incident from {from_status!r} to {to_status!r}; "
            f"legal next states are {sorted(allowed_transitions(from_status))}"
        )


def requires_error_code(to_status: str) -> bool:
    """Statuses that must carry a domain code explaining why the incident stopped.

    REJECTED and ESCALATED are excluded deliberately. Rejecting an alert as not an
    incident is a conclusion a human reached, and escalation is a routing decision --
    both carry a reason in the audit entry rather than an error code, because neither
    is a system failure.
    """
    return to_status in (incident.FAILED, incident.PARTIALLY_COMPLETED)


def is_terminal(status: str) -> bool:
    return status in incident.TERMINAL_STATUSES
