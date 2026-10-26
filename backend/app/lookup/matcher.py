"""Pure matching logic against the cookie lookup table, decoupled from the DB so
rules/consent_rules.py's classify_cookie() stays a pure, unit-testable function —
repository.py does the one query to load entries; this module just matches."""

from dataclasses import dataclass


@dataclass(frozen=True)
class LookupEntry:
    name_pattern: str
    is_prefix_pattern: bool
    category: str
    vendor: str | None = None
    domain_pattern: str | None = None


# A wildcard shorter than this is a letter or two (`_s`, `CT`, `tp`) that any unrelated cookie starts with: `_session_id` was labelled analytics, `CTX` marketing.
MIN_PREFIX_LENGTH = 4

# Names that many unrelated services use. An entry for one is only evidence about a cookie that sits on the entry's own domain.
_GENERIC_NAMES = frozenset({"uid", "token", "id", "sid", "session", "user", "visitor", "guest", "lang", "cart", "test", "tmp", "temp", "data", "key", "auth"})


def _is_generic(name: str) -> bool:
    return len(name) <= 3 or name.lower() in _GENERIC_NAMES


def _domain_of(pattern: str | None) -> str | None:
    """"facebook.com (3rd party)" -> "facebook.com"; "*.example.com" -> "example.com"; None when there is nothing usable."""
    if not pattern or not pattern.strip():
        return None
    token = pattern.strip().split()[0].lstrip("*.").lstrip(".").lower()
    return token or None


def _on_domain(entry: LookupEntry, cookie_domain: str | None) -> bool:
    entry_domain = _domain_of(entry.domain_pattern)
    host = (cookie_domain or "").strip().lstrip(".").lower()
    if not entry_domain or not host:
        return False
    return host == entry_domain or host.endswith("." + entry_domain) or entry_domain.endswith("." + host)


def match_cookie_lookup(name: str, entries: list[LookupEntry], domain: str | None = None) -> LookupEntry | None:
    """An exact name first, then the longest wildcard. A generic exact name (`uid`, `token`, `ID`) counts only for a cookie on that entry's own domain, and a
    wildcard shorter than MIN_PREFIX_LENGTH never counts."""
    for entry in entries:
        if not entry.is_prefix_pattern and entry.name_pattern == name:
            if _is_generic(name) and not _on_domain(entry, domain):
                continue
            return entry

    prefix_candidates = [e for e in entries if e.is_prefix_pattern and len(e.name_pattern) >= MIN_PREFIX_LENGTH and name.startswith(e.name_pattern)]
    if not prefix_candidates:
        return None
    return max(prefix_candidates, key=lambda e: len(e.name_pattern))
