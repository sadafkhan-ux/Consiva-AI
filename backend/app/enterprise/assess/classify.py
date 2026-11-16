"""Cookie classification for the Enterprise assessors: the Open Cookie Database and the vendor signatures the 5-page scanner already uses, called as they are."""

from __future__ import annotations

from collections.abc import Callable

from app.rules.consent_rules import classify_cookie
from app.scanner.schemas import CookieRecord


def build_cookie_classifier(lookup_entries: list) -> Callable[[str, str | None], str | None]:
    """name, domain -> "analytics" | "marketing" | "functional" | ... or None when the cookie is not in the catalogue."""

    def classify(name: str, domain: str | None) -> str | None:
        return classify_cookie(CookieRecord(local_id="c", name=name, domain=domain), lookup_entries).category

    return classify
