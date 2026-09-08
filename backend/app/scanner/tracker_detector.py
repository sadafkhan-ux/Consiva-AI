"""Raw third-party script/request detection. Vendor/category classification happens
later in rules/consent_rules.py."""

from app.scanner._domain import registered_domain as _registered_domain
from app.scanner.page_parser import ParsedScript
from app.scanner.schemas import TrackerRecord


def detect_trackers(
    *,
    site_domain: str,
    scripts_by_page: dict[str, list[ParsedScript]],
    network_requests_by_page: dict[str, list[tuple[str, str]]],
) -> list[TrackerRecord]:
    """`scripts_by_page` and `network_requests_by_page` are keyed by page local_id.
    Network requests catch trackers that fire via fetch/beacon/img rather than a
    <script src> tag (common for pixels); `network_requests_by_page` values are
    (url, resource_type) pairs -- resource_type is Playwright's own classification
    ("script"/"xhr"/"fetch"/"image"/"font"/"stylesheet"/"media"/...), recorded so
    rules/consent_rules.py can tell a real tracking script/beacon apart from a plain
    static asset with no behavioral-tracking signal, without discarding either --
    every cross-origin request is still recorded here exactly as before."""
    site_registered = _registered_domain(site_domain)
    seen_srcs: set[str] = set()
    records: list[TrackerRecord] = []
    counter = 0

    def _maybe_add(page_local_id: str, src: str, resource_type: str | None) -> None:
        nonlocal counter
        if not src:
            return
        # Dedup on the URL WITHOUT its query string: a GA/GTM-style beacon appends
        # changing cache-busting/session params on every page load, and raw-string
        # dedup recorded that as N distinct "trackers" for N page visits -- inflating
        # evidence lists, DB rows, and the LLM prompt with copies of one real tracker.
        # The stored script_src keeps the FIRST occurrence's full URL (query included),
        # so evidence stays a real, complete observed request, not a synthesized one.
        dedup_key = src.split("?", 1)[0]
        if dedup_key in seen_srcs:
            return
        if _registered_domain(src) == site_registered:
            return  # first-party, not a tracker
        seen_srcs.add(dedup_key)
        records.append(TrackerRecord(
            local_id=f"tracker-{counter}", page_local_id=page_local_id, script_src=src, resource_type=resource_type,
        ))
        counter += 1

    for page_local_id, scripts in scripts_by_page.items():
        for script in scripts:
            if script.src:
                _maybe_add(page_local_id, script.src, "script")

    for page_local_id, requests in network_requests_by_page.items():
        for url, resource_type in requests:
            _maybe_add(page_local_id, url, resource_type)

    return records
