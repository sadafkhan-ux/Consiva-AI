"""What the ten-check engine reads of a scan, derived from the scan's stored evidence and the website-scan stage record.

Three small derivations, ported from the browser report builders they replace (frontend report/scanPages.ts, reportVisual.ts and buildReport.ts) and held to their results by
tests/fixtures/ten_inputs_golden.json (tests/test_ten_inputs_parity.py):

  build_scan_pages   how many pages the scan scanned, found, failed on and skipped, and why it stopped
  build_visual       pages scanned and found, and the consent comparison (were Accept and Reject tested, and how many known analytics or marketing requests in each state)
  untested_consent_states   the consent states in which nothing was clicked
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

LOADED_VIA = frozenset({"seed", "link", "sitemap"})

STOP_REASON = {
    "queue_exhausted": "The scan stopped because no more pages of the entered domain were found to scan.",
    "page_limit": "The scan stopped because it reached the page limit.",
    "deadline": "The scan stopped at its time limit, so pages that were still queued were not scanned.",
    "slow_site": "The scan stopped early because the pages it tried kept timing out, so the site was too slow to scan more of it.",
    "attempt_limit": "The scan stopped because too many of the pages it tried could not be scanned (they failed, timed out, or led away from the entered domain).",
    "cancelled": "The scan was cancelled before every queued page was scanned.",
}

STATES = ("pre_consent", "post_accept", "post_reject")


def _page_status(p: dict) -> tuple[str, bool]:
    via = p.get("discovered_via")
    if via is not None and via in LOADED_VIA:
        return (f"Scanned (HTTP {p['http_status']})" if p.get("http_status") else "Scanned", True)
    if via == "timeout":
        return ("Not scanned: timed out", False)
    if via == "failed":
        return ("Not scanned: failed to load", False)
    if via == "robots_disallowed":
        return ("Not scanned: blocked by robots.txt", False)
    return ("Recorded; load result not stored for this scan", False)


def untested_consent_states(meta: dict | None) -> list[str]:
    """The consent states in which nothing was clicked. With no scan record at all, nothing is reported as untested."""
    if meta is None:
        return []
    out = []
    if meta.get("accept_interaction") != "clicked":
        out.append("post_accept")
    if meta.get("reject_interaction") != "clicked":
        out.append("post_reject")
    return out


def build_scan_pages(pages: list[dict] | None, meta: dict | None) -> dict[str, Any] | None:
    """Page accounting for the engine: scanned count, pages found, pages that did not load (and why), pages found but not scanned, why the crawl stopped, and the addresses scanned."""
    if pages is None and meta is None:
        return None
    pages = pages or []
    records = [(p, *_page_status(p)) for p in pages]
    loaded = sum(1 for _p, _t, scanned in records if scanned)
    scanned_urls = [p["url"] for p, _t, scanned in records if scanned]
    failed = [{"url": p["url"], "reason": text.replace("Not scanned: ", "")} for p, text, scanned in records if not scanned and text.startswith("Not scanned")]
    m = meta or {}
    scanned_count = m["pages_scanned"] if m.get("pages_scanned") is not None else loaded
    found = m.get("pages_discovered")
    skipped_by_reason = m.get("skipped_by_reason") or {}
    skipped = [{"count": n} for reason, n in skipped_by_reason.items() if n > 0 and reason != "duplicate_url"]
    unattempted = m.get("pages_not_attempted_budget_exhausted") or 0
    if unattempted > 0 and not (skipped_by_reason.get("page_limit", 0) > 0):
        skipped.append({"count": unattempted})
    stop = m.get("stop_reason")
    return {
        "scanned_count": scanned_count,
        "discovered": None if found is None else max(found, scanned_count),
        "failed": failed,
        "skipped": skipped,
        "stop_reason": STOP_REASON.get(stop) if stop else None,
        "rows": [{"url": u, "scanned": True} for u in scanned_urls],
    }


def build_visual(evidence: dict | None, meta: dict | None) -> dict[str, Any] | None:
    """Pages scanned and found, and the consent comparison the engine reads. None when the evidence was not loaded."""
    if evidence is None:
        return None
    untested = untested_consent_states(meta)
    trackers = evidence.get("trackers") or []
    known_trackers = [t for t in trackers if t.get("category") in ("analytics", "marketing")]
    known = [sum(1 for t in known_trackers if state in (t.get("consent_states") or [])) for state in STATES]
    m = meta or {}
    pages = m["pages_scanned"] if m.get("pages_scanned") is not None else len(evidence.get("pages") or [])
    if meta is not None and "pages_discovered" in meta:
        found = meta["pages_discovered"]
        pages_found: int | None = max(0 if found is None else found, pages)
    else:
        pages_found = None
    return {
        "pages_scanned": pages,
        "pages_found": pages_found,
        "consent": {"accept_tested": "post_accept" not in untested, "reject_tested": "post_reject" not in untested, "known": known},
    }


def host_of(url: str) -> str | None:
    try:
        h = urlsplit(url).hostname
    except ValueError:
        return None
    return h.lower().removeprefix("www.") if h else None
