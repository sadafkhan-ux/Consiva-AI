"""What certificate a site presented, read only to explain a certificate failure.

When a scan fails because a site's TLS certificate cannot be trusted, "certificate has expired" does not say which certificate, since when, or whether it is the right host's. This reads the certificate
the server presents so the report can say it ("expired on 2026-09-30, 27 days ago; issued by R11 for uat.consiva.ai").

This is the one place that opens a TLS connection without verifying the certificate, and it is deliberately narrow: it only completes the handshake to take the certificate's public fields (names,
dates, issuer), sends no request, reads no page, and its result is only ever text in an explanation. It is never used to fetch content, and the scan itself keeps verification on. It runs only after
the verified connection has already failed, to a host the scan had already accepted as a safe public address.
"""

from __future__ import annotations

import asyncio
import fnmatch
import socket
import ssl
from datetime import UTC, datetime

from cryptography import x509
from cryptography.x509.oid import ExtensionOID, NameOID


def _name(name: x509.Name) -> str:
    for oid in (NameOID.COMMON_NAME, NameOID.ORGANIZATION_NAME):
        attrs = name.get_attributes_for_oid(oid)
        if attrs:
            return str(attrs[0].value)
    return ""


def _host_matches(host: str, names: list[str]) -> bool:
    host = host.lower()
    return any(n.lower() == host or (n.startswith("*.") and fnmatch.fnmatch(host, n.lower()) and host.count(".") == n.count(".")) for n in names)


def parse_certificate(der: bytes, host: str, now: datetime | None = None) -> dict:
    """The public facts of a DER certificate, and what they mean for `host`."""
    now = now or datetime.now(UTC)
    cert = x509.load_der_x509_certificate(der)
    not_after = cert.not_valid_after_utc
    not_before = cert.not_valid_before_utc
    try:
        san = cert.extensions.get_extension_for_oid(ExtensionOID.SUBJECT_ALTERNATIVE_NAME).value.get_values_for_type(x509.DNSName)
    except x509.ExtensionNotFound:
        san = []
    names = list(san) or ([_name(cert.subject)] if _name(cert.subject) else [])
    return {
        "subject": _name(cert.subject), "issuer": _name(cert.issuer), "names": names[:10],
        "not_before": not_before.date().isoformat(), "not_after": not_after.date().isoformat(),
        "expired": now > not_after, "not_yet_valid": now < not_before,
        "days_since_expiry": (now - not_after).days if now > not_after else None,
        "days_until_expiry": (not_after - now).days if now <= not_after else None,
        "hostname_matches": _host_matches(host, names),
        "self_signed": cert.issuer == cert.subject,
    }


def inspect_certificate(host: str, port: int = 443, timeout: float = 6.0) -> dict:
    """The certificate `host` presents, or {"error": ...} when it cannot be read. Blocking: call it through `describe_certificate`."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False          # see the module docstring: only to read the certificate that failed verification, never to fetch anything
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock, ctx.wrap_socket(sock, server_hostname=host) as tls:
            der = tls.getpeercert(binary_form=True)
        return parse_certificate(der, host) if der else {"error": "the server presented no certificate"}
    except Exception as exc:  # noqa: BLE001 -- a diagnostic must never fail the scan's own error handling
        return {"error": str(exc)[:160]}


async def describe_certificate(host: str | None, port: int = 443) -> dict | None:
    if not host:
        return None
    try:
        return await asyncio.wait_for(asyncio.to_thread(inspect_certificate, host, port), timeout=10)
    except Exception:  # noqa: BLE001
        return None


def sentence(host: str, info: dict | None) -> str:
    """One sentence for an explanation, or '' when nothing could be read."""
    if not info or "error" in info:
        return ""
    parts = []
    if info["expired"]:
        days = info["days_since_expiry"]
        ago = "less than a day ago" if not days else f"{days} day{'s' if days != 1 else ''} ago"
        parts.append(f"expired on {info['not_after']} ({ago})")
    elif info["not_yet_valid"]:
        parts.append(f"is not valid until {info['not_before']}")
    else:
        parts.append(f"is valid until {info['not_after']}")
    if info["issuer"]:
        parts.append(f"issued by {info['issuer']}")
    if info["self_signed"]:
        parts.append("is self-signed")
    if not info["hostname_matches"]:
        shown = ", ".join(info["names"][:4]) or "no names"
        parts.append(f"does not name {host} (it names {shown})")
    return f" The certificate {host} presents {'; '.join(parts)}."
