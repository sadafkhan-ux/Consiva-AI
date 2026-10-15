"""Domain-first DSR onboarding: the logic behind the setup wizard.

The flow a business walks through is: enter a domain -> prove it controls that domain ->
see what Consiva can connect to -> authorise one source, explicitly -> test it. This
module is the first three steps. The last two are PUT /config/sources and
POST /config/sources/{id}/test, which already existed and are deliberately not duplicated.

WHAT A DOMAIN PROVES, AND WHAT IT DOES NOT
------------------------------------------
A verified domain proves the organisation controls that site's DNS. It says nothing about
any database behind the site, and it authorises nothing: no source, no table, no column,
no operation. Nothing in this module can enable a data source, and no code path here turns
"domain verified" into "access granted". Source authorisation stays an explicit,
administrator-only act with its own allowlists.

WHY THE EXISTING VERIFICATION IS REUSED (READ-ONLY), AND WHERE IT IS NOT TRUSTED
-------------------------------------------------------------------------------
The Consent agent already verifies a domain by DNS TXT record (`consiva-verify=<token>`,
the token an HMAC of org + domain) and stamps `websites.verified_at`. That is the same claim
DSR onboarding needs -- "this organisation controls this domain's DNS" -- so it is reused
rather than reinvented, and nothing here modifies that code or that table. Three limits of
that record are handled rather than ignored:

  * `verified_at` never expires, and a domain can change hands. A verification older than
    DOMAIN_VERIFICATION_MAX_AGE_DAYS is treated as STALE and must be re-proven.
  * A `websites` row can exist with `verified_at` NULL (the self-attestation demo flow), so
    only a stamped, fresh `verified_at` counts -- the row's existence proves nothing.
  * Proving a domain here is stateless: a fresh DNS check is read-only and writes nothing, so
    onboarding DSR never adds a site to the Consent agent's list as a side effect.

WHAT DISCOVERY DOES NOT DO
--------------------------
Consiva does not guess endpoints. Discovery reads the one document a customer may publish for
this purpose (`/.well-known/consiva-dsr.json`), treats everything in it as the customer's
unverified claim, and never calls an endpoint it lists. The supported integration paths are
a fixed catalogue; anything not on it is reported as not available, with the path that does
work, rather than implied to be coming.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import dns.asyncresolver
import dns.exception
import httpx
from pydantic import BaseModel, Field, HttpUrl, ValidationError, field_validator

from app.agents.dsr.connectors.factory import SUPPORTED_CONNECTORS
from app.agents.dsr.errors import DsrError
from app.core.exceptions import ConsivaError
from app.scanner.url_safety import assert_safe_host
from app.services import verification_service

logger = logging.getLogger(__name__)

#: A stamped verification older than this must be re-proven. The Consent agent never expires
#: one; DSR does not inherit that, because DSR onboarding leads to access to personal data.
DOMAIN_VERIFICATION_MAX_AGE_DAYS = 365

MANIFEST_PATH = "/.well-known/consiva-dsr.json"
MANIFEST_MAX_BYTES = 64 * 1024
MANIFEST_TIMEOUT_SECONDS = 5.0

_TXT_PREFIX = "consiva-verify="

# Names that can never be a customer's public business domain.
_RESERVED_SUFFIXES = (
    ".local", ".localhost", ".internal", ".lan", ".home", ".corp", ".intranet",
    ".test", ".invalid", ".example", ".arpa",
)
_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_TLD = r"(?:[a-z]{2,63}|xn--[a-z0-9-]{1,59})"
_DOMAIN_RE = re.compile(rf"^(?:{_LABEL}\.)+{_TLD}$")


class InvalidDomainError(DsrError):
    """The text is not a domain this flow can use. 422: the caller can fix it."""

    status_code = 422
    code = "INVALID_DOMAIN"


class DomainNotVerifiedError(DsrError):
    """Discovery and source setup need a domain the organisation has proven it controls."""

    status_code = 403
    code = "DOMAIN_NOT_VERIFIED"


def normalize_domain(raw: str) -> str:
    """A bare lower-case ASCII domain from whatever a person pasted.

    Accepts `example.com`, `www.example.com`, `https://example.com/path?x=1` and
    `HTTPS://Example.COM:443/`. Rejects IP addresses, single-label and internal names,
    anything with credentials in it, and anything that is not a syntactically valid hostname.
    Internationalised names are converted to punycode so the same domain always compares
    equal.
    """
    text = (raw or "").strip()
    if not text:
        raise InvalidDomainError("enter your website's domain, for example example.com")
    if len(text) > 2048 or any(ch.isspace() for ch in text):
        raise InvalidDomainError("that does not look like a domain")

    candidate = text if "://" in text else f"//{text}"
    try:
        parts = urlsplit(candidate)
        host = parts.hostname
        if parts.username or parts.password:
            raise InvalidDomainError("a domain must not contain a username or password")
        if parts.scheme and parts.scheme not in ("http", "https"):
            raise InvalidDomainError("use the website's address, for example https://example.com")
        port = parts.port  # raises ValueError on a malformed port
    except ValueError as exc:
        raise InvalidDomainError("that does not look like a domain") from exc
    if port not in (None, 80, 443):
        raise InvalidDomainError("a domain must not specify a port")
    if not host:
        raise InvalidDomainError("that does not look like a domain")

    host = host.rstrip(".").lower()
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise InvalidDomainError("enter a domain name, not an IP address")
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise InvalidDomainError("that is not a valid domain name") from exc

    if len(host) > 253 or not _DOMAIN_RE.fullmatch(host):
        raise InvalidDomainError("that is not a valid domain name")
    if host.endswith(_RESERVED_SUFFIXES):
        raise InvalidDomainError("an internal or reserved name cannot be verified; use your public domain")
    return host


# ── Ownership ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Ownership:
    proven: bool
    #: "existing_verification" (fresh websites.verified_at), "dns_txt" (checked just now), or None.
    method: str | None
    verified_at: datetime | None = None
    #: True when a verification exists but is older than the allowed age and was not re-proven.
    stale: bool = False
    detail: str = ""


def txt_record_for(org_id: uuid.UUID, domain: str) -> dict[str, str]:
    """What the administrator must publish. The same record the Consent agent asks for, so a
    domain already verified there needs nothing new."""
    token = verification_service.expected_token(org_id, domain)
    return {"type": "TXT", "host": domain, "value": f"{_TXT_PREFIX}{token}"}


async def txt_has_token(domain: str, token: str) -> bool:
    """Read-only DNS lookup. False on any lookup failure -- an unreachable resolver is
    "not proven", never "proven"."""
    resolver = dns.asyncresolver.Resolver()
    resolver.lifetime = 10
    try:
        answers = await resolver.resolve(domain, "TXT")
    except (dns.exception.DNSException, OSError) as exc:
        logger.info("DSR domain TXT lookup failed for %s: %s", domain, type(exc).__name__)
        return False
    wanted = f"{_TXT_PREFIX}{token}"
    for rdata in answers:
        value = "".join(part.decode("utf-8", "replace") for part in rdata.strings)
        if value.strip() == wanted:
            return True
    return False


def _is_fresh(verified_at: datetime | None, now: datetime) -> bool:
    if verified_at is None:
        return False
    if verified_at.tzinfo is None:
        verified_at = verified_at.replace(tzinfo=UTC)
    return now - verified_at <= timedelta(days=DOMAIN_VERIFICATION_MAX_AGE_DAYS)


async def establish_ownership(
    *,
    org_id: uuid.UUID,
    domain: str,
    verified_at: datetime | None,
    check_dns: bool,
    now: datetime | None = None,
    dns_check=txt_has_token,
) -> Ownership:
    """Decide whether this org controls `domain`, and say how it knows.

    `verified_at` is the org's existing `websites.verified_at` for the domain (the caller
    reads it; this function does no I/O of its own besides the optional DNS lookup, so it is
    testable with nothing but values). A fresh stamp is enough. A stale or missing one needs a
    live DNS check, which only runs when `check_dns` is set.
    """
    moment = now or datetime.now(UTC)
    if _is_fresh(verified_at, moment):
        return Ownership(
            proven=True, method="existing_verification", verified_at=verified_at,
            detail="this domain was already verified for your organisation",
        )
    stale = verified_at is not None
    if not check_dns:
        return Ownership(
            proven=False, method=None, verified_at=verified_at, stale=stale,
            detail=(
                "an earlier verification is older than "
                f"{DOMAIN_VERIFICATION_MAX_AGE_DAYS} days and must be re-proven"
                if stale else "this domain has not been verified yet"
            ),
        )
    token = verification_service.expected_token(org_id, domain)
    if await dns_check(domain, token):
        return Ownership(
            proven=True, method="dns_txt", verified_at=moment, stale=False,
            detail="the verification record was found in your DNS just now",
        )
    return Ownership(
        proven=False, method=None, verified_at=verified_at, stale=stale,
        detail=(
            "the verification record was not found; publish it and try again "
            "(DNS changes can take a few minutes)"
        ),
    )


# ── Discovery ────────────────────────────────────────────────────────────────────

class ManifestConnector(BaseModel):
    type: str = Field(min_length=1, max_length=40, pattern=r"^[a-z0-9_\-]+$")
    base_url: HttpUrl
    docs_url: HttpUrl | None = None
    description: str | None = Field(default=None, max_length=300)

    @field_validator("base_url", "docs_url")
    @classmethod
    def _https_only(cls, value):
        if value is not None and value.scheme != "https":
            raise ValueError("only https endpoints may be declared")
        return value


class Manifest(BaseModel):
    version: int = Field(ge=1, le=1)
    connectors: list[ManifestConnector] = Field(default_factory=list, max_length=20)


async def fetch_manifest(domain: str, *, client_factory=None) -> dict[str, Any]:
    """The customer's published integration manifest, or a precise reason there is none.

    https only, no redirects followed (a redirect is how a public name reaches an internal
    address), a hard size and time limit, and the host is resolved through the same SSRF
    guard every other outbound fetch in this codebase uses. The result is the customer's own
    claim: `declared_by_customer` is always True and no listed endpoint is ever contacted.
    """
    try:
        await assert_safe_host(domain)
    except ConsivaError:
        return {"found": False, "reason": "host_not_reachable_safely", "declared_by_customer": True}

    url = f"https://{domain}{MANIFEST_PATH}"
    make_client = client_factory or (
        lambda: httpx.AsyncClient(timeout=MANIFEST_TIMEOUT_SECONDS, follow_redirects=False)
    )
    try:
        async with make_client() as client:
            async with client.stream("GET", url) as response:
                if response.status_code == 404:
                    return {"found": False, "reason": "not_published", "declared_by_customer": True}
                if 300 <= response.status_code < 400:
                    return {"found": False, "reason": "redirect_not_followed", "declared_by_customer": True}
                if response.status_code != 200:
                    return {
                        "found": False, "reason": f"http_{response.status_code}",
                        "declared_by_customer": True,
                    }
                body = b""
                async for chunk in response.aiter_bytes():
                    body += chunk
                    if len(body) > MANIFEST_MAX_BYTES:
                        return {"found": False, "reason": "too_large", "declared_by_customer": True}
    except (httpx.HTTPError, OSError) as exc:
        logger.info("DSR manifest fetch failed for %s: %s", domain, type(exc).__name__)
        return {"found": False, "reason": "unreachable", "declared_by_customer": True}

    try:
        manifest = Manifest.model_validate_json(body)
    except ValidationError:
        return {"found": False, "reason": "invalid_manifest", "declared_by_customer": True}

    return {
        "found": True, "reason": None, "declared_by_customer": True,
        "url": url,
        "connectors": [
            {
                "type": c.type, "base_url": str(c.base_url),
                "docs_url": str(c.docs_url) if c.docs_url else None,
                "description": c.description,
                # Listed, not trusted and not called: Consiva has no connector for it unless
                # the type is one it actually ships.
                "supported_by_consiva": c.type in SUPPORTED_CONNECTORS,
            }
            for c in manifest.connectors
        ],
    }


#: Every way a customer's data can reach the DSR agent today, and what each one needs. A fixed
#: list on purpose: discovery must not invent a path, and "not available" is a complete answer.
INTEGRATION_PATHS: tuple[dict[str, Any], ...] = (
    {
        "id": "postgres_direct",
        "title": "Direct PostgreSQL connection",
        "status": "supported",
        "summary": "Consiva connects to your PostgreSQL database with a read-only role to find a "
                   "requester's records, and (only if you later allow it) a separate write role to act.",
        "requires": [
            "A PostgreSQL database reachable from Consiva (a public address; private and internal "
            "addresses are refused)",
            "A read-only database role limited to the tables you want searched",
            "The source registered once in ROPA Sources, with its credential stored there",
            "An administrator to authorise the tables, columns and operations below",
        ],
    },
    {
        "id": "evidence_push",
        "title": "Push evidence from your own system",
        "status": "supported",
        "summary": "Your backend looks the person up itself and posts the matched records to "
                   "POST /api/v1/dsr/requests/{id}/evidence. Consiva never holds your database "
                   "credentials; erasures come back to you as an execution manifest, and you report "
                   "the outcome.",
        "requires": [
            "An integration key with the dsr:operate scope, held only by your backend",
            "A small change in your backend to call the evidence and execution-report endpoints",
        ],
    },
    {
        "id": "rest_connector",
        "title": "Call your REST API",
        "status": "not_available",
        "summary": "Consiva cannot yet search or change data through a customer's own API.",
        "alternative": "evidence_push",
    },
    {
        "id": "other_databases",
        "title": "MySQL, SQL Server and other databases",
        "status": "not_available",
        "summary": "Only PostgreSQL has a DSR connector today.",
        "alternative": "evidence_push",
    },
)


def assess_sources(sources: list[Any], authorizations: list[Any]) -> list[dict[str, Any]]:
    """For each source the organisation has registered: can DSR use it, and what is the next step.

    `readiness` is exactly one of: authorized, ready_to_authorize, needs_credential,
    connector_not_supported, disabled, revoked. Nothing here changes anything.
    """
    by_source = {a.data_source_id: a for a in authorizations}
    out: list[dict[str, Any]] = []
    for source in sources:
        auth = by_source.get(source.id)
        has_credential = bool(
            getattr(source, "credential_ref", None) or getattr(source, "credential_ciphertext", None)
        )
        if not source.enabled:
            readiness, step = "disabled", "Enable this source in ROPA Sources first."
        elif source.connector not in SUPPORTED_CONNECTORS:
            readiness = "connector_not_supported"
            step = (
                f"DSR cannot search {source.connector!r} sources. Use the push-evidence path instead."
            )
        elif not has_credential:
            readiness, step = "needs_credential", "Store this source's read credential in ROPA Sources."
        elif auth is None:
            readiness, step = "ready_to_authorize", "Choose which tables and columns DSR may use."
        elif not auth.enabled:
            readiness, step = "revoked", "This authorisation was revoked. Authorise it again to use it."
        else:
            readiness, step = "authorized", "Ready. Run the connection test to confirm it is healthy."
        out.append({
            "data_source_id": str(source.id),
            "name": source.name,
            "connector": source.connector,
            "readiness": readiness,
            "next_step": step,
            "allow_execution": bool(auth and auth.enabled and auth.allow_execution),
            "write_credential_stored": bool(auth and getattr(auth, "write_credential_ciphertext", None)),
        })
    return out
