"""One vocabulary for why a scan could not be completed, and what to do about it.

Every failure the scanner can meet (an unreadable robots.txt, an expired certificate, a refused connection, a rate limit...) is described the same way, for every website: a machine-readable
code, a plain explanation, whether trying again can help, who has to act, and the next step. The pieces that already existed (scanner/failures.py: retry classification and the exceptions;
scanner/robots.py: the robots.txt rules) stay the source of the decisions; this module only NAMES them, so the API, the dashboard and the report cannot disagree about what happened.

Nothing here is stored: it is derived from the exception, or from the error text a scan already carries, so scans stored before this existed are described the same way (and nothing in the
database changes). A technical failure is never a compliance finding: a scan that could not look says so, and says nothing about the site's consent practices.

Also here: `scan_outcome`, the lifecycle word for a scan (QUEUED ... BLOCKED, FAILED) worked out from the stored status plus what was actually assessed, so "completed" can never be said of a
scan that assessed no page.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from enum import StrEnum


class FailureCategory(StrEnum):
    ROBOTS_TXT_DISALLOWED = "ROBOTS_TXT_DISALLOWED"
    ROBOTS_TXT_UNAVAILABLE = "ROBOTS_TXT_UNAVAILABLE"
    TLS_CERTIFICATE_EXPIRED = "TLS_CERTIFICATE_EXPIRED"
    TLS_CERTIFICATE_INVALID = "TLS_CERTIFICATE_INVALID"
    DNS_RESOLUTION_FAILED = "DNS_RESOLUTION_FAILED"
    CONNECTION_FAILED = "CONNECTION_FAILED"
    REQUEST_TIMEOUT = "REQUEST_TIMEOUT"
    HTTP_ACCESS_DENIED = "HTTP_ACCESS_DENIED"
    AUTHENTICATION_REQUIRED = "AUTHENTICATION_REQUIRED"
    RATE_LIMITED = "RATE_LIMITED"
    SERVER_ERROR = "SERVER_ERROR"
    BROWSER_NAVIGATION_FAILED = "BROWSER_NAVIGATION_FAILED"
    PARTIAL_SCAN = "PARTIAL_SCAN"
    UNKNOWN_SCAN_ERROR = "UNKNOWN_SCAN_ERROR"


# Who has to act: the website's owner (a certificate, a robots.txt, access), the operator of this scanner (its network, its settings), or nobody (try again).
OWNER_WEBSITE = "website_owner"
OWNER_OPERATOR = "scanner_operator"
OWNER_NONE = "none"


@dataclass(frozen=True)
class FailureInfo:
    category: FailureCategory
    code: str                     # the category's name: stable and machine-readable
    title: str
    explanation: str
    retryable: bool               # whether scanning again, unchanged, could succeed (a permanent failure never does)
    transient: bool               # whether the cause is likely temporary
    owner: str                    # who must act
    next_action: str
    detail: str = ""              # the underlying error, as the browser or client reported it (one line, no secrets). For developers and integrators: a user interface does not show it.
    url: str | None = None        # the address concerned
    user_message: str = ""        # what happened, in one plain sentence for a person (no technical terms)
    what_you_can_do: str = ""     # what that person can do next, in one plain sentence
    short: str = ""               # a few words for a status label

    def as_dict(self) -> dict:
        return {**asdict(self), "category": self.category.value}


@dataclass(frozen=True)
class _Spec:
    title: str
    retryable: bool
    transient: bool
    owner: str
    explanation: str
    next_action: str


_CATALOG: dict[FailureCategory, _Spec] = {
    FailureCategory.ROBOTS_TXT_DISALLOWED: _Spec(
        title="Blocked by robots.txt", retryable=False, transient=False, owner=OWNER_WEBSITE,
        explanation="The site's robots.txt explicitly does not allow this scanner to visit the address, and the scanner honours robots.txt. No page was scanned and nothing was assessed.",
        next_action="If you control the site, allow the scanner in robots.txt (for example 'User-agent: ConsivaConsentAgent' followed by 'Allow: /'), then scan again. If you do not control it, ask its owner for "
                    "permission or scan a page the site allows. The restriction is never overridden."),
    FailureCategory.ROBOTS_TXT_UNAVAILABLE: _Spec(
        title="robots.txt could not be read", retryable=True, transient=True, owner=OWNER_NONE,
        explanation="The site's robots.txt could not be retrieved, so it cannot be established which pages the scanner may visit. An unreadable robots.txt is not treated as permission to scan. Nothing was assessed.",
        next_action="The scan is retried automatically with growing waits. If it keeps failing, check that the site's robots.txt answers (HTTP 200 or 404) and is not slow or blocked for the scanner, then scan again."),
    FailureCategory.TLS_CERTIFICATE_EXPIRED: _Spec(
        title="The site's security certificate has expired", retryable=False, transient=False, owner=OWNER_WEBSITE,
        explanation="The site presented a TLS certificate that has expired, so a browser cannot trust it either. Certificate checking is never turned off to make a scan succeed. Nothing was assessed, and this says "
                    "nothing about the site's consent practices.",
        next_action="The site's owner must renew the certificate (and install the full chain) for the host named in the error. Scan again once the site loads in a normal browser without a warning."),
    FailureCategory.TLS_CERTIFICATE_INVALID: _Spec(
        title="The site's security certificate is not valid", retryable=False, transient=False, owner=OWNER_WEBSITE,
        explanation="The site's TLS certificate is untrusted, self-signed, issued for a different host name, or its chain is incomplete. Certificate checking is never turned off to make a scan succeed. Nothing was "
                    "assessed, and this says nothing about the site's consent practices.",
        next_action="The site's owner must install a certificate that a browser trusts for this exact host name, with its full chain. If the same error appears for many sites, check this scanner's own certificate "
                    "store and clock. Scan again once the site loads in a normal browser without a warning."),
    FailureCategory.DNS_RESOLUTION_FAILED: _Spec(
        title="The domain name did not resolve", retryable=False, transient=False, owner=OWNER_WEBSITE,
        explanation="The address's domain name could not be resolved after a short bounded retry. A misspelt or unregistered name is the usual cause; a resolver outage on the scanner's network is possible. Nothing was assessed.",
        next_action="Check the spelling of the address and that the domain is registered with DNS records. If it is correct and other sites fail the same way, check the scanner's DNS, then scan again."),
    FailureCategory.CONNECTION_FAILED: _Spec(
        title="The connection to the site failed", retryable=False, transient=False, owner=OWNER_WEBSITE,
        explanation="The site refused, reset or closed the connection on every bounded attempt. Some sites do this to automated visitors; retrying only adds load. Nothing was assessed.",
        next_action="Check that the site is up and accepting connections on this address and port, and that a firewall or bot protection is not turning the scanner away. If you control it, allow the scanner's address, "
                    "then scan again."),
    FailureCategory.REQUEST_TIMEOUT: _Spec(
        title="The site did not answer in time", retryable=True, transient=True, owner=OWNER_NONE,
        explanation="The site was too slow or unavailable for the whole bounded retry window. Nothing was assessed.",
        next_action="Scan again later. If it keeps timing out, the site may be overloaded or silently dropping the scanner; check it from another network."),
    FailureCategory.HTTP_ACCESS_DENIED: _Spec(
        title="The site denied access", retryable=False, transient=False, owner=OWNER_WEBSITE,
        explanation="The site answered with an access-denied response (for example HTTP 403) instead of a page: its firewall or bot protection is turning the scanner away. The error page is not scanned as if it were the site.",
        next_action="If you control the site, allow the scanner in its firewall or bot protection, then scan again. Otherwise ask the owner for permission. Access controls are never bypassed."),
    FailureCategory.AUTHENTICATION_REQUIRED: _Spec(
        title="The page needs a login", retryable=False, transient=False, owner=OWNER_WEBSITE,
        explanation="The address requires authentication (HTTP 401), which the scanner does not have. Nothing behind a login is scanned.",
        next_action="Scan a public page of the site instead. Testing a logged-in area needs explicit authorisation and a test account, which is not part of an automated scan."),
    FailureCategory.RATE_LIMITED: _Spec(
        title="The site is rate limiting the scanner", retryable=True, transient=True, owner=OWNER_NONE,
        explanation="The site answered HTTP 429 (too many requests). The scanner waited as the site asked and retried within a bounded window, and was still limited. Nothing was assessed.",
        next_action="Scan again later, and avoid scanning the same site many times at once. If you control the site, raise the limit for the scanner."),
    FailureCategory.SERVER_ERROR: _Spec(
        title="The site returned a server error", retryable=True, transient=True, owner=OWNER_NONE,
        explanation="The site answered with a server error (HTTP 5xx) on every bounded attempt. Nothing was assessed.",
        next_action="Scan again later; if the site is yours, check its server logs."),
    FailureCategory.BROWSER_NAVIGATION_FAILED: _Spec(
        title="The browser could not load the page", retryable=True, transient=True, owner=OWNER_OPERATOR,
        explanation="The browser could not complete navigation to the address and no more specific cause was identified. Nothing was assessed.",
        next_action="Open the address in a normal browser. If it loads there, scan again; if it keeps failing, give the error shown below to the scanner's operator."),
    FailureCategory.PARTIAL_SCAN: _Spec(
        title="Some pages could not be assessed", retryable=True, transient=True, owner=OWNER_NONE,
        explanation="Some pages were scanned, but others that were requested could not be loaded or were not allowed. The assessment covers only the pages that were scanned.",
        next_action="Read the coverage beside the score. Scan again to retry the pages that failed; pages blocked by robots.txt or access controls need the site owner's permission."),
    FailureCategory.UNKNOWN_SCAN_ERROR: _Spec(
        title="The scan failed for an unrecognised reason", retryable=True, transient=False, owner=OWNER_OPERATOR,
        explanation="The scan failed and the error was not recognised as any known cause, so it is reported as it was received. It says nothing about the site's consent practices.",
        next_action="Scan again; if it repeats, give the error below to the scanner's operator."),
}


# What a person is told, for every category: short, plain, from their point of view, with no technical terms (a test enforces it). The technical explanation above and the underlying error stay for
# developers and the logs.
_USER: dict[FailureCategory, tuple[str, str, str]] = {
    FailureCategory.ROBOTS_TXT_DISALLOWED: ("short=Not allowed by the website", "This website doesn't allow automated scanning, so we didn't scan it.", "Ask the website's owner to allow it, or try a page the site permits."),
    FailureCategory.ROBOTS_TXT_UNAVAILABLE: ("short=Couldn't check the website's rules", "We couldn't check this website's scanning rules, so we didn't scan it.", "Try again in a few minutes."),
    FailureCategory.TLS_CERTIFICATE_EXPIRED: ("short=Website security has expired", "This website's security certificate has expired, so we couldn't open it safely.", "The website's owner needs to renew it. Try again afterwards."),
    FailureCategory.TLS_CERTIFICATE_INVALID: ("short=Website security problem", "This website's security certificate isn't valid, so we couldn't open it safely.", "The website's owner needs to fix it. Try again afterwards."),
    FailureCategory.DNS_RESOLUTION_FAILED: ("short=Website not found", "We couldn't find this website address.", "Check the spelling of the address and try again."),
    FailureCategory.CONNECTION_FAILED: ("short=Couldn't connect", "The website refused or dropped our connection.", "Check that the site is online. If it's yours, allow our scanner, then try again."),
    FailureCategory.REQUEST_TIMEOUT: ("short=Website too slow", "The website took too long to respond.", "Try again in a few minutes."),
    FailureCategory.HTTP_ACCESS_DENIED: ("short=Blocked by the website", "The website blocked our scanner.", "If it's your site, allow the scanner in its firewall. Otherwise ask the owner for permission."),
    FailureCategory.AUTHENTICATION_REQUIRED: ("short=Needs a login", "This page needs a login, which we can't use.", "Scan a public page of the site instead."),
    FailureCategory.RATE_LIMITED: ("short=Website is limiting us", "The website is limiting how fast we can scan it.", "Wait a few minutes and try again."),
    FailureCategory.SERVER_ERROR: ("short=Website had a problem", "The website had a problem and couldn't load.", "Try again later."),
    FailureCategory.BROWSER_NAVIGATION_FAILED: ("short=Couldn't open the page", "We couldn't open this page.", "Check that it opens in your browser, then try again."),
    FailureCategory.PARTIAL_SCAN: ("short=Some pages couldn't be scanned", "Some pages couldn't be scanned, so the results cover only the pages that were.", "Scan again to retry the others."),
    FailureCategory.UNKNOWN_SCAN_ERROR: ("short=Something went wrong", "Something went wrong while scanning.", "Please try again. If it keeps happening, contact support and quote the scan ID."),
}


def describe(category: FailureCategory, *, detail: str = "", url: str | None = None) -> FailureInfo:
    spec = _CATALOG[category]
    short, message, action = _USER[category]
    return FailureInfo(category=category, code=category.value, detail=_one_line(detail), url=url, user_message=message, what_you_can_do=action, short=short.removeprefix("short="), **asdict(spec))


def _one_line(text: str, limit: int = 240) -> str:
    return (str(text or "").strip().splitlines() or [""])[0][:limit]


# ── from the text of an error ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────

_EXPIRED = re.compile(r"certificate has expired|cert_has_expired|err_cert_date_invalid|certificate is not yet valid|expired certificate", re.IGNORECASE)
_CERT = re.compile(r"certificate|err_cert_|err_ssl_|depth_zero_self_signed|altnames|self[- ]signed|unable to verify the first|local issuer", re.IGNORECASE)
_DNS = re.compile(r"did not resolve|err_name_not_resolved|err_name_resolution_failed|enotfound|getaddrinfo|dns_failure", re.IGNORECASE)
_REFUSED = re.compile(r"closed the connection|econnrefused|econnreset|err_connection_(refused|reset|closed)|err_empty_response|err_http2_protocol_error|socket hang up|refused the connection", re.IGNORECASE)
_TIMEOUT = re.compile(r"timeout|timed out|too slow or unavailable|err_timed_out|err_connection_timed_out|exceeded its .*(budget|time)|was aborted so the worker", re.IGNORECASE)
_STATUS = re.compile(r"\bhttp (\d{3})\b", re.IGNORECASE)


def classify_message(message: str | None, *, url: str | None = None) -> FailureInfo | None:
    """What a stored scan's error text means, or None when there is no error. Text this scanner wrote is recognised exactly; browser and client errors by their well-known phrases."""
    if not message or not str(message).strip():
        return None
    text = str(message)
    low = text.lower()
    if "robots.txt" in low and "does not allow" in low:
        return describe(FailureCategory.ROBOTS_TXT_DISALLOWED, detail=text, url=url)
    # A certificate problem is the cause even when it surfaced while reading robots.txt (an older scan stored "could not read the robots.txt (certificate has expired)").
    if _EXPIRED.search(text):
        return describe(FailureCategory.TLS_CERTIFICATE_EXPIRED, detail=text, url=url)
    if _CERT.search(text):
        return describe(FailureCategory.TLS_CERTIFICATE_INVALID, detail=text, url=url)
    if "robots.txt" in low and ("could not read" in low or "unreadable" in low):
        return describe(FailureCategory.ROBOTS_TXT_UNAVAILABLE, detail=text, url=url)
    if _DNS.search(text):
        return describe(FailureCategory.DNS_RESOLUTION_FAILED, detail=text, url=url)
    m = _STATUS.search(text)
    status = int(m.group(1)) if m else None
    if status == 429:
        return describe(FailureCategory.RATE_LIMITED, detail=text, url=url)
    if status == 401:
        return describe(FailureCategory.AUTHENTICATION_REQUIRED, detail=text, url=url)
    if status in (403, 451) or "access denied" in low or "refusing the scanner" in low:
        return describe(FailureCategory.HTTP_ACCESS_DENIED, detail=text, url=url)
    if status is not None and 500 <= status < 600:
        return describe(FailureCategory.SERVER_ERROR, detail=text, url=url)
    if _REFUSED.search(text):
        return describe(FailureCategory.CONNECTION_FAILED, detail=text, url=url)
    if _TIMEOUT.search(text):
        return describe(FailureCategory.REQUEST_TIMEOUT, detail=text, url=url)
    if "could not reach the site at all" in low or "navigation" in low or "could not be loaded" in low:
        return describe(FailureCategory.BROWSER_NAVIGATION_FAILED, detail=text, url=url)
    return describe(FailureCategory.UNKNOWN_SCAN_ERROR, detail=text, url=url)


def classify_exception(exc: BaseException, *, url: str | None = None) -> FailureInfo:
    """The same, from the exception itself: the scanner's own exceptions carry a code, anything else is read by its text."""
    from app.scanner.failures import RobotsUnreadable, ScanNotPossible

    if isinstance(exc, RobotsUnreadable):
        return describe(FailureCategory.ROBOTS_TXT_UNAVAILABLE, detail=str(exc), url=url)
    if isinstance(exc, ScanNotPossible):
        by_code = {"robots_disallowed": FailureCategory.ROBOTS_TXT_DISALLOWED, "dns_failure": FailureCategory.DNS_RESOLUTION_FAILED, "refused": FailureCategory.CONNECTION_FAILED}
        if exc.code in by_code:
            return describe(by_code[exc.code], detail=str(exc), url=url)
    return classify_message(str(exc), url=url) or describe(FailureCategory.UNKNOWN_SCAN_ERROR, detail=type(exc).__name__, url=url)


# ── the lifecycle word ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

QUEUED, RUNNING, COMPLETED, COMPLETED_WITH_WARNINGS = "QUEUED", "RUNNING", "COMPLETED", "COMPLETED_WITH_WARNINGS"
PARTIALLY_COMPLETED, BLOCKED, FAILED, CANCELLED = "PARTIALLY_COMPLETED", "BLOCKED", "FAILED", "CANCELLED"

#: A failure whose cause is a restriction on access, not a fault: the scan was blocked, not broken.
_BLOCKING = frozenset({FailureCategory.ROBOTS_TXT_DISALLOWED, FailureCategory.ROBOTS_TXT_UNAVAILABLE, FailureCategory.HTTP_ACCESS_DENIED, FailureCategory.AUTHENTICATION_REQUIRED})


def scan_outcome(
    status: str, *, error: str | None = None, pages_scanned: int | None = None, pages_not_assessed: int = 0, warnings: bool = False,
) -> str:
    """The lifecycle word for a scan, worked out from its stored status and what was assessed. A scan that assessed no page is never "completed": it was blocked or it failed.

    `pages_not_assessed` counts requested pages that failed, timed out or were blocked; `warnings` is true when the scan completed but could not test everything it set out to (for
    example the consent banner was never displayed, so Accept and Reject were not tested)."""
    s = (status or "").lower()
    if s in ("queued", "pending"):
        return QUEUED
    if s in ("running", "in_progress", "analyzing"):
        return RUNNING
    if s == "cancelled":
        return CANCELLED
    if s == "failed":
        info = classify_message(error)
        return BLOCKED if info and info.category in _BLOCKING else FAILED
    if s == "completed":
        if pages_scanned is not None and pages_scanned <= 0:
            info = classify_message(error)
            return BLOCKED if info and info.category in _BLOCKING else FAILED
        if pages_not_assessed > 0:
            return PARTIALLY_COMPLETED
        return COMPLETED_WITH_WARNINGS if warnings else COMPLETED
    return FAILED if error else RUNNING
