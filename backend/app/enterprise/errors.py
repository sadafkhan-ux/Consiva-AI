"""Errors of the Enterprise API. They subclass the application's ConsivaError, so the existing exception handler turns them into the same JSON error responses."""

from app.core.exceptions import ConsivaError


class EnterpriseDisabled(ConsivaError):
    """ENTERPRISE_ENABLED is false: the product is not available on this deployment. 404, as if the route did not exist."""

    status_code = 404


class EnterpriseNotPermitted(ConsivaError):
    """Authenticated but not allowed: a full-site scan is limited to organisation administrators and to service keys granted `consent:scan:application`."""

    status_code = 403


class DomainNotVerified(ConsivaError):
    """A full-site scan needs a domain that has passed DNS ownership verification."""

    status_code = 403


class AttestationRequired(ConsivaError):
    """`authorized` must be true, and the URL must be absolute http(s)."""

    status_code = 400


class InvalidScanState(ConsivaError):
    """The scan is not in a state this operation accepts."""

    status_code = 409
