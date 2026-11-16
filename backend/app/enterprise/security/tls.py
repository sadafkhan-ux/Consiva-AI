"""Probe a site's TLS: is the certificate valid and for how long, which protocol version is negotiated, and is TLS 1.3 offered.

The address is checked with the same SSRF guard as the crawl and the connection goes to that checked address, so the probe cannot be steered at a private host by DNS. The evidence
stored holds only what the server said (the certificate's end date, not "days remaining"), so the same server gives the same evidence on every run and the days left are worked out
at assessment time. Older protocol versions (TLS 1.0 and 1.1) are not probed: a current client does not offer them, and the report says so.
"""

from __future__ import annotations

import asyncio
import logging
import ssl
from datetime import datetime, timezone

from app.scanner.url_safety import assert_safe_host

logger = logging.getLogger(__name__)
TIMEOUT_S = 10.0


def _rdn(parts) -> str:
    """The certificate's issuer or subject as a short name: its organisation, else its common name."""
    flat = {k: v for rdn in parts or () for k, v in rdn}
    return flat.get("organizationName") or flat.get("commonName") or ""


def describe_cert(cert: dict) -> dict:
    """A verified certificate (as `getpeercert()` returns it) as plain evidence. Dates are ISO, in UTC."""
    def iso(key: str) -> str | None:
        raw = cert.get(key)
        return None if not raw else datetime.fromtimestamp(ssl.cert_time_to_seconds(raw), tz=timezone.utc).isoformat()
    return {
        "not_before": iso("notBefore"), "not_after": iso("notAfter"), "issuer": _rdn(cert.get("issuer")), "subject": _rdn(cert.get("subject")),
        "alt_names": [v for k, v in cert.get("subjectAltName", ()) if k == "DNS"][:20],
    }


async def _handshake(ip: str, host: str, port: int, *, verify: bool = True, only_tls13: bool = False) -> tuple[str, dict]:
    ctx = ssl.create_default_context()
    if not verify:
        ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
    if only_tls13:
        ctx.minimum_version = ctx.maximum_version = ssl.TLSVersion.TLSv1_3
    reader, writer = await asyncio.wait_for(asyncio.open_connection(ip, port, ssl=ctx, server_hostname=host), TIMEOUT_S)
    try:
        obj = writer.get_extra_info("ssl_object")
        return obj.version() or "", (obj.getpeercert() or {})
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def probe_tls(host: str, port: int = 443) -> dict:
    """What the server's TLS showed. Never raises: a failure is part of the answer (`reachable` false, or `verified` false with the reason)."""
    out: dict = {"probe": "tls", "host": host, "port": port, "reachable": False, "verified": False, "verify_error": None, "negotiated": None, "tls13_supported": None}
    try:
        ips = await assert_safe_host(host)
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"[:200]
        return out
    ip = ips[0]
    try:
        version, cert = await _handshake(ip, host, port)
        out.update(reachable=True, verified=True, negotiated=version, **describe_cert(cert))
    except ssl.SSLCertVerificationError as exc:
        out.update(reachable=True, verify_error=(exc.verify_message or str(exc))[:200])
        try:
            version, _ = await _handshake(ip, host, port, verify=False)
            out["negotiated"] = version
        except Exception:
            pass
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"[:200]
        return out
    try:
        await _handshake(ip, host, port, verify=out["verified"], only_tls13=True)
        out["tls13_supported"] = True
    except (ssl.SSLError, OSError, asyncio.TimeoutError):
        out["tls13_supported"] = False
    return out
