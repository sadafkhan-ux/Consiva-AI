"""Domain-ownership verification via DNS TXT record (master reference §12 Critical:
"Back the scan-authorization attestation with real domain-ownership verification...
before any third-party scanning goes live").

Flow: the org asks for its expected token (deterministic per org+domain, so nothing
new needs storing), publishes `consiva-verify=<token>` as a TXT record on the domain,
then calls verify — a successful DNS match stamps websites.verified_at. Enforcement is
gated behind settings.scanner_require_domain_verification (default False) so the
existing self-attestation demo flow keeps working until the flag is deliberately
flipped for a pilot; the attestation checkbox remains required either way.
"""

import hashlib
import hmac
import logging
import uuid
from datetime import UTC, datetime

import dns.asyncresolver
import dns.exception
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.exceptions import ScanAuthorizationError
from app.db.repositories import scan_repository

logger = logging.getLogger(__name__)

_TXT_PREFIX = "consiva-verify="


def expected_token(org_id: uuid.UUID, domain: str) -> str:
    """Deterministic HMAC of org+domain keyed on the server-side JWT secret: the token
    proves the publisher controls the domain's DNS AND was told the token by us (it's
    only ever served to an authenticated member of that org), with no extra table or
    expiry state to manage. Domain is lowercased so `Example.com` and `example.com`
    verify identically."""
    settings = get_settings()
    message = f"{org_id}:{domain.lower()}".encode()
    return hmac.new(settings.supabase_jwt_secret.encode(), message, hashlib.sha256).hexdigest()[:32]


async def _domain_has_token(domain: str, token: str) -> bool:
    resolver = dns.asyncresolver.Resolver()
    resolver.lifetime = 10
    try:
        answers = await resolver.resolve(domain, "TXT")
    except (dns.exception.DNSException, OSError) as exc:
        logger.info("TXT lookup failed for %s: %s", domain, exc)
        return False
    wanted = f"{_TXT_PREFIX}{token}"
    for rdata in answers:
        # a TXT record's value arrives as one or more quoted byte-strings
        value = "".join(part.decode("utf-8", "replace") for part in rdata.strings)
        if value.strip() == wanted:
            return True
    return False


async def verify_domain(db: AsyncSession, *, org_id: uuid.UUID, domain: str) -> datetime:
    """Checks the domain's TXT records for this org's token and stamps
    websites.verified_at on success. Raises ScanAuthorizationError (403) on failure —
    a failed verification is a permission problem, not a server error."""
    domain = domain.lower().strip()
    token = expected_token(org_id, domain)
    if not await _domain_has_token(domain, token):
        raise ScanAuthorizationError(
            f"Domain verification failed for {domain}: no TXT record "
            f"'{_TXT_PREFIX}{token}' found. Publish that record on the domain's DNS "
            "and retry (propagation can take a few minutes)."
        )
    website = await scan_repository.get_or_create_website(db, org_id=org_id, domain=domain)
    website.verified_at = datetime.now(UTC)
    await db.flush()
    return website.verified_at
