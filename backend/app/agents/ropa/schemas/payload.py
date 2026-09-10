"""Versioned wire contract for an external source pushing ROPA evidence.

This is the ONE schema an external team (PrepMyEvent, or any future source)
codes against. It deliberately WRAPS `DiscoveryEvidence` rather than redefining
it, so there is a single definition of what evidence looks like and the wrapper
carries only what a wire protocol needs that the evidence itself doesn't:

  * `schema_version`  -- so the contract can evolve without breaking senders
  * `correlation_id`  -- one id threaded through adapter logs, this API, the
                         audit trail and the resulting run, so a support
                         question ("what happened to our 09:00 push?") is
                         answerable across three systems
  * `generated_at`    -- when the SENDER collected it, which is not when we
                         received it
  * `adapter_version` -- which build of the adapter produced this

Compatibility rule: a sender declaring a MAJOR version this backend doesn't
support is rejected outright rather than parsed on a guess. Minor differences
are accepted -- new optional fields must never break an older sender.
"""

from datetime import UTC, datetime

from pydantic import BaseModel, Field, field_validator

from app.agents.ropa.schemas.evidence import DiscoveryEvidence

# Bump MINOR for additive/optional changes, MAJOR only for a breaking one.
CURRENT_SCHEMA_VERSION = "1.0"
SUPPORTED_MAJOR_VERSIONS = frozenset({"1"})


class SourcePayload(BaseModel):
    """What an external adapter POSTs to /api/v1/ropa/evidence."""

    schema_version: str = Field(default=CURRENT_SCHEMA_VERSION)
    source_name: str = Field(min_length=1, max_length=200)
    correlation_id: str = Field(min_length=1, max_length=200)
    generated_at: datetime
    adapter_version: str | None = Field(default=None, max_length=50)
    evidence: DiscoveryEvidence
    # Makes a retried push return the SAME run instead of duplicating work.
    idempotency_key: str | None = Field(default=None, max_length=200)

    @field_validator("schema_version")
    @classmethod
    def _supported_version(cls, value: str) -> str:
        major = value.split(".", 1)[0]
        if major not in SUPPORTED_MAJOR_VERSIONS:
            raise ValueError(
                f"unsupported schema_version {value!r}; this backend supports major "
                f"version(s) {sorted(SUPPORTED_MAJOR_VERSIONS)}"
            )
        return value

    @field_validator("generated_at")
    @classmethod
    def _not_absurdly_future(cls, value: datetime) -> datetime:
        """A timestamp far in the future usually means a broken clock on the
        sender, which would corrupt change-detection ordering later."""
        now = datetime.now(UTC)
        reference = value if value.tzinfo else value.replace(tzinfo=UTC)
        if (reference - now).total_seconds() > 86_400:
            raise ValueError("generated_at is more than 24h in the future; check the sender's clock")
        return value


class PayloadAccepted(BaseModel):
    """What the sender gets back -- enough to correlate and to poll."""

    run_id: str
    correlation_id: str
    schema_version: str
    status: str
    tables_received: int
    columns_received: int
    personal_data_elements: int
