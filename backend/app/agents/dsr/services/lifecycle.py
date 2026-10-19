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
    # REVIEW_REQUIRED is reachable before a search when the request is not one a search
    # should start for (a grievance, or a classification too uncertain to act on): a
    # person decides what it is first. It is the same state a post-search ambiguity
    # uses, and it leaves through the same doors (reclassify, then SEARCHING).
    case.IDENTITY_VERIFIED: frozenset({case.CLASSIFIED, case.SEARCHING, case.REVIEW_REQUIRED}),
    case.CLASSIFIED: frozenset({
        case.IDENTITY_PENDING, case.IDENTITY_VERIFIED, case.SEARCHING, case.REVIEW_REQUIRED,
    }),
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


# A read-only projection of the stored status onto the vocabulary a portal or customer
# sees. Nothing is stored under these names and no transition depends on them -- the
# stored statuses stay the source of truth, so no migration is needed and the state
# machine above is untouched. `rejected`, `cancelled`, `expired` and `escalated` keep
# their own names because the projection has nothing truer to call them.
_PUBLIC_STATUS = {
    case.RECEIVED: "PENDING",
    case.CLASSIFIED: "PENDING",
    case.IDENTITY_PENDING: "VERIFICATION_REQUIRED",
    case.IDENTITY_VERIFIED: "SEARCHING",
    case.SEARCHING: "SEARCHING",
    case.SEARCH_COMPLETED: "SEARCHING",
    case.REVIEW_REQUIRED: "AWAITING_APPROVAL",
    case.APPROVAL_REQUIRED: "AWAITING_APPROVAL",
    # APPROVED is "approved, execution not yet started" -- still the execution phase
    # from the requester's side, and not done until the response has gone out.
    case.APPROVED: "EXECUTING",
    case.EXECUTING: "EXECUTING",
    case.EXECUTION_VERIFIED: "EXECUTING",
    case.RESPONSE_PENDING: "EXECUTING",
    case.COMPLETED: "COMPLETED",
    case.PARTIALLY_COMPLETED: "PARTIALLY_COMPLETED",
    case.FAILED: "FAILED",
    case.REJECTED: "REJECTED",
    case.ESCALATED: "ESCALATED",
    case.CANCELLED: "CANCELLED",
    case.EXPIRED: "EXPIRED",
}

# Failures a retry can fix without the requester doing anything: a source was down or
# a write did not verify. Everything else (identity, authorization, policy) needs a
# person to change something first.
_RETRYABLE_ERROR_CODES = frozenset({
    case.ERR_CONNECTOR_UNAVAILABLE, case.ERR_CONNECTOR_TIMEOUT, case.ERR_SEARCH_FAILED,
    case.ERR_ACTION_FAILED, case.ERR_VERIFICATION_FAILED,
})


# FAILED because nothing was set up to search/act on is not a broken case -- it is a case
# waiting for an administrator, and calling it "failed" tells everyone to look in the wrong
# place. Same stored status; only its customer-facing name differs.
_SETUP_ERROR_CODES = frozenset({case.ERR_SOURCE_NOT_AUTHORIZED})


def public_status(status: str, error_code: str | None = None) -> str:
    """The customer/portal-facing name for a stored case status."""
    if status == case.FAILED and error_code in _SETUP_ERROR_CODES:
        return "WAITING_FOR_SETUP"
    return _PUBLIC_STATUS.get(status, status.upper())


# What to tell a person, in words, instead of a code: (what happened, what to do next).
_PLAIN_BY_ERROR = {
    case.ERR_SOURCE_NOT_AUTHORIZED: (
        "The search could not run because no data source is connected for this organisation.",
        "An administrator needs to connect a data source under DSR → Configuration, then start the search again.",
    ),
    case.ERR_CONNECTOR_UNAVAILABLE: (
        "A connected data source could not be reached.",
        "Check the source's connection under DSR \u2192 Configuration, then start the search again.",
    ),
    case.ERR_CONNECTOR_TIMEOUT: (
        "A connected data source took too long to answer.",
        "Try again; if it keeps happening, check the source's health under DSR \u2192 Configuration.",
    ),
    case.ERR_SEARCH_FAILED: (
        "The search did not complete in every connected source.",
        "Review which source failed in the results, then start the search again.",
    ),
    case.ERR_NO_MATCH: (
        "No record matching the requester's details was found.",
        "Generate the response, which will say that nothing was found, and complete the case.",
    ),
    case.ERR_MULTIPLE_MATCHES: (
        "More than one person matched these details.",
        "A reviewer must confirm which records belong to the requester before anything proceeds.",
    ),
    case.ERR_IDENTITY_REQUIRED: (
        "The requester's identity has not been verified yet.",
        "Complete identity verification first.",
    ),
    case.ERR_IDENTITY_FAILED: (
        "Identity verification did not succeed.",
        "Ask the requester to try again, or verify them manually.",
    ),
    case.ERR_ACTION_FAILED: (
        "An approved action could not be completed.",
        "Press Retry failed actions to reopen it, then Execute.",
    ),
    case.ERR_ACTION_PARTIAL: (
        "Some actions were completed; others were blocked, rejected or failed.",
        "Review what changed and what did not, then send the response.",
    ),
    case.ERR_VERIFICATION_FAILED: (
        "A change was made but could not be confirmed by reading it back.",
        "Check the record in the source before retrying.",
    ),
}

_PLAIN_BY_STATUS = {
    case.RECEIVED: ("The request has been received.", "Classify it and verify the requester."),
    case.CLASSIFIED: ("The request has been classified.", "Verify the requester's identity."),
    case.IDENTITY_PENDING: (
        "Waiting for the requester to prove who they are.", "Complete identity verification.",
    ),
    case.IDENTITY_VERIFIED: ("Identity verified.", "Start the search."),
    case.SEARCHING: ("Searching the connected systems.", "Wait for the search to finish."),
    case.SEARCH_COMPLETED: ("The search finished.", "Review what was found."),
    case.REVIEW_REQUIRED: (
        "A person needs to review this case before it goes further.",
        "Open the case, confirm what the request is, and decide what to do.",
    ),
    case.APPROVAL_REQUIRED: (
        "Proposed actions are waiting for approval.", "Approve or reject each action.",
    ),
    case.APPROVED: ("Approved.", "Press Execute to carry out the approved actions."),
    case.EXECUTING: ("The approved actions are being carried out.", "Wait for the result."),
    case.EXECUTION_VERIFIED: ("The actions were carried out and verified.", "Generate the response."),
    case.RESPONSE_PENDING: ("The response to the requester is ready to prepare.", "Generate and send it."),
    case.COMPLETED: ("The request is complete and the requester has been answered.", None),
    case.PARTIALLY_COMPLETED: (
        "The request was closed, but not everything asked for was done.",
        "The response explains what was and was not changed.",
    ),
    case.REJECTED: ("The request was rejected.", None),
    case.CANCELLED: ("The request was cancelled.", None),
    case.EXPIRED: ("The deadline passed before the request was resolved.", None),
    case.ESCALATED: ("This case was escalated to a senior reviewer.", "Wait for the reviewer."),
}


def plain_status(
    status: str, error_code: str | None = None, request_type: str | None = None,
) -> tuple[str, str | None]:
    """(what happened, what to do next) in plain words, for people who should not have to
    read a status code. A grievance waiting for review says why no search was run."""
    if status == case.REVIEW_REQUIRED and request_type in case.HUMAN_TRIAGE_TYPES:
        return (
            "This is a complaint, not a request to look up or change data, so no search was run.",
            "A team member should read it and respond to the requester directly.",
        )
    if (
        status == case.REVIEW_REQUIRED
        and error_code is None
        and request_type in (case.OTHER, case.UNCLASSIFIED)
    ):
        return (
            "The system could not tell what this request is asking for, so no search was run.",
            "Read the request, set its type, then start the search.",
        )
    if error_code in _PLAIN_BY_ERROR:
        return _PLAIN_BY_ERROR[error_code]
    return _PLAIN_BY_STATUS.get(status, (f"The case is {status.replace('_', ' ')}.", None))


def needs_human_triage(request_type: str, confidence: float | None, method: str | None) -> str | None:
    """Why a search must not start for this case yet, or None when it may.

    A grievance is never searched. Anything classified below MIN_CONFIDENCE_TO_SEARCH waits
    for a person -- unless a person already set the type (method "manual"), which is the
    decision this check exists to obtain.
    """
    if request_type in case.HUMAN_TRIAGE_TYPES:
        return "this is a grievance, which is handled by a person rather than by a data search"
    if method != "manual" and (confidence is None or confidence < case.MIN_CONFIDENCE_TO_SEARCH):
        return (
            "the request could not be classified with enough confidence to search records "
            "on its behalf"
        )
    return None


def failure_is_retryable(status: str, error_code: str | None) -> bool:
    """Whether a FAILED case is the kind a retry can fix. False for any other status."""
    return status == case.FAILED and error_code in _RETRYABLE_ERROR_CODES
