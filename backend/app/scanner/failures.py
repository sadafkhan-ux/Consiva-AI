"""Why a scan could not reach a site, in plain words, and whether trying again could help.

Three kinds of navigation failure behave differently and were all treated alike (three immediate attempts, whatever the error):

  transient   a timeout, a resolver blip, a network change, or the site answering "try again" (HTTP 408, 425, 429, 500, 502, 503, 504). Retrying later can
              work, so these are retried with growing waits for as long as a time window allows (never for a fixed number of attempts).
  refused     the site reset or emptied the connection (HTTP/2 protocol error, empty response, connection reset or refused). A reset can be a blip, so it is
              retried briefly; if it keeps happening it is the site's own bot protection or network policy turning the scanner away, and more attempts only add
              load.
  permanent   retrying cannot change the answer: a certificate that does not validate, an invalid or unsafe address, or the site answering with an error
              (HTTP 401, 403, 404, 410 and the like) instead of a page.

A failure that cannot be helped by retrying is raised as ScanNotPossible, which the job queue does not retry and which says in plain words what happened, so a
scan never "completes" on an error page or with nothing scanned (it used to: a 403 "Access Denied" page was scored as the site itself).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

TRANSIENT, REFUSED, PERMANENT, DNS = "transient", "refused", "permanent", "dns"

#: The site said "try again": worth retrying later.
TRANSIENT_HTTP = frozenset({408, 425, 429, 500, 502, 503, 504})

_PERMANENT_MARKERS = (
    "ERR_CERT_", "ERR_INVALID_URL", "ERR_UNSAFE_PORT", "ERR_UNKNOWN_URL_SCHEME", "ERR_DISALLOWED_URL_SCHEME", "ERR_BLOCKED_BY_", "ERR_ABORTED",
    "ERR_TOO_MANY_REDIRECTS", "ERR_INVALID_REDIRECT", "ERR_ADDRESS_INVALID", "ERR_SSL_VERSION_OR_CIPHER_MISMATCH", "ERR_BAD_SSL_CLIENT_AUTH_CERT",
)
#: The name did not resolve. A misspelt or unregistered domain is the usual cause and retrying for minutes cannot change it, but a resolver blip is possible, so it is retried
#: briefly (the same short window as a refused connection) and then stopped with a message that says both.
_DNS_MARKERS = ("ERR_NAME_NOT_RESOLVED", "ERR_NAME_RESOLUTION_FAILED")
_REFUSED_MARKERS = ("ERR_HTTP2_PROTOCOL_ERROR", "ERR_EMPTY_RESPONSE", "ERR_CONNECTION_RESET", "ERR_CONNECTION_CLOSED", "ERR_CONNECTION_REFUSED")


class NavigationHttpError(Exception):
    """The page was reached but the site answered with an HTTP error instead of a page."""

    def __init__(self, status: int, url: str, retry_after: float | None = None):
        super().__init__(f"HTTP {status} from {url}")
        self.status = status
        self.url = url
        self.retry_after = retry_after


class RobotsUnreadable(RuntimeError):
    """robots.txt could not be read, so which pages the site permits is unknown and (policy "disallow") nothing was scanned. Retryable: the file may simply have been
    unavailable for a moment, and the scan is not made to look compliant by guessing."""

    def __init__(self, host: str, reason: str):
        super().__init__(
            f"Could not read the robots.txt of {host} ({reason}), so it cannot be determined which pages the site permits the scanner to visit. Nothing was assessed. "
            "The scan is retried automatically; if the file stays unreadable it ends as failed, and scanning again later may work.")
        self.reason = reason


class ScanNotPossible(RuntimeError):
    """A scan that cannot succeed however often it is repeated (see the module docstring). `retryable` is read by the job queue; `code` names the reason."""

    retryable = False

    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code


def classify_navigation_failure(error: BaseException | str | None) -> str:
    """transient, refused or permanent, from the error Playwright raised (or the HTTP status error)."""
    if isinstance(error, NavigationHttpError):
        return TRANSIENT if error.status in TRANSIENT_HTTP else PERMANENT
    text = str(error or "")
    if any(m in text for m in _PERMANENT_MARKERS):
        return PERMANENT
    if any(m in text for m in _DNS_MARKERS):
        return DNS
    if any(m in text for m in _REFUSED_MARKERS):
        return REFUSED
    return TRANSIENT


def parse_retry_after(value: str | None, ceiling: float = 30.0, now: datetime | None = None) -> float | None:
    """A Retry-After header as seconds to wait, bounded by `ceiling` so a site cannot park the scan. Both forms RFC 9110 defines are read: delta-seconds ("7") and an
    HTTP-date ("Wed, 21 Oct 2026 07:28:00 GMT", the wait being the time until then). None when the value is missing, malformed, or a date already in the past: the caller
    then uses its normal backoff, so a bad header can neither shorten nor lengthen the wait beyond the ceiling."""
    if not value:
        return None
    m = re.fullmatch(r"\s*(\d+)\s*", value)
    if m:
        return min(float(m.group(1)), ceiling)
    try:
        when = parsedate_to_datetime(value.strip())
    except (TypeError, ValueError, IndexError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    seconds = (when - (now or datetime.now(UTC))).total_seconds()
    return min(seconds, ceiling) if seconds > 0 else None


#: What the browser's error code means for the person reading the report, in the order they are tried. The scan outcome is decided by classify_navigation_failure; this
#: only names the likely cause so that "could not load" is never the whole explanation.
_CAUSES = (
    (("ERR_NAME_NOT_RESOLVED", "ERR_NAME_RESOLUTION_FAILED", "ERR_DNS_"), "the domain name did not resolve (DNS): check the spelling, or that the domain has DNS records"),
    (("ERR_CONNECTION_REFUSED",), "nothing is accepting connections on that address and port (or a firewall refused the scanner)"),
    (("ERR_PROXY_", "ERR_TUNNEL_CONNECTION_FAILED"), "the network path to the site goes through a proxy that failed"),
    (("ERR_CONNECTION_TIMED_OUT", "ERR_TIMED_OUT", "Timeout"), "the site did not answer in time (it is slow, overloaded, or a firewall is silently dropping the scanner)"),
    (("ERR_ADDRESS_UNREACHABLE", "ERR_INTERNET_DISCONNECTED", "ERR_NETWORK_CHANGED", "ERR_NETWORK_ACCESS_DENIED"), "there is no network route from the scanner to the site"),
    (("ERR_CONNECTION_RESET", "ERR_CONNECTION_CLOSED", "ERR_EMPTY_RESPONSE", "ERR_HTTP2_PROTOCOL_ERROR"), "the site cut the connection, as some firewalls and bot protection do to automated visitors"),
    (("ERR_CERT_", "ERR_SSL_"), "the site's security certificate or TLS setup is not valid"),
)


def likely_cause(error: BaseException | str | None) -> str:
    """A short plain-words cause for a browser error, or '' when it is not one we recognise."""
    text = str(error or "")
    return next((cause for markers, cause in _CAUSES if any(m in text for m in markers)), "")


#: What an HTTP client (not the browser) says when a site's TLS certificate cannot be trusted. Retrying cannot change it, so it is a permanent failure wherever it is met.
_CERTIFICATE_MARKERS = (
    "certificate has expired", "certificate is not yet valid", "self signed certificate", "self-signed certificate", "unable to verify the first certificate",
    "unable to get local issuer certificate", "certificate verify failed", "DEPTH_ZERO_SELF_SIGNED_CERT", "CERT_HAS_EXPIRED", "ERR_CERT_", "altnames",
)


def is_certificate_error(error: BaseException | str | None) -> bool:
    text = str(error or "")
    return any(m.lower() in text.lower() for m in _CERTIFICATE_MARKERS)


def certificate_failure(url: str, reason: str, diagnosis: str = "") -> ScanNotPossible:
    """The scan stops, with the cause, when the site's security certificate is not valid. Seen live: uat.consiva.ai answered the robots.txt request with "certificate has expired", which
    was reported as an unreadable robots.txt to be retried, though no retry can make an expired certificate valid."""
    return ScanNotPossible(
        f"Could not reach the site at all: {url} does not present a valid security certificate ({reason}). Trying again cannot change this until the site renews or corrects its "
        "certificate, so the scan was stopped and nothing was assessed." + diagnosis, "certificate_invalid")


def first_line(error: BaseException | str | None, limit: int = 160) -> str:
    """The one-line cause of a Playwright error (its call log is not for a reader)."""
    return (str(error or "").strip().splitlines() or [""])[0][:limit]


def explain_seed_failure(url: str, cause: BaseException | None, attempts: int) -> ScanNotPossible | RuntimeError:
    """The exception for a seed address that could not be loaded: ScanNotPossible when repeating the scan cannot help, a plain (retryable) RuntimeError when it
    might (the site was slow or briefly unavailable and the retry window ran out)."""
    kind = classify_navigation_failure(cause)
    tries = f"{attempts} attempt{'s' if attempts != 1 else ''}"
    if isinstance(cause, NavigationHttpError):
        status = cause.status
        if kind == PERMANENT:
            hint = ("The site is refusing the scanner (access denied), which is how the bot protection of some sites turns automated visitors away."
                    if status in (401, 403, 451) else "The address does not serve a page.")
            return ScanNotPossible(
                f"Could not reach the site at all: {url} answered with HTTP {status} instead of a page, so there was nothing real to scan and no report was made. "
                f"{hint} Nothing was assessed.", "http_blocked" if status in (401, 403, 451) else "http_error")
        return RuntimeError(f"Could not reach the site at all: {url} kept answering with HTTP {status} (temporarily unavailable) after {tries}; the scan can be tried again later.")
    reason = first_line(cause)
    cause_text = likely_cause(cause)
    if cause_text:
        reason = f"{reason}; likely cause: {cause_text}"
    if kind == PERMANENT:
        return ScanNotPossible(f"Could not reach the site at all: {url} could not be loaded ({reason}). Trying again cannot change this, so the scan was stopped and nothing was assessed.", "unreachable")
    if kind == DNS:
        return ScanNotPossible(
            f"Could not reach the site at all: the domain name of {url} did not resolve on any of {tries} ({reason}). Check the spelling of the address and that the domain is registered "
            "and has DNS records; if it is correct and the problem is on the scanner's network, scan again later. Nothing was assessed.", "dns_failure")
    if kind == REFUSED:
        return ScanNotPossible(
            f"Could not reach the site at all: {url} closed the connection to the scanner on every one of {tries} ({reason}). Some sites do this to automated "
            "visitors, so retrying only adds load. Nothing was assessed.", "refused")
    return RuntimeError(f"Could not reach the site at all: {url} was too slow or unavailable over the whole retry window ({tries}; {reason}); the scan can be tried again later.")
