"""Raw cookie detection. Vendor/category classification happens later in
rules/consent_rules.py — this module only records what's present."""

from datetime import UTC, datetime
from typing import Any

from app.scanner._domain import registered_domain as _registered_domain
from app.scanner.schemas import CookieRecord


def detect_cookies(raw_cookies: list[dict[str, Any]], site_domain: str) -> list[CookieRecord]:
    """`raw_cookies` is Playwright's `BrowserContext.cookies()` output:
    [{"name", "value", "domain", "path", "expires", ...}, ...]. Values are never stored.
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
        ))
    return records
