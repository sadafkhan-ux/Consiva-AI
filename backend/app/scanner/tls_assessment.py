"""The site's SSL certificate and TLS version, observed for the scorecard (rows P11 and P12).

One short handshake with certificate checking ON (the same trust a browser applies), to the host the scan already accepted. It sends no request and reads no page. It records public facts only:
whether the certificate is trusted for the host, when it expires, and the best TLS version the server negotiates. Results:

  certificate.status   valid | expired | invalid | no_https | unreachable
  tls.version          "1.3" | "1.2" | "1.1" | "1.0" | None (not determined)

Nothing here guesses: a handshake that could not be made (timeout, DNS, refused) is "unreachable" and the scorecard leaves both rows out as not assessed, never passed or failed.
The best version is read from a client that offers 1.3, 1.2 and, only to find out that a server is stuck on an old version, 1.1 and 1.0 (so "TLS 1.1 or below" is a finding, not a guess).
"""

from __future__ import annotations

import asyncio
import logging
import socket
import ssl
from datetime import UTC, datetime
from urllib.parse import urlsplit

from app.scanner.tls_diagnostics import parse_certificate

logger = logging.getLogger(__name__)

#: OpenSSL's verify code for "certificate has expired".
_X509_EXPIRED = 10
_VERSION = {"TLSv1.3": "1.3", "TLSv1.2": "1.2", "TLSv1.1": "1.1", "TLSv1": "1.0"}


def _context(*, verify: bool, legacy: bool = False) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    if verify:
        ctx.load_default_certs()
    else:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE        # only to learn the version a server speaks after its certificate already failed; nothing is fetched
    if legacy:
        ctx.minimum_version = ssl.TLSVersion.TLSv1
        try:
            ctx.set_ciphers("ALL:@SECLEVEL=0")
        except ssl.SSLError:
            pass
    return ctx


def _handshake(host: str, port: int, ctx: ssl.SSLContext, timeout: float) -> tuple[str | None, bytes | None]:
    with socket.create_connection((host, port), timeout=timeout) as sock, ctx.wrap_socket(sock, server_hostname=host) as tls:
        return tls.version(), tls.getpeercert(binary_form=True)


def _certificate(der: bytes | None, host: str, status: str, reason: str | None = None, now: datetime | None = None) -> dict:
    out: dict = {"status": status, "reason": reason}
    if der:
        try:
            info = parse_certificate(der, host, now)
        except Exception:  # noqa: BLE001 -- a certificate we cannot parse is simply not described
            return out
        out.update({"not_after": info["not_after"], "not_before": info["not_before"], "issuer": info["issuer"], "subject": info["subject"],
                    "days_remaining": info["days_until_expiry"] if not info["expired"] else -(info["days_since_expiry"] or 0), "hostname_matches": info["hostname_matches"],
                    "self_signed": info["self_signed"]})
    return out


def probe(host: str, port: int = 443, timeout: float = 6.0) -> dict:
    """The certificate and TLS version of `host`. Blocking: call it through `assess_site_tls`."""
    result: dict = {"host": host, "port": port, "checked_at": datetime.now(UTC).date().isoformat(), "certificate": {"status": "unreachable", "reason": None}, "tls": {"version": None, "reason": None}}
    version: str | None = None
    der: bytes | None = None
    try:
        version, der = _handshake(host, port, _context(verify=True), timeout)
        result["certificate"] = _certificate(der, host, "valid")
    except ssl.SSLCertVerificationError as exc:
        expired = getattr(exc, "verify_code", None) == _X509_EXPIRED
        result["certificate"] = {"status": "expired" if expired else "invalid", "reason": (getattr(exc, "verify_message", None) or str(exc))[:160]}
        try:                                                    # the certificate's dates and the version, read without trusting it
            version, der = _handshake(host, port, _context(verify=False), timeout)
            result["certificate"] = _certificate(der, host, result["certificate"]["status"], result["certificate"]["reason"])
        except (OSError, ssl.SSLError):
            version = None
    except ssl.SSLError as exc:
        # No handshake at the modern floor (1.2): the server may only speak 1.1 or 1.0. Say so only if a legacy handshake really succeeds.
        try:
            legacy_version, der = _handshake(host, port, _context(verify=False, legacy=True), timeout)
            version = legacy_version
            result["certificate"] = _certificate(der, host, "invalid", "the server only offers a legacy TLS version, so the certificate could not be verified")
        except (OSError, ssl.SSLError):
            result["certificate"] = {"status": "unreachable", "reason": "the TLS handshake could not be completed: " + str(exc)[:120]}
    except OSError as exc:
        result["certificate"] = {"status": "unreachable", "reason": str(exc)[:160] or exc.__class__.__name__}
    if version:
        result["tls"] = {"version": _VERSION.get(version), "reason": None}
    elif result["certificate"]["status"] == "unreachable":
        result["tls"] = {"version": None, "reason": result["certificate"]["reason"]}
    return result


async def assess_site_tls(root_url: str, timeout: float = 6.0) -> dict:
    """The certificate and TLS record for the site's address. A site that is not served over https has no certificate to check: status "no_https" (a finding, since the page is open in transit)."""
    parts = urlsplit(root_url)
    host = parts.hostname
    if not host:
        return {"certificate": {"status": "unreachable", "reason": "no host in the address"}, "tls": {"version": None, "reason": "no host in the address"}}
    if parts.scheme != "https":
        return {"host": host, "certificate": {"status": "no_https", "reason": "the site was reached over http"}, "tls": {"version": None, "reason": "the site was reached over http"}}
    try:
        return await asyncio.wait_for(asyncio.to_thread(probe, host, parts.port or 443, timeout), timeout=timeout * 3 + 2)
    except Exception as exc:  # noqa: BLE001 -- an observation that could not be made is simply absent
        logger.warning("The certificate and TLS check for %s did not complete: %s", host, exc)
        return {"host": host, "certificate": {"status": "unreachable", "reason": "the check did not complete"}, "tls": {"version": None, "reason": "the check did not complete"}}
