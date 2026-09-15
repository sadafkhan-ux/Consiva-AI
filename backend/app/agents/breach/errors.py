"""Agent 4 domain errors (prompt §39).

Every one subclasses the platform's `ConsivaError`, so `main.py`'s existing handler
turns it into `{"detail": ...}` with the right status without Agent 4 registering a
handler of its own -- the same arrangement Agent 3 uses.

Each carries a `code` from schemas/incident.py's ERROR_CODES, written to
`incident_cases.error_code` so the reason an incident stalled is queryable rather
than only readable in a message string.
"""

from __future__ import annotations

from app.agents.breach.schemas import incident
from app.core.exceptions import ConsivaError


class IncidentError(ConsivaError):
    """Base for every Agent 4 domain failure."""

    status_code = 500
    code: str | None = None

    def __init__(self, message: str, *, code: str | None = None):
        self.code = code or self.code
        super().__init__(message)


class InvalidIncidentError(IncidentError):
    """The incident as described cannot be accepted -- missing detail, impossible
    timestamps, an unknown source."""

    status_code = 400
    code = incident.ERR_INVALID_INCIDENT


class IncidentNotFoundError(IncidentError):
    """The incident does not exist, or belongs to another organisation. Deliberately
    the same error for both: telling a caller "this exists but is not yours" confirms
    the id, which is the IDOR disclosure org scoping exists to prevent."""

    status_code = 404


class UnauthorizedIncidentAccessError(IncidentError):
    """Incident evidence can contain security-sensitive detail -- account names,
    attack paths, sometimes credentials. Access is a separate question from being
    signed in."""

    status_code = 403
    code = incident.ERR_UNAUTHORIZED_ACCESS


class EvidenceUnavailableError(IncidentError):
    status_code = 409
    code = incident.ERR_EVIDENCE_UNAVAILABLE


class InvestigationFailedError(IncidentError):
    status_code = 502
    code = incident.ERR_INVESTIGATION_FAILED


class ImpactUnknownError(IncidentError):
    """Impact could not be established from available evidence. Not a crash -- a real
    outcome that must be visible rather than rounded to zero."""

    status_code = 409
    code = incident.ERR_IMPACT_UNKNOWN


class RiskAssessmentFailedError(IncidentError):
    status_code = 502
    code = incident.ERR_RISK_ASSESSMENT_FAILED


class ApprovalRequiredError(IncidentError):
    """A containment action or an external communication was attempted without a
    current approval. The guard that makes "recommendation is not action" real."""

    status_code = 403
    code = incident.ERR_APPROVAL_REQUIRED


class ActionBlockedError(IncidentError):
    status_code = 409
    code = incident.ERR_ACTION_BLOCKED


class ActionFailedError(IncidentError):
    status_code = 502
    code = incident.ERR_ACTION_FAILED


class VerificationFailedError(IncidentError):
    """A containment action was performed but its effect could not be confirmed. Never
    reported as success."""

    status_code = 502
    code = incident.ERR_VERIFICATION_FAILED


class NotificationFailedError(IncidentError):
    status_code = 502
    code = incident.ERR_NOTIFICATION_FAILED


class InvalidIncidentTransitionError(IncidentError):
    """The incident was asked to move to a status it cannot legally reach. 409, not
    500: the caller acted on an out-of-date view, which is a conflict rather than a
    server fault."""

    status_code = 409


class IncidentNotReadyError(IncidentError):
    """A precondition for this step is missing -- no evidence, no risk assessment, no
    approved plan. The status may be legal to leave, but something the step depends on
    is not there."""

    status_code = 409
