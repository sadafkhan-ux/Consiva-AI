"""Agent 3 domain errors (prompt §38).

Every one subclasses the platform's own `ConsivaError`, so `main.py`'s existing
exception handler turns it into `{"detail": ...}` with the right HTTP status without
Agent 3 registering a handler of its own.

Each carries a `code` from schemas/case.py's ERROR_CODES. That code is what gets
written to `dsr_requests.error_code` / `dsr_search_runs.error_code`, so the reason a
case stopped is queryable rather than only readable in a message string. A failure
without a code is the thing this module exists to prevent -- "never silently swallow
exceptions, never convert a failed operation into success" (§38).
"""

from __future__ import annotations

from app.agents.dsr.schemas import case
from app.core.exceptions import ConsivaError


class DsrError(ConsivaError):
    """Base for every Agent 3 domain failure."""

    status_code = 500
    code: str | None = None

    def __init__(self, message: str, *, code: str | None = None):
        # An explicit code beats the class default, so one exception class can serve
        # several codes where the distinction is runtime (e.g. timeout vs unreachable).
        self.code = code or self.code
        super().__init__(message)


# ── Identity (§13) ───────────────────────────────────────────────────────────────

class IdentityRequiredError(DsrError):
    """A step that reads the requester's records was attempted before identity
    verification succeeded. This is the security boundary, so it is 403, not 409."""

    status_code = 403
    code = case.ERR_IDENTITY_REQUIRED


class IdentityFailedError(DsrError):
    status_code = 403
    code = case.ERR_IDENTITY_FAILED


# ── Sources and connectors (§15, §16, §38) ───────────────────────────────────────

class SourceNotAuthorizedError(DsrError):
    """The source is not authorized for DSR at all, or not for the operation asked
    of it -- searching a table outside the allowlist, or executing against a source
    whose `allow_execution` is false."""

    status_code = 403
    code = case.ERR_SOURCE_NOT_AUTHORIZED


class ConnectorUnavailableError(DsrError):
    status_code = 502
    code = case.ERR_CONNECTOR_UNAVAILABLE


class ConnectorTimeoutError(DsrError):
    status_code = 504
    code = case.ERR_CONNECTOR_TIMEOUT


# ── Search (§17) ─────────────────────────────────────────────────────────────────

class SearchFailedError(DsrError):
    status_code = 502
    code = case.ERR_SEARCH_FAILED


class MultipleMatchesError(DsrError):
    """More than one distinct subject matched. Deliberately an error rather than a
    "pick the best one": handing one person another person's record is the single
    worst outcome a DSR system can produce, so this always stops for a human (§42
    scenario 5)."""

    status_code = 409
    code = case.ERR_MULTIPLE_MATCHES


# ── Policy and approval (§20, §22, §23) ──────────────────────────────────────────

class PolicyReviewRequiredError(DsrError):
    status_code = 409
    code = case.ERR_POLICY_REVIEW_REQUIRED


class ApprovalRequiredError(DsrError):
    """Execution was attempted without a current, unexpired approval. The one guard
    that makes "approval is not execution" (§23) real."""

    status_code = 403
    code = case.ERR_APPROVAL_REQUIRED


class ActionBlockedError(DsrError):
    status_code = 409
    code = case.ERR_ACTION_BLOCKED


# ── Execution and verification (§24) ─────────────────────────────────────────────

class ActionFailedError(DsrError):
    status_code = 502
    code = case.ERR_ACTION_FAILED


class ActionPartialError(DsrError):
    status_code = 502
    code = case.ERR_ACTION_PARTIAL


class VerificationFailedError(DsrError):
    """The write reported success but the read-back did not confirm it. This is the
    difference between "the command returned OK" and "the data actually changed",
    and it is never reported as success."""

    status_code = 502
    code = case.ERR_VERIFICATION_FAILED


# ── Lifecycle (§12) ──────────────────────────────────────────────────────────────

class InvalidCaseTransitionError(DsrError):
    """A DSR case was asked to move to a status it cannot legally reach from where it
    is. 409, not 500 -- it means the caller (or a stale UI) acted on an out-of-date
    view of the case, which is a conflict, not a server fault."""

    status_code = 409


class CaseNotReadyError(DsrError):
    """A precondition for this step is not met -- identity not verified, no approved
    plan, no evidence. Distinct from InvalidCaseTransitionError: the status may be
    legal to leave, but something the step depends on is missing."""

    status_code = 409


class DsrNotFoundError(DsrError):
    """The case does not exist, or belongs to another org. Deliberately the same
    error for both: telling a caller "this case exists but is not yours" confirms the
    id, which is the IDOR disclosure the org scoping exists to prevent (§37)."""

    status_code = 404
