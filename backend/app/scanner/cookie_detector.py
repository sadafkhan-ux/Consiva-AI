"""Raw cookie detection. Vendor/category classification happens later in
rules/consent_rules.py — this module only records what's present."""

from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from app.scanner._domain import registered_domain as _registered_domain
from app.scanner.schemas import CookieObservation, CookieRecord


def detect_cookies(raw_cookies: list[dict[str, Any]], site_domain: str) -> list[CookieRecord]:
    """`raw_cookies` is Playwright's `BrowserContext.cookies()` output:
    [{"name", "value", "domain", "path", "expires", "secure", "httpOnly", "sameSite"},
    ...]. Values are never stored.
    """
    site_registered = _registered_domain(site_domain)
    records: list[CookieRecord] = []
    for i, cookie in enumerate(raw_cookies):
        domain = (cookie.get("domain") or "").lstrip(".")
        expires = cookie.get("expires")
        expiry_iso = (
            datetime.fromtimestamp(expires, tz=UTC).isoformat()
            if isinstance(expires, (int, float)) and expires > 0
            else None
        )
        records.append(CookieRecord(
            local_id=f"cookie-{i}",
            name=cookie["name"],
            domain=domain or None,
            path=cookie.get("path"),
            expiry=expiry_iso,
            is_first_party=(_registered_domain(domain) == site_registered) if domain else None,
            secure=cookie.get("secure") if isinstance(cookie.get("secure"), bool) else None,
            http_only=cookie.get("httpOnly") if isinstance(cookie.get("httpOnly"), bool) else None,
            same_site=cookie.get("sameSite") or None,
        ))
    return records


def cookie_key(name: str, domain: str | None, path: str | None = None) -> tuple[str, str, str]:
    """The identity used to merge one cookie across reads: name, domain (leading dot
    and case dropped, matching detect_cookies()' own normalisation), and path.

    Path is part of the identity, not metadata layered on top of it -- the browser
    itself can hold `session` at `/` and `session` at `/admin` as two separate
    cookies, and collapsing them into one record would silently merge distinct
    cookies. A missing/empty path means the browser's default path; callers that
    don't have one (a jar read always does) should pass None and let this normalise
    it to "/" rather than guessing a directory-scoped default themselves.
    """
    return name, (domain or "").lstrip(".").lower(), path or "/"


def _default_path(request_url: str) -> str:
    """RFC 6265 §5.1.4's default-path algorithm: the directory of the request path, or
    "/" if that path is empty or has no more than one "/". Used only when a Set-Cookie
    header omits Path -- needed so the header-derived identity matches the one the
    browser itself assigns (and thus the one the jar read reports), or header
    attribution silently stops matching for every cookie that relies on the default.
    """
    uri_path = urlparse(request_url).path
    if not uri_path or not uri_path.startswith("/"):
        return "/"
    last_slash = uri_path.rfind("/")
    if last_slash <= 0:
        return "/"
    return uri_path[:last_slash]


def parse_set_cookie_headers(header_values: list[str], response_url: str) -> list[tuple[str, str, str]]:
    """(name, domain, path) -- see cookie_key() -- for every cookie a response's
    Set-Cookie headers set.

    Chromium can hand several Set-Cookie headers back joined by newlines in one value,
    so each value is split first. The domain is the Domain attribute when present, and
    otherwise the response's host -- which is what the browser itself stores for a
    host-only cookie. The path is likewise the Path attribute when present, and
    otherwise RFC 6265's default-path of the response's own URL.
    """
    host = (urlparse(response_url).hostname or "").lower()
    found: list[tuple[str, str, str]] = []
    for value in header_values:
        for line in value.split("\n"):
            parts = [p.strip() for p in line.split(";")]
            if not parts or "=" not in parts[0]:
                continue
            name = parts[0].split("=", 1)[0].strip()
            if not name:
                continue
            domain = host
            path = None
            for attr in parts[1:]:
                key, _, attr_value = attr.partition("=")
                attr_key = key.strip().lower()
                if attr_key == "domain" and attr_value.strip():
                    domain = attr_value.strip()
                elif attr_key == "path" and attr_value.strip():
                    path = attr_value.strip()
            found.append(cookie_key(name, domain, path or _default_path(response_url)))
    return found


def attribute_new_cookies(
    raw_cookies: list[dict[str, Any]],
    already_seen: set[tuple[str, str, str]],
    batch_page_urls: list[str],
    header_events: list[dict[str, Any]],
    observed_at: str,
    consent_state: str,
) -> dict[tuple[str, str, str], CookieObservation]:
    """Attributes every cookie in `raw_cookies` (a jar read taken just after a batch of
    pages loaded) that is not in `already_seen` to the page that set it.

    `header_events` are this batch's Set-Cookie observations:
    {"name", "domain", "path", "page_url", "request_url", "observed_at"} with
    name/domain/path as cookie_key() returns them. A header is direct evidence, so it
    wins; the earliest one is taken when several pages set the same cookie. Without
    one, the page is known exactly only when the batch loaded a single page --
    otherwise it is left unset and the batch's pages are listed as candidates, never
    guessed.
    """
    by_key: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for event in header_events:
        by_key.setdefault((event["name"], event["domain"], event["path"]), []).append(event)

    attributed: dict[tuple[str, str, str], CookieObservation] = {}
    for cookie in raw_cookies:
        key = cookie_key(cookie["name"], cookie.get("domain"), cookie.get("path"))
        if key in already_seen or key in attributed:
            continue
        events = sorted(by_key.get(key, []), key=lambda e: e["observed_at"])
        if events:
            first = events[0]
            attributed[key] = CookieObservation(
                consent_state=consent_state, page_url=first["page_url"],
                observed_at=first["observed_at"], method="set_cookie_header",
                source_request_url=first["request_url"],
            )
        elif len(batch_page_urls) == 1:
            attributed[key] = CookieObservation(
                consent_state=consent_state, page_url=batch_page_urls[0],
                observed_at=observed_at, method="single_page_load",
            )
        else:
            attributed[key] = CookieObservation(
                consent_state=consent_state, page_url=None, observed_at=observed_at,
                method="concurrent_batch", candidate_page_urls=list(batch_page_urls),
            )
    return attributed
