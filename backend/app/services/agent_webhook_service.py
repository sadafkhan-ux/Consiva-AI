"""Outbound event notifications to an external-partner endpoint, fired when a
ROPA run or a Consent Agent API scan reaches a final state (completed or
failed).

NOT the same thing as `webhook_service.py`: that module delivers to a
CALLER-supplied, per-scan `webhook_url` (customer-configured, SSRF-checked,
signed with Consiva's own `X-Consiva-Signature` scheme, retried through the
job queue). This module signs with the receiving partner's own scheme instead:
HMAC_SHA256(secret, "<unix_timestamp>.<raw_body>"), hex, sent as
`X-Agent-Signature: sha256=<hex>` alongside `X-Agent-Timestamp: <unix_timestamp>`.
Both header names and the "sha256=" prefix are the partner's contract, not a
Consiva design choice.

DESTINATION: one operator-configured pair per deployment by default --
`AGENT_WEBHOOK_URL`/`AGENT_WEBHOOK_SECRET` in that environment's .env, the same
way `CORS_ALLOWED_ORIGINS` already differs between UAT and production. That
default is ONLY correct if each environment runs its own agent instance.
Measured on this deployment: it is not -- one backend process answers for both
consiva.ai and uat.consiva.ai (same CORS list, same database), so a single
fixed destination would send a UAT-started run's notification to production's
endpoint, signed with production's secret, which that endpoint would
(correctly) reject. `send_event`'s `callback_url`/`callback_secret` parameters
exist for exactly this: the caller that created the run or scan is the one who
actually knows which environment started it, and may pass its own destination
and secret to override the fixed config for that one delivery. Neither is
persisted anywhere by this module -- callers that need the override to survive
past the original request (a connector-pull discovery run the worker executes
later) persist it themselves (migration 0032) and pass it back in at send time.

Fire-and-forget by design: the run/scan's own state is already durably
persisted by the time this is called, so a partner endpoint being briefly
down must not fail or retry-loop the agent's own job -- it is logged instead,
so an operator can notice and re-deliver out of band if needed. (Unlike
webhook_service.deliver, which intentionally raises so the job queue retries
a CUSTOMER's own webhook; this is a different destination with no
customer-facing SLA of its own yet.)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx

from app.agents.ropa.connectors.base import ConnectorError
from app.agents.ropa.connectors.factory import decrypt_credential, encrypt_credential
from app.config import get_settings
from app.scanner.url_safety import assert_safe_url

logger = logging.getLogger(__name__)

# Short on purpose: this runs inline in a worker job, and a dead partner
# endpoint must not hold that slot open any longer than necessary.
_TIMEOUT_SECONDS = 10.0

_TIMESTAMP_HEADER = "X-Agent-Timestamp"
_SIGNATURE_HEADER = "X-Agent-Signature"


def _sign(timestamp: str, body: bytes, secret: str) -> str:
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256)
    return f"sha256={mac.hexdigest()}"


def _now_iso() -> str:
    """The partner's spec example is plain UTC with a trailing Z and no
    fractional seconds (e.g. "2026-10-07T12:00:00Z") -- matched exactly here
    rather than using isoformat()'s "+00:00"/microseconds, since this field is
    read by their system, not ours."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


async def prepare_callback_override(
    callback_url: str | None, callback_secret: str | None
) -> tuple[str | None, str | None]:
    """Validates a caller-supplied (callback_url, callback_secret) pair at the
    point a run/scan is CREATED, for storage on its row (migration 0032) and
    later use by `send_event`'s override parameters. Returns (callback_url,
    encrypted_secret) to persist, or (None, None) when the caller supplied
    neither.

    Raises ValueError -- callers map this to a 4xx -- for: only one of the
    pair supplied (almost certainly a caller error, not "use the fixed config
    instead"); a non-http(s) or unsafe (private/loopback/metadata) URL, same
    check `webhook_service.validate_target` applies to the per-scan
    `webhook_url`; or a secret with no ROPA_CREDENTIAL_ENCRYPTION_KEY
    configured to encrypt it under (CredentialEncryptionNotConfigured, a
    ConnectorError -- converted to ValueError here so every caller of this
    function has one exception type to catch)."""
    if not callback_url and not callback_secret:
        return None, None
    if not (callback_url and callback_secret):
        raise ValueError("callback_url and callback_secret must be supplied together, or not at all")
    if not callback_url.startswith(("http://", "https://")):
        raise ValueError("callback_url must be an absolute http:// or https:// URL")
    await assert_safe_url(callback_url)
    try:
        ciphertext = encrypt_credential(callback_secret)
    except ConnectorError as exc:
        raise ValueError(str(exc)) from exc
    return callback_url, ciphertext


def resolve_callback_secret(ciphertext: str | None) -> str | None:
    """The other half of prepare_callback_override: decrypts a stored
    callback_secret back to plaintext at delivery time. None in, None out --
    callers pass this straight through to send_event's callback_secret, and a
    row with no override naturally yields no override."""
    return decrypt_credential(ciphertext) if ciphertext else None


def is_configured() -> bool:
    """Whether the FIXED, operator-configured destination is usable -- not
    whether a per-call override exists, since that is only known per run/scan,
    not globally. A deployment relying entirely on per-call overrides (every
    caller always supplies its own callback_url/callback_secret) correctly
    reports False here; `send_event` still works for it."""
    settings = get_settings()
    return bool(settings.agent_webhook_url and settings.agent_webhook_secret)


async def send_event(
    event: str, *, callback_url: str | None = None, callback_secret: str | None = None, **fields: Any
) -> None:
    """Build, sign and POST one event.

    `callback_url`/`callback_secret` override the fixed AGENT_WEBHOOK_URL/
    AGENT_WEBHOOK_SECRET config for this one delivery (see module docstring) --
    both or neither; a caller passing only one almost certainly means to
    override both, so this treats that as "no override" rather than silently
    mixing one environment's URL with another's secret.

    Never raises -- logs and returns on any failure, including nothing usable
    being configured at all (the common case until an operator sets
    AGENT_WEBHOOK_URL/AGENT_WEBHOOK_SECRET, or every caller supplies its own
    override)."""
    settings = get_settings()
    if callback_url and callback_secret:
        url, secret = callback_url, callback_secret
    else:
        url, secret = settings.agent_webhook_url, settings.agent_webhook_secret
    if not url or not secret:
        return

    try:
        await assert_safe_url(url)
    except Exception:
        logger.warning("agent webhook destination refused as unsafe: event=%s", event, exc_info=True)
        return

    payload: dict[str, Any] = {
        "event": event,
        "event_id": str(uuid.uuid4()),
        "occurred_at": _now_iso(),
        **fields,
    }
    body = json.dumps(payload, separators=(",", ":")).encode()
    timestamp = str(int(time.time()))
    signature = _sign(timestamp, body, secret)

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            response = await client.post(
                url,
                content=body,
                headers={
                    "Content-Type": "application/json",
                    _TIMESTAMP_HEADER: timestamp,
                    _SIGNATURE_HEADER: signature,
                },
            )
        if 200 <= response.status_code < 300:
            logger.info("agent webhook delivered: event=%s status=%s", event, response.status_code)
        else:
            logger.warning(
                "agent webhook %s returned HTTP %s for event %s", url, response.status_code, event,
            )
    except httpx.HTTPError:
        logger.warning("agent webhook delivery failed for event %s", event, exc_info=True)
