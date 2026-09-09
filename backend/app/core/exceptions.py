class ConsivaError(Exception):
    """Base class for domain errors that map to a specific HTTP response."""

    status_code = 500

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


class NotFoundError(ConsivaError):
    status_code = 404


class InvalidUrlError(ConsivaError):
    """Raised when a scan request's URL is malformed (no parseable scheme/netloc) --
    distinct from ScanAuthorizationError, which is about permission, not shape."""

    status_code = 400


class ScanAuthorizationError(ConsivaError):
    """Raised when a scan is requested for a domain that hasn't been attested as
    owned/authorized by the requesting org. See docs/architecture §P — no domain
    verification flow (DNS/meta-tag) exists yet; this enforces the minimal
    self-attestation stopgap instead of scanning arbitrary third-party sites.
    """

    status_code = 403


class ScanTimeoutError(ConsivaError):
    """Raised when a scan exceeds its overall wall-clock budget and is aborted.

    Exists because a hung scan is not just a slow scan: jobs/worker.py processes one
    job at a time and runs reap_stale_jobs() at the TOP of its loop, so a scan that
    never returns blocks BOTH every subsequent job and the reaper that would otherwise
    recover it. Observed live -- one job held the only worker for 15+ minutes while
    five later scans sat "queued" and never started. Failing loudly here lets the job
    be marked failed, retried by the queue's normal policy, and the worker move on.
    """

    status_code = 504


class RateLimitExceededError(ConsivaError):
    """Raised when an org exceeds the scan rate limit (master prompt §5: "rate-limit
    and queue scans")."""

    status_code = 429


class ScanNotReadyError(ConsivaError):
    """Raised when /analyze is called on a scan that hasn't finished the scan phase
    yet (status is anything other than "completed") -- the scan exists (that's a 404
    question) but isn't in the required state, so this is a conflict, not a server
    error. Previously a bare ValueError here fell through to FastAPI's generic 500
    handler -- correct status/message, but the wrong HTTP semantics and error shape."""

    status_code = 409


class AgentRunAlreadyInProgressError(ConsivaError):
    """Raised when /analyze is called on a scan that already has a non-terminal
    AgentRun (pending/running/paused) -- calling analyze twice (e.g. a UI double-click
    or a client retry after a slow response) previously created a SECOND independent
    agent_run/analyze job with no relationship to the first, silently doubling the
    real NVIDIA LLM cost and producing two overlapping sets of findings for one scan."""

    status_code = 409


class FindingAlreadyDecidedError(ConsivaError):
    """Raised when approve/reject/edit targets a finding that is no longer "pending"
    -- either a stale UI showing an already-decided finding, or two reviewers racing
    on the same finding. status_code=409: the request conflicts with the finding's
    current state, not a not-found or a permission issue."""

    status_code = 409


class LLMOutputValidationError(ConsivaError):
    """Raised when the LLM's structured output fails schema validation or cites a
    dpdp_reference not present in the retrieved RAG context, after all retries."""

    status_code = 502


class ReasonRequiredError(ConsivaError):
    """Raised when a review decision that must carry a recorded rationale (approving a
    HIGH-risk finding; reject/edit are enforced at the request-schema level) arrives
    without one."""

    status_code = 422


class NotificationDeliveryError(ConsivaError):
    """Raised when a notification Action can't actually be delivered (no endpoint
    configured, or the real HTTP POST to it failed) -- the action stays "open" rather
    than being marked "done" for something that was never really sent."""

    status_code = 502


class InvalidActionTransitionError(ConsivaError):
    """Raised when an Action Module status transition isn't valid for that action's
    type/current status (e.g. skipping config_change's staged step, or acting on an
    unapproved finding)."""

    status_code = 409


class InvalidEditError(ConsivaError):
    """Raised when a reviewer's edited_payload contains a value outside the same
    enum/type constraints the LLM's own output is held to (e.g. risk_level="banana").
    The edit path is the one place a HUMAN writes these fields -- without this check,
    an out-of-enum string would be persisted where every other writer is schema-bound."""

    status_code = 422
