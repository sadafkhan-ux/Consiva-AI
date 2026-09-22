"""Fetching an approved regulatory source over HTTP.

SSRF IS THE WHOLE RISK HERE
--------------------------
This is the one place in the platform that fetches a URL an operator typed. Agent 1
solved that already -- `app/scanner/url_safety.assert_safe_url` resolves the hostname
and refuses private, loopback, link-local and multicast addresses, and it is re-checked
on every redirect hop rather than only on the URL as submitted. Reusing it means a
regulatory source cannot be pointed at `http://169.254.169.254/` to read cloud
credentials, or at an internal admin page.

Redirects are followed manually, one hop at a time, so each new location goes through
the same check. `follow_redirects=False` on the client is deliberate: letting httpx
follow them internally would resolve and connect to a host this never validated.

WHAT COUNTS AS A FAILURE
------------------------
Everything that is not a 2xx with usable content. The distinction the spec cares about
is not "did we get bytes back" but "do we have something we can compare against a
baseline" -- a 200 that returns a login wall or an empty body is a failed collection,
not an unchanged source, because reporting it as unchanged is the exact thing §15
forbids.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass

import httpx

from app.agents.regwatch.errors import ContentUnusableError, SourceUnreachableError
from app.scanner.url_safety import assert_safe_url

logger = logging.getLogger(__name__)

# Regulatory pages are documents, not applications. A generous ceiling that still
# refuses to pull a DVD image into a text column.
MAX_BYTES = 8 * 1024 * 1024
MAX_REDIRECTS = 5
TIMEOUT_SECONDS = 30.0

# Below this, a "successful" response is not a document. A regulator's page that
# suddenly returns 40 bytes has broken, and adopting that as the new content would
# manufacture an enormous false change and then absorb the real one.
#
# A default, not a law: it is overridable per source via `config.min_usable_chars`,
# because a short notice page or a sparse RSS item is a real document that this would
# otherwise refuse forever. Configuration rather than a constant, the same way Agent
# 3 handles retention rules. Whichever way it is set, falling below it FAILS loudly
# with a reason rather than silently reporting the source as unchanged.
MIN_USABLE_CHARS = 200

_SCRIPT_STYLE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_TAG = re.compile(r"<[^>]+>")
_WHITESPACE = re.compile(r"[ \t\r\f\v]+")
_BLANK_LINES = re.compile(r"\n{3,}")

# Things that come back with HTTP 200 and are not the document anyone asked for.
_WALL_MARKERS = (
    "enable javascript", "checking your browser", "access denied",
    "are you a robot", "captcha", "sign in to continue", "please log in",
)


@dataclass(frozen=True)
class Fetched:
    """One successful collection. `content_hash` is what change detection compares;
    `text` is kept so a reviewer can read what was actually retrieved rather than
    trusting a diff summary about it."""

    url: str
    http_status: int
    text: str
    content_hash: str
    byte_count: int
    # The undecoded-then-decoded body, before `normalize` flattened it. Carried so a
    # connector that reads the document's STRUCTURE -- a feed, say -- still can: the
    # HTML stripper removes the very tags it would need. In memory only; what gets
    # persisted is `text`.
    raw: str = ""


def normalize(raw: str) -> str:
    """Strip a page down to the text a change should be measured against.

    Scripts and styles go first: a site that rotates a cache-busting token in a
    <script> tag every hour would otherwise report a regulatory change every hour, and
    an agent that cries wolf hourly is one nobody reads. Same reasoning for collapsing
    runs of whitespace -- a reflow is not an amendment.

    What is deliberately NOT stripped: anything inside the visible text. Removing
    dates or numbers to reduce noise would also remove the substance of most
    regulatory changes.
    """
    text = _SCRIPT_STYLE.sub(" ", raw)
    text = _TAG.sub(" ", text)
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&")
                .replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"'))
    text = _WHITESPACE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return _BLANK_LINES.sub("\n\n", text).strip()


def hash_content(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _looks_like_a_wall(text: str) -> str | None:
    lowered = text[:2000].lower()
    for marker in _WALL_MARKERS:
        if marker in lowered:
            return marker
    return None


async def fetch(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    min_usable_chars: int = MIN_USABLE_CHARS,
) -> Fetched:
    """Retrieve one approved source. Raises rather than returning a partial result.

    Every raise here becomes a `failed` collection row with a code, which is what
    keeps a source that could not be read visibly different from one that had not
    changed.
    """
    request_headers = {
        # Identify honestly. A regulator blocking us is a problem to solve by asking,
        # not by pretending to be a browser.
        "User-Agent": "Consiva-RegulatoryWatch/1.0 (+compliance monitoring)",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,text/plain;q=0.8",
        **(headers or {}),
    }

    current = url
    async with httpx.AsyncClient(
        follow_redirects=False, timeout=TIMEOUT_SECONDS, http2=False
    ) as client:
        for hop in range(MAX_REDIRECTS + 1):
            # Re-checked on EVERY hop. A public URL that redirects to 127.0.0.1 is the
            # standard way around a check applied only to what was submitted.
            try:
                await assert_safe_url(current)
            except Exception as exc:
                raise SourceUnreachableError(
                    f"refusing to fetch {current}: {exc}"
                ) from exc

            try:
                response = await client.get(current, headers=request_headers)
            except httpx.HTTPError as exc:
                raise SourceUnreachableError(
                    f"could not reach {current}: {type(exc).__name__}"
                ) from exc

            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    raise SourceUnreachableError(
                        f"{current} returned {response.status_code} with no Location header")
                current = str(response.url.join(location))
                continue

            if response.status_code >= 400:
                raise SourceUnreachableError(
                    f"{current} returned HTTP {response.status_code}")

            raw = response.content[:MAX_BYTES]
            decoded = raw.decode(response.encoding or "utf-8", errors="replace")
            text = normalize(decoded)

            if len(text) < min_usable_chars:
                raise ContentUnusableError(
                    f"{current} returned {len(text)} characters of text, below this "
                    f"source's minimum of {min_usable_chars}; treating as a failed "
                    "collection rather than as a change. If this source really is this "
                    "short, set config.min_usable_chars on it."
                )
            wall = _looks_like_a_wall(text)
            if wall:
                raise ContentUnusableError(
                    f"{current} returned a page containing {wall!r} rather than the "
                    "document -- likely a login wall or bot check, not a change"
                )

            return Fetched(
                url=current,
                http_status=response.status_code,
                text=text,
                content_hash=hash_content(text),
                byte_count=len(raw),
                raw=decoded,
            )

    raise SourceUnreachableError(f"{url} exceeded {MAX_REDIRECTS} redirects")
