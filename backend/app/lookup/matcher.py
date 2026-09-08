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


def match_cookie_lookup(name: str, entries: list[LookupEntry]) -> LookupEntry | None:
    for entry in entries:
        if not entry.is_prefix_pattern and entry.name_pattern == name:
            return entry

    prefix_candidates = [e for e in entries if e.is_prefix_pattern and name.startswith(e.name_pattern)]
    if not prefix_candidates:
        return None
    return max(prefix_candidates, key=lambda e: len(e.name_pattern))
