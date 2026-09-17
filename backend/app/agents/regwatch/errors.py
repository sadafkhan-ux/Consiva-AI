"""Agent 5 domain errors.

Every one subclasses the platform's `ConsivaError`, so `main.py`'s existing handler
turns it into `{"detail": ...}` with the right status without Agent 5 registering a
handler of its own -- the arrangement Agents 3 and 4 both use.

Each carries a `code` from schemas/watch.py's ERROR_CODES, written to
`regwatch_findings.error_code` or `regwatch_collections.error_code`, so the reason a
watch stalled is queryable rather than only readable in a message string.
"""

from __future__ import annotations

from app.agents.regwatch.schemas import watch
from app.core.exceptions import ConsivaError


class RegWatchError(ConsivaError):
    """Base for every Agent 5 domain failure."""

    status_code = 500
    code: str | None = None

    def __init__(self, message: str, *, code: str | None = None):
        self.code = code or self.code
        super().__init__(message)


class InvalidSourceError(RegWatchError):
    """The source as described cannot be accepted -- no URL, an unknown connector, a
    check interval that would hammer a regulator's website."""

    status_code = 400
    code = watch.ERR_INVALID_SOURCE


class SourceNotFoundError(RegWatchError):
    """The source does not exist, or belongs to another organisation. Deliberately the
    same error for both: telling a caller "this exists but is not yours" confirms the
    id, which is the IDOR disclosure org scoping exists to prevent."""

    status_code = 404


class FindingNotFoundError(RegWatchError):
    status_code = 404


class SourceNotAuthorizedError(RegWatchError):
    """Only approved sources are ever monitored (spec §15). A request to collect from
    something not in the registry, or from a disabled source, stops here."""

    status_code = 403
    code = watch.ERR_SOURCE_NOT_AUTHORIZED


class SourceUnreachableError(RegWatchError):
    """Collection failed. NOT silently swallowed and NOT reported as "no change" --
    the whole agent turns on that distinction."""

    status_code = 502
    code = watch.ERR_SOURCE_UNREACHABLE


class ContentUnusableError(RegWatchError):
    """Something came back, but not something that can be compared -- an empty body, a
    login wall, a block page. Distinct from unreachable because the fix is different."""

    status_code = 502
    code = watch.ERR_CONTENT_UNUSABLE


class NoBaselineError(RegWatchError):
    """A comparison was asked for against a baseline that does not exist yet. The
    first successful collection produces a `first_capture` change instead."""

    status_code = 409
    code = watch.ERR_NO_BASELINE


class AssessmentFailedError(RegWatchError):
    status_code = 502
    code = watch.ERR_ASSESSMENT_FAILED


class ApprovalRequiredError(RegWatchError):
    """Something that needs a recorded human decision was attempted without one --
    advancing a baseline, or acting on a finding nobody has approved."""

    status_code = 403
    code = watch.ERR_APPROVAL_REQUIRED


class InvalidWatchTransitionError(RegWatchError):
    """The finding was asked to move to a status it cannot legally reach. 409, not
    500: the caller acted on an out-of-date view, which is a conflict rather than a
    server fault."""

    status_code = 409


class WatchNotReadyError(RegWatchError):
    """A precondition for this step is missing -- no collection, no change, no
    assessment. The status may be legal to leave, but something the step depends on is
    not there."""

    status_code = 409
