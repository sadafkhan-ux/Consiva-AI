"""Outbound scan-completion callbacks.

Two things make this different from an ordinary HTTP POST, and both are load-bearing:

1. The target URL comes from an API caller, which makes a webhook exactly as dangerous
   as the scan URL itself -- a request this server makes to an address the caller
   chose. It goes through the SAME assert_safe_url used on scan targets, and it is
   re-checked at DELIVERY time rather than only at registration, because DNS can be
   repointed to 169.254.169.254 in between.

2. The receiver has no way to know a POST really came from us unless we prove it, so
   every delivery is signed. Without that, anyone who learns a customer's webhook URL
   can forge scan results into their system.

Retries are the existing job queue's (agent_jobs.attempts + linear backoff, 3 attempts)
rather than a loop here -- a retry that lives inside one request dies with the process.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx

from app.config import get_settings
from app.scanner.url_safety import assert_safe_url
from app.db.models import WebhookDelivery
from app.db.session import apply_org_scope, async_session_factory

logger = logging.getLogger(__name__)

# A webhook receiver that has not answered in this long is not going to. Short on
# purpose: this runs on a worker slot that a real scan could otherwise be using.
_TIMEOUT_SECONDS = 10.0

_SIGNATURE_HEADER = "X-Consiva-Signature"
_TIMESTAMP_HEADER = "X-Consiva-Timestamp"
_EVENT_HEADER = "X-Consiva-Event"


def sign(payload: bytes, timestamp: str, secret: str) -> str:
    """HMAC-SHA256 over `timestamp.payload`, hex, prefixed with the scheme version.

    The timestamp is inside the signed material, not just alongside it: signing the
    body alone lets an attacker who captures one delivery replay it forever, since
    every byte they need is in the copy they already hold. With the timestamp signed,
    a receiver can reject anything older than its own tolerance and the replay stops
    working.
    """
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + payload, hashlib.sha256)
    return f"v1={mac.hexdigest()}"


def is_configured() -> bool:
    """Whether outbound webhooks can be signed, and therefore sent at all.

    Exposed so the API can reject `webhook_url` at request time with a clear error,
    instead of accepting a scan whose callback will silently never arrive.
    """
    return bool(get_settings().webhook_signing_secret)


async def validate_target(url: str) -> None:
    """Refuses a webhook URL that would make this server attack its own network.

    Called at REGISTRATION so a caller gets a 4xx immediately instead of a silent
    non-delivery later -- and again at delivery, because passing once says nothing
    about where the name resolves minutes later.
    """
    if not url.startswith(("http://", "https://")):
        raise ValueError("webhook_url must be an absolute http:// or https:// URL")
    await assert_safe_url(url)


def build_payload(*, scan_id: uuid.UUID, status: str, website_url: str, event: str) -> dict[str, Any]:
    """What a receiver is told.

    Deliberately minimal: identifiers and a status, never findings or evidence. A
    webhook is a notification, and the result itself is fetched back over the
    authenticated API -- so a mis-typed webhook URL leaks the fact that a scan
    happened, not what it found.
    """
    return {
        "event": event,
        "scan_id": str(scan_id),
        "status": status,
        "website_url": website_url,
        "result_url": f"/api/v1/consent-agent/scans/{scan_id}",
        "completed_at": datetime.now(UTC).isoformat(),
    }


async def deliver(
    *, org_id: uuid.UUID, scan_id: uuid.UUID, target_url: str, payload: dict[str, Any], attempt: int = 1
) -> bool:
    """One delivery attempt. Returns True on a 2xx.

    Raises on a failure the queue should retry, so the existing backoff applies. A
    refused TARGET (SSRF check) is recorded and NOT raised: retrying a URL that points
    at link-local space will never start succeeding, and burning three worker slots to
    re-learn that helps nobody.
    """
    settings = get_settings()
    secret = settings.webhook_signing_secret
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    timestamp = str(int(datetime.now(UTC).timestamp()))

    record = WebhookDelivery(
        org_id=org_id, scan_id=scan_id, event=payload["event"], target_url=target_url,
        attempt=attempt, status="pending",
    )

    # Refuse rather than send unsigned.
    #
    # This used to log a warning and deliver anyway. That is the wrong default: an
    # unsigned webhook is one the receiver cannot authenticate, so anyone who learns a
    # customer's webhook URL can forge scan results into their system -- and the only
    # thing standing between that and production was a log line nobody reads. A missing
    # secret is a deployment mistake, not a degraded mode.
    #
    # Recorded as a failed delivery and NOT raised: retrying changes nothing until an
    # operator sets the secret, and burning the queue's three attempts to re-learn that
    # helps nobody. is_configured() below lets the API reject webhook_url up front, so
    # in practice a caller finds out at request time, not silently afterwards.
    if not secret:
        record.status = "failed"
        record.error = (
            "WEBHOOK_SIGNING_SECRET is not configured; refusing to deliver an unsigned "
            "webhook that the receiver could not authenticate."
        )
        await _save(record)
        logger.error(
            "WEBHOOK_SIGNING_SECRET is not set; REFUSED to deliver scan %s. Set it and "
            "re-run the scan.", scan_id,
        )
        return False

    try:
        await validate_target(target_url)
    except Exception as exc:
        record.status = "failed"
        record.error = f"Refused unsafe webhook target: {exc}"[:500]
        await _save(record)
        logger.warning("Webhook for scan %s refused: %s", scan_id, exc)
        return False

    headers = {
        "Content-Type": "application/json",
        _EVENT_HEADER: payload["event"],
        _TIMESTAMP_HEADER: timestamp,
        "User-Agent": "Consiva-ConsentAgent/1.0 (+webhook)",
    }
    headers[_SIGNATURE_HEADER] = sign(body, timestamp, secret)

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS, follow_redirects=False) as client:
            # follow_redirects=False on purpose: a 302 to an internal address is the
            # oldest way around an SSRF check that only ran on the original URL.
            response = await client.post(target_url, content=body, headers=headers)
        record.response_status = response.status_code
        if 200 <= response.status_code < 300:
            record.status = "delivered"
            record.delivered_at = datetime.now(UTC)
            await _save(record)
            return True
        record.status = "failed"
        record.error = f"HTTP {response.status_code}"
        await _save(record)
        raise RuntimeError(f"Webhook returned HTTP {response.status_code}")
    except httpx.HTTPError as exc:
        record.status = "failed"
        record.error = f"{type(exc).__name__}: {exc}"[:500]
        await _save(record)
        raise


async def _save(record: WebhookDelivery) -> None:
    """Best effort. A delivery that succeeded must not be reported as failed because
    the bookkeeping write had a problem, and a bookkeeping failure must never take down
    the job that was otherwise fine.

    The org scope is bound explicitly. This runs on a background worker, not in a
    request, so nothing has set `app.org_id` -- and webhook_deliveries has forced
    row-level security like every other table, so the INSERT is refused by policy:
    "new row violates row-level security policy". Measured, not predicted -- the first
    live delivery succeeded (HTTP 200, signature verified by the receiver) and its
    record was silently dropped by the except below, which is precisely the failure
    tracking this table exists to provide.
    """
    try:
        async with async_session_factory() as db:
            await apply_org_scope(db, record.org_id)
            db.add(record)
            await db.commit()
    except Exception:
        logger.warning("Could not record webhook delivery for scan %s", record.scan_id, exc_info=True)
