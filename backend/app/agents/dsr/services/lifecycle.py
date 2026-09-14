"""The DSR case state machine (prompt §12).

This module is deliberately pure: it takes a status and a target and answers whether
the move is legal, with no database, no I/O and no side effects. Everything that
actually moves a case calls `assert_transition` first, so there is exactly one place
that defines what "RECEIVED -> EXECUTING must not be possible" means.

Two rules the transition table encodes that are easy to lose otherwise:

  * You cannot reach EXECUTING except from APPROVED. Not from SEARCH_COMPLETED, not
    from REVIEW_REQUIRED, not from APPROVAL_REQUIRED. Approval is not a formality
    that execution can skip when it is "obvious" -- it is the only door in.

  * You cannot reach COMPLETED except from RESPONSE_PENDING. A case is finished when
    the requester has been answered, not when the database write succeeded.

Exception states are reachable from anywhere non-terminal, because a connector can
fail or a case be cancelled at any point; terminal states are reachable from nothing.
"""

from __future__ import annotations

from app.agents.dsr.errors import InvalidCaseTransitionError
from app.agents.dsr.schemas import case

__all__ = [
    "allowed_transitions",
    "assert_transition",
    "can_transition",
    "is_terminal",
    "requires_error_code",
]


# The happy path, plus the legal detours. Read each line as "from X you may go to ...".
_TRANSITIONS: dict[str, frozenset[str]] = {
    case.RECEIVED: frozenset({case.IDENTITY_PENDING, case.CLASSIFIED}),
    # A case may be classified before or after identity -- classification reads only
    # the requester's own words, never their records, so it is not behind the gate.
    case.IDENTITY_PENDING: frozenset({case.IDENTITY_VERIFIED, case.CLASSIFIED}),
    case.IDENTITY_VERIFIED: frozenset({case.CLASSIFIED, case.SEARCHING}),
    case.CLASSIFIED: frozenset({case.IDENTITY_PENDING, case.IDENTITY_VERIFIED, case.SEARCHING}),
    case.SEARCHING: frozenset({case.SEARCH_COMPLETED}),
    # Search can finish and find nothing. That is SEARCH_COMPLETED with an explicit
    # NO_MATCH error code, then a response -- never a silent stop (§47).
    #
    # APPROVED is reachable directly from here, without passing through
    # APPROVAL_REQUIRED, when the plan contains nothing a reviewer could decide --
    # an access request whose every action is a disclosure, say. Routing such a plan
    # through APPROVAL_REQUIRED would be a lie about the case AND a dead end: the
    # only thing that leaves that status is a decision recorded against an action,
    # and there is no action to decide. What this does NOT do is open a second door
    # into EXECUTING, which is still reachable from APPROVED alone.
    case.SEARCH_COMPLETED: frozenset({
        case.REVIEW_REQUIRED, case.APPROVAL_REQUIRED, case.APPROVED, case.RESPONSE_PENDING,
    }),
    case.REVIEW_REQUIRED: frozenset({
        case.APPROVAL_REQUIRED, case.APPROVED, case.SEARCHING, case.RESPONSE_PENDING,
    }),
    case.APPROVAL_REQUIRED: frozenset({case.APPROVED, case.REVIEW_REQUIRED}),
    # The only edge into EXECUTING -- plus two edges BACKWARD, into stricter states.
    # Building a new plan supersedes the old one, and an approval authorizes specific
    # actions rather than a case, so a re-plan has to be able to pull the case back
    # out of APPROVED. Without that the transition was silently skipped and a case
    # went on claiming it was approved while newly-planned work awaited a decision.
    case.APPROVED: frozenset({case.EXECUTING, case.APPROVAL_REQUIRED, case.REVIEW_REQUIRED}),
    case.EXECUTING: frozenset({case.EXECUTION_VERIFIED}),
    case.EXECUTION_VERIFIED: frozenset({case.RESPONSE_PENDING}),
    # The only edge into COMPLETED.
    case.RESPONSE_PENDING: frozenset({case.COMPLETED}),
    case.COMPLETED: frozenset(),
    # Exception states. ESCALATED and FAILED are recoverable -- a human can put an
    # escalated case back into review, and a failed search can be retried -- which is
    # why they are not in TERMINAL_STATUSES.
    case.FAILED: frozenset({case.REVIEW_REQUIRED, case.SEARCHING, case.ESCALATED}),
    case.ESCALATED: frozenset({case.REVIEW_REQUIRED, case.APPROVAL_REQUIRED, case.RESPONSE_PENDING}),
    case.REJECTED: frozenset(),
    case.PARTIALLY_COMPLETED: frozenset(),
    case.CANCELLED: frozenset(),
    case.EXPIRED: frozenset(),
}

# Reachable from any non-terminal status, so they are not repeated in every row above.
# A connector can die, a reviewer can reject, a requester can withdraw, and an SLA can
# lapse at any point in the flow.
_ALWAYS_REACHABLE = frozenset({
    case.FAILED, case.REJECTED, case.CANCELLED, case.EXPIRED, case.ESCALATED,
    case.PARTIALLY_COMPLETED,
})


def allowed_transitions(from_status: str) -> frozenset[str]:
    """Every status reachable from `from_status`, including the always-reachable
    exception states. Empty for a terminal status."""
    if from_status in case.TERMINAL_STATUSES:
        return frozenset()
    return _TRANSITIONS.get(from_status, frozenset()) | _ALWAYS_REACHABLE


def can_transition(from_status: str, to_status: str) -> bool:
    return to_status in allowed_transitions(from_status)


def assert_transition(from_status: str, to_status: str) -> None:
    """Raise unless the move is legal. Called by every function that changes a case's
    status -- this is the single enforcement point for §12."""
    if to_status not in case.ALL_STATUSES:
        raise InvalidCaseTransitionError(f"{to_status!r} is not a DSR case status")
    if from_status in case.TERMINAL_STATUSES:
        raise InvalidCaseTransitionError(
            f"case is {from_status} (terminal); it cannot move to {to_status}"
        )
    if not can_transition(from_status, to_status):
        raise InvalidCaseTransitionError(
            f"cannot move a DSR case from {from_status!r} to {to_status!r}; "
            f"legal next states are {sorted(allowed_transitions(from_status))}"
        )


def requires_error_code(to_status: str) -> bool:
    """Statuses that must carry a domain error code explaining why the case stopped.

    ESCALATED is excluded on purpose: escalation is a routing decision made by a
    human ("this needs a senior reviewer"), not a system failure, so it carries a
    reason in the audit entry rather than an error code.
    """
    return to_status in (case.FAILED, case.REJECTED, case.EXPIRED, case.PARTIALLY_COMPLETED)


def is_terminal(status: str) -> bool:
    return status in case.TERMINAL_STATUSES
