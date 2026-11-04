"""The URL inventory: one record per distinct URL a scan discovered, and what became of it.

Pure bookkeeping, no browser and no database, so the whole lifecycle is testable offline. The crawler feeds it events (discovered, started,
finished, excluded) and reads the records back at the end. Keyed on the same normalized form the crawler de-duplicates with, so a page is
one record however many links, sitemaps or spellings named it.

Discovered does not mean scanned. A URL stays PENDING until the crawl starts it, and one the crawl never reached stays PENDING with the reason.
"""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from app.scanner.url_normalize import normalize_url

PENDING = "PENDING"
IN_PROGRESS = "IN_PROGRESS"
COMPLETED = "COMPLETED"
FAILED = "FAILED"
PARTIALLY_SCANNED = "PARTIALLY_SCANNED"
EXCLUDED = "EXCLUDED"
STATUSES = (PENDING, IN_PROGRESS, COMPLETED, FAILED, PARTIALLY_SCANNED, EXCLUDED)

#: The only moves a record may make. COMPLETED, PARTIALLY_SCANNED, FAILED and EXCLUDED are final.
TRANSITIONS: dict[str, frozenset[str]] = {
    PENDING: frozenset({IN_PROGRESS, EXCLUDED}),
    IN_PROGRESS: frozenset({COMPLETED, PARTIALLY_SCANNED, FAILED, EXCLUDED}),   # EXCLUDED after load: it redirected out of scope or to a page already scanned
    COMPLETED: frozenset(),
    PARTIALLY_SCANNED: frozenset(),
    FAILED: frozenset(),
    EXCLUDED: frozenset(),
}

#: A sitemap names a page without saying how it is linked, so it counts as one step from the entry address.
SITEMAP_DEPTH = 1
#: Most URLs a scan without crawl controls (every public web scan) records, so a very large sitemap cannot grow the inventory without bound.
UNCONTROLLED_CAP = 5_000


class InvalidTransition(ValueError):
    """A status change the lifecycle does not allow (for example COMPLETED back to PENDING)."""


class UrlInventoryRecord(BaseModel):
    """One discovered URL. Mirrors the scan_url_inventory table."""

    domain: str
    original_url: str
    normalized_url: str
    final_url: str | None = None
    page_title: str | None = None
    http_status: int | None = None
    content_type: str | None = None
    discovery_source: str
    discovery_sources: list[str] = Field(default_factory=list)
    crawl_depth: int = Field(ge=0)
    scan_status: str = PENDING
    status_reason: str | None = None
    failure_reason: str | None = None
    discovered_at: datetime
    scan_started_at: datetime | None = None
    scan_completed_at: datetime | None = None
    last_updated_at: datetime


def _now() -> datetime:
    return datetime.now(UTC)


class InventoryTracker:
    """The inventory of one scan, built while it crawls.

    `max_urls` is the most distinct URLs the inventory will hold (None = no cap). A URL past it is counted in `over_limit` and not recorded, so
    the scan can say how much was left undiscovered rather than lose it silently.
    """

    def __init__(self, max_urls: int | None = None, clock=_now) -> None:
        self._records: dict[str, UrlInventoryRecord] = {}
        self._max_urls = max_urls
        self._clock = clock
        self.over_limit = 0

    # ── reading ──────────────────────────────────────────────────────────────────────
    def __len__(self) -> int:
        return len(self._records)

    def get(self, url: str) -> UrlInventoryRecord | None:
        return self._records.get(normalize_url(url))

    def depth_of(self, url: str) -> int | None:
        rec = self.get(url)
        return None if rec is None else rec.crawl_depth

    def records(self) -> list[UrlInventoryRecord]:
        return list(self._records.values())

    def counts(self) -> dict[str, int]:
        out = {s: 0 for s in STATUSES}
        for r in self._records.values():
            out[r.scan_status] += 1
        return out

    # ── events ───────────────────────────────────────────────────────────────────────
    def discover(self, url: str, source: str, depth: int) -> UrlInventoryRecord | None:
        """Records `url` as found by `source` at `depth`. Returns the new record, or None when it was already known (the source is added to it) or the
        URL limit has been reached (counted in `over_limit`)."""
        key = normalize_url(url)
        known = self._records.get(key)
        now = self._clock()
        if known is not None:
            if source not in known.discovery_sources:
                known.discovery_sources.append(source)
                known.last_updated_at = now
            return None
        if self._max_urls is not None and len(self._records) >= self._max_urls:
            self.over_limit += 1
            return None
        rec = UrlInventoryRecord(
            domain=(urlparse(url).netloc or "").lower(), original_url=url, normalized_url=key,
            discovery_source=source, discovery_sources=[source], crawl_depth=depth,
            discovered_at=now, last_updated_at=now,
        )
        self._records[key] = rec
        return rec

    def _move(self, url: str, to: str) -> UrlInventoryRecord | None:
        rec = self.get(url)
        if rec is None:
            return None
        if to not in TRANSITIONS[rec.scan_status]:
            raise InvalidTransition(f"{rec.scan_status} -> {to} is not allowed for {rec.normalized_url}")
        rec.scan_status = to
        rec.last_updated_at = self._clock()
        return rec

    def exclude(self, url: str, reason: str, *, final_url: str | None = None, http_status: int | None = None) -> None:
        """The URL will not be scanned, and `reason` says why (robots.txt, depth limit, ...). A page already loaded can be excluded too (it redirected
        out of scope), in which case where it landed is kept."""
        rec = self._move(url, EXCLUDED)
        if rec is not None:
            rec.status_reason = reason
            if final_url:
                rec.final_url, rec.http_status = final_url, http_status
                rec.scan_completed_at = rec.last_updated_at

    def start(self, url: str) -> None:
        rec = self._move(url, IN_PROGRESS)
        if rec is not None:
            rec.scan_started_at = rec.last_updated_at

    def fail(self, url: str, reason: str, http_status: int | None = None) -> None:
        rec = self._move(url, FAILED)
        if rec is not None:
            rec.failure_reason = reason or "the page could not be loaded"
            rec.http_status = http_status
            rec.scan_completed_at = rec.last_updated_at

    def finish(self, url: str, *, final_url: str | None, title: str | None, http_status: int | None, content_type: str | None, incomplete_reason: str | None = None) -> None:
        """The page loaded. It is COMPLETED, or PARTIALLY_SCANNED when `incomplete_reason` says what could not be extracted."""
        rec = self._move(url, PARTIALLY_SCANNED if incomplete_reason else COMPLETED)
        if rec is None:
            return
        rec.final_url, rec.page_title, rec.http_status, rec.content_type = final_url, title, http_status, content_type
        rec.status_reason = incomplete_reason
        rec.scan_completed_at = rec.last_updated_at

    def leave_pending(self, url_reasons: dict[str, str]) -> None:
        """At the end of the crawl: a URL that was never started stays PENDING and records why it was not reached."""
        for url, reason in url_reasons.items():
            rec = self.get(url)
            if rec is not None and rec.scan_status == PENDING:
                rec.status_reason = reason
                rec.last_updated_at = self._clock()

    def pending_urls(self) -> list[str]:
        return [r.original_url for r in self._records.values() if r.scan_status == PENDING]


def incomplete_extraction(*, text_length: int, content_type: str | None) -> str | None:
    """Why a page that loaded still counts as only partly scanned, or None when its content was extracted. HTTP success is not extraction success."""
    if content_type and "html" not in content_type.lower() and "xml" not in content_type.lower():
        return f"not an HTML page ({content_type.split(';')[0].strip()}); no page text was extracted"
    if text_length <= 0:
        return "the page loaded but no text could be extracted from it"
    return None
