"""The regulatory finding state machine (spec §4, §9, §10).

One table of legal moves, one function that enforces it. Everything else asks this
module rather than carrying its own idea of what may follow what, which is what keeps
the API, the worker and the UI from disagreeing.

THE INVARIANTS, AND WHY EACH ONE EXISTS
---------------------------------------
  * ACTION_OPEN is reachable only from APPROVED. Follow-up work is created because
    somebody decided the change matters; work that appears without that decision is
    work nobody asked for.

  * CLOSED is reachable only from ACTION_OPEN or APPROVED. A finding is not finished
    because a reviewer looked at it -- it is finished when what it required has been
    done, or when it was approved and required nothing.

  * DISMISSED is terminal, and so is CLOSED. "This does not apply to us" is a
    position taken with a reason and a name. Re-opening it would quietly erase that.
    A later change to the same source raises a NEW finding, which is how the history
    of what was decided survives.

  * SUPERSEDED exists so a source that changes again before anyone reviewed the last
    change does not accumulate two live findings competing for the same decision.
    Only the assessment states can be superseded -- once a human has approved or
    dismissed something, their decision stands and is not overwritten by a re-check.

  * FAILED is NOT terminal. A collection or assessment that broke can be retried, and
    a finding that failed once should not be a dead row in a compliance record.
"""

from __future__ import annotations

from app.agents.regwatch.errors import InvalidWatchTransitionError, WatchNotReadyError
from app.agents.regwatch.schemas import watch

_ALLOWED: dict[str, frozenset[str]] = {
    # Freshly detected from a change. Either it gets assessed, or a reviewer picks it
    # up directly -- a person may always short-circuit the machine's work.
    watch.DETECTED: frozenset({
        watch.ASSESSING, watch.REVIEW_REQUIRED, watch.DISMISSED,
        watch.SUPERSEDED, watch.FAILED,
    }),

    # The agent is working: relevance, impact, interpretation. Can end up needing a
    # human (the normal case), dismissed by a rule that positively established
    # irrelevance, or failed.
    watch.ASSESSING: frozenset({
        watch.REVIEW_REQUIRED, watch.DISMISSED, watch.SUPERSEDED, watch.FAILED,
    }),

    # Waiting on a person. The only place APPROVED and DISMISSED come from.
    watch.REVIEW_REQUIRED: frozenset({
        watch.APPROVED, watch.DISMISSED, watch.ASSESSING, watch.SUPERSEDED,
        watch.FAILED,
    }),

    # A human said this matters. Either it needs work, or it needed none.
    watch.APPROVED: frozenset({
        watch.ACTION_OPEN, watch.CLOSED, watch.REVIEW_REQUIRED,
    }),

    # Work outstanding. Back to review if the work changes the picture.
    watch.ACTION_OPEN: frozenset({
        watch.CLOSED, watch.REVIEW_REQUIRED,
    }),

    # A retry re-enters the pipeline; it does not jump to a conclusion.
    watch.FAILED: frozenset({
        watch.DETECTED, watch.ASSESSING, watch.REVIEW_REQUIRED, watch.DISMISSED,
    }),

    watch.CLOSED: frozenset(),
    watch.DISMISSED: frozenset(),
    watch.SUPERSEDED: frozenset(),
}

# Moving here without saying why is not allowed -- see `requires_error_code`.
_REQUIRE_ERROR_CODE = frozenset({watch.FAILED})

# Statuses a re-check may supersede. Deliberately excludes everything a human has
# touched: an approval or a dismissal is not overwritten by the next poll.
SUPERSEDABLE = frozenset({watch.DETECTED, watch.ASSESSING, watch.REVIEW_REQUIRED})


def allowed_transitions(from_status: str) -> frozenset[str]:
    return _ALLOWED.get(from_status, frozenset())


def can_transition(from_status: str, to_status: str) -> bool:
    return to_status in allowed_transitions(from_status)


def assert_transition(from_status: str, to_status: str) -> None:
    """Raise unless this move is legal. The single gate every caller goes through."""
    if to_status not in watch.ALL_STATUSES:
        raise InvalidWatchTransitionError(f"{to_status!r} is not a regulatory finding status")
    if from_status == to_status:
        raise InvalidWatchTransitionError(
            f"a finding is already {to_status}; moving it there again would write a "
            "second audit entry for something that did not happen"
        )
    if not can_transition(from_status, to_status):
        raise InvalidWatchTransitionError(
            f"cannot move a regulatory finding from {from_status!r} to {to_status!r}; "
            f"legal next states are {sorted(allowed_transitions(from_status))}"
        )


def requires_error_code(to_status: str) -> bool:
    """FAILED without a code is a dead end nobody can act on."""
    return to_status in _REQUIRE_ERROR_CODE


def is_terminal(status: str) -> bool:
    return status in watch.TERMINAL_STATUSES


def can_be_superseded(status: str) -> bool:
    """Whether a fresh change may retire this finding.

    False once a person has decided. A re-check that quietly superseded an approval
    would lose the decision AND the reason for it, and the next reviewer would see a
    finding with no history of having been handled.
    """
    return status in SUPERSEDABLE


def assert_reviewable(status: str) -> None:
    """Guard for the decision endpoints."""
    if status != watch.REVIEW_REQUIRED:
        raise WatchNotReadyError(
            f"a finding is decided from {watch.REVIEW_REQUIRED}, not from {status!r}"
        )
