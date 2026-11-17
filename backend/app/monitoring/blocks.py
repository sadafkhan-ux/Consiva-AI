"""Is the website turning our scanner away? Recognises the answers of bot protection and access control, and tells them apart from an ordinary failure.

  BOT_BLOCKED     the site actively challenged or refused an automated visitor: a Cloudflare or other WAF challenge page, a CAPTCHA, a "verify you are human" page, an anti-bot response header, a bot-protection
                  access-denied page.
  ACCESS_BLOCKED  the site refused with an access status and no page worth reading (HTTP 401, 403, 429, 451) or cut the connection the way bot protection does.

NOT a block: a server error (500, 502, 503, 504 without a challenge), a DNS failure, a timeout, a certificate error, a site that is down. Those are other categories with other advice.

This only RECOGNISES a block. Nothing here solves a CAPTCHA, answers a challenge, or works around a firewall, and no code in the application does: a blocked scan stops and says so.

A page that merely contains a CAPTCHA widget (a contact form protected by reCAPTCHA) is a normal page. A challenge is recognised from a status that refuses, a challenge header, or a SHORT page whose title or text is the challenge itself.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

BOT_BLOCKED, ACCESS_BLOCKED = "BOT_BLOCKED", "ACCESS_BLOCKED"
USER_MESSAGE = "Website blocked our scanner."
EXPLANATION = ("The scan could not be completed because the website's protection (a firewall, bot protection or access control) refused the scanner. "
               "No compliance score or report was made, because nothing about the site could be assessed. The scanner does not try to get round this protection.")

#: Visible text under this length counts as "a page that is only the challenge".
SHORT_PAGE_CHARS = 2500


@dataclass(frozen=True)
class BlockVerdict:
    blocked: bool
    kind: str | None = None             # BOT_BLOCKED | ACCESS_BLOCKED
    signal: str = ""                    # what was recognised, in words
    vendor: str | None = None           # cloudflare, akamai, imperva, datadome, perimeterx, aws_waf, sucuri, ...

    def __bool__(self) -> bool:
        return self.blocked


NOT_BLOCKED = BlockVerdict(False)

_TITLE_CHALLENGE = (
    (re.compile(r"^\s*just a moment\.{0,3}\s*$", re.I), "cloudflare", "a Cloudflare challenge page (\"Just a moment...\")"),
    (re.compile(r"attention required!?\s*\|\s*cloudflare", re.I), "cloudflare", "a Cloudflare block page (\"Attention Required\")"),
    (re.compile(r"^\s*access denied\s*$", re.I), None, "an \"Access Denied\" page"),
    (re.compile(r"^\s*(403 forbidden|forbidden|error 403|403 error)\b", re.I), None, "a \"403 Forbidden\" page"),
    (re.compile(r"(are you a (robot|human)|verify you are (a )?human|security check|human verification|bot (verification|check)|checking your browser|one more step)", re.I), None, "a bot-verification page"),
    (re.compile(r"(pardon our interruption|request blocked|you have been blocked|blocked by (security|firewall))", re.I), None, "a \"you have been blocked\" page"),
    (re.compile(r"^\s*captcha\b|\bcaptcha\s*$", re.I), None, "a CAPTCHA page"),
)

#: (vendor, pattern, what it says). Only meaningful on a short page (see SHORT_PAGE_CHARS).
_BODY_CHALLENGE = (
    ("cloudflare", re.compile(r"(cf-browser-verification|challenge-platform|cf-chl-|enable javascript and cookies to continue|checking (if the site connection is secure|your browser before accessing)|performing security verification|ray id:)", re.I), "a Cloudflare challenge"),
    ("cloudflare", re.compile(r"cf-turnstile|challenges\.cloudflare\.com", re.I), "a Cloudflare Turnstile CAPTCHA"),
    ("akamai", re.compile(r"(reference #\s*[0-9a-f.]+|errors\.edgesuite\.net|you don't have permission to access .{0,80} on this server)", re.I), "an Akamai access-denied page"),
    ("imperva", re.compile(r"(incapsula incident id|request unsuccessful\.? incapsula|_incapsula_resource)", re.I), "an Imperva (Incapsula) block"),
    ("datadome", re.compile(r"(datadome|captcha-delivery\.com|geo\.captcha-delivery)", re.I), "a DataDome challenge"),
    ("perimeterx", re.compile(r"(px-captcha|press (&|and) hold|perimeterx|_pxhd|human security)", re.I), "a PerimeterX (HUMAN) challenge"),
    ("sucuri", re.compile(r"sucuri website firewall|access denied - sucuri", re.I), "a Sucuri firewall block"),
    ("aws_waf", re.compile(r"(awswaf|aws waf|x-amzn-waf|request blocked\.? we can't connect to the server)", re.I), "an AWS WAF block"),
    (None, re.compile(r"(g-recaptcha|h-captcha|hcaptcha|recaptcha/api|please (complete|solve) the captcha|prove you are (not a robot|human)|verify that you are (a )?human|i'm not a robot|unusual traffic|automated (access|queries|requests|traffic)|bot detected|suspicious activity)", re.I), "a CAPTCHA or bot-verification page"),
)

_HEADER_SIGNALS = (
    ("cf-mitigated", re.compile(r"challenge|block", re.I), "cloudflare", "a Cloudflare mitigation header"),
    ("x-datadome", re.compile(r".+"), "datadome", "a DataDome header"),
    ("x-datadome-response", re.compile(r".+"), "datadome", "a DataDome header"),
    ("x-amzn-waf-action", re.compile(r"(captcha|challenge|block)", re.I), "aws_waf", "an AWS WAF action header"),
    ("x-sucuri-block", re.compile(r".+"), "sucuri", "a Sucuri block header"),
    ("x-iinfo", re.compile(r".+"), "imperva", "an Imperva header"),
    ("x-px-block", re.compile(r".+"), "perimeterx", "a PerimeterX header"),
)
_ACCESS_STATUSES = {401: "401 Unauthorized", 403: "403 Forbidden", 429: "429 Too Many Requests", 451: "451 Unavailable For Legal Reasons"}
_PROTECTION_SERVERS = {"cloudflare": "cloudflare", "akamaighost": "akamai", "akamai": "akamai", "sucuri/cloudproxy": "sucuri", "imperva": "imperva", "incapsula": "imperva"}


def _lower(headers: dict | None) -> dict[str, str]:
    return {str(k).lower(): str(v) for k, v in (headers or {}).items()}


def _vendor_from_server(h: dict[str, str]) -> str | None:
    server = h.get("server", "").lower()
    return next((v for k, v in _PROTECTION_SERVERS.items() if k in server), None) or ("cloudflare" if "cf-ray" in h else None)


def detect_block(status: int | None, headers: dict | None = None, title: str | None = None, text: str | None = None) -> BlockVerdict:
    """The verdict for one response: its HTTP status, headers, page title and visible text (any may be missing)."""
    h = _lower(headers)
    title = (title or "").strip()
    body = text or ""
    short = len(body.strip()) <= SHORT_PAGE_CHARS

    for header, pattern, vendor, what in _HEADER_SIGNALS:
        if header in h and pattern.search(h[header]):
            return BlockVerdict(True, BOT_BLOCKED, f"{what} on the response" + (f" (HTTP {status})" if status else ""), vendor)

    for pattern, vendor, what in _TITLE_CHALLENGE:
        if title and pattern.search(title) and (short or (status or 0) >= 400):
            return BlockVerdict(True, BOT_BLOCKED, what, vendor or _vendor_from_server(h))

    if short or (status or 0) in _ACCESS_STATUSES:
        for vendor, pattern, what in _BODY_CHALLENGE:
            if pattern.search(body) and (short or (status or 0) >= 400):
                if what == "a CAPTCHA or bot-verification page" and not (short and ((status or 0) >= 400 or len(body.strip()) < 1200)):
                    continue                         # a CAPTCHA widget on a real, longer page is just a form protected by one
                return BlockVerdict(True, BOT_BLOCKED, what, vendor or _vendor_from_server(h))

    if status in _ACCESS_STATUSES:
        vendor = _vendor_from_server(h)
        via = f" from a {vendor.title()}-protected site" if vendor else ""
        return BlockVerdict(True, ACCESS_BLOCKED, f"the site answered HTTP {_ACCESS_STATUSES[status]}{via} instead of a page", vendor)

    if status == 503 and _vendor_from_server(h) == "cloudflare" and short and re.search(r"(just a moment|checking your browser|ddos protection)", f"{title} {body}", re.I):
        return BlockVerdict(True, BOT_BLOCKED, "a Cloudflare challenge served with HTTP 503", "cloudflare")
    return NOT_BLOCKED


_CONNECTION_CUT = ("err_connection_reset", "err_connection_closed", "err_empty_response", "err_http2_protocol_error", "closed the connection to the scanner", "connection reset by peer", "remote end closed connection")


def detect_block_from_error(error: BaseException | str | None) -> BlockVerdict:
    """A block recognised from a failure message alone: used for a scan that has already failed (the five-page scanner stores its reason as text), and for a browser that was cut off."""
    text = str(error or "")
    low = text.lower()
    if "refusing the scanner" in low or "bot protection" in low or "access denied" in low and "http" in low:
        return BlockVerdict(True, BOT_BLOCKED if "bot protection" in low else ACCESS_BLOCKED, "the site refused the scanner (access denied)", None)
    m = re.search(r"\bHTTP (401|403|429|451)\b", text)
    if m:
        return BlockVerdict(True, ACCESS_BLOCKED, f"the site answered HTTP {m.group(1)} instead of a page", None)
    if any(k in low for k in _CONNECTION_CUT):
        return BlockVerdict(True, BOT_BLOCKED, "the site cut the connection, as bot protection does to automated visitors", None)
    return NOT_BLOCKED


def is_block_message(error: str | None) -> bool:
    """True for a stored scan error that says the site blocked the scanner (our own BOT_BLOCKED / ACCESS_BLOCKED prefix, or the five-page scanner's wording)."""
    text = (error or "").lstrip()
    return text.startswith(("BOT_BLOCKED", "ACCESS_BLOCKED")) or bool(detect_block_from_error(text))
