"""Scan profiles: the budget a scan is allowed, decided by the server and nothing else.

Two profiles, one engine. They differ ONLY in how many pages the pre-consent crawl may fetch
-- neither has a crawl deadline by default, and both keep a fixed whole-scan safety ceiling so a
hung browser cannot hold a worker. Every other protection (SSRF checks, same-site restriction,
robots.txt, per-navigation timeout, bounded concurrency, URL de-duplication) lives in the
crawler and applies to both identically.

    web          public website scans. At most CONSENT_WEB_MAX_PAGES unique pages (default 5,
                 never more than 25). This is the default for anything that does not name a
                 profile -- the console, scheduled monitoring, the plain POST /scans alias --
                 so a missing or unrecognised profile can only ever resolve to the SMALL one.

    application  authorized application scans. No fixed page-count cap when
                 CONSENT_APP_MAX_PAGES is 0, and no crawl deadline when CONSENT_APP_MAX_SECONDS
                 is 0 (the default): the crawl runs until it has visited what it can reach. A
                 positive CONSENT_APP_MAX_SECONDS opts back in to a deadline. Either way the whole
                 scan keeps a fixed 24 hour ceiling, only so a hung browser cannot hold a worker.

WHO DECIDES
-----------
The profile is chosen by the endpoint that was called and the caller's authorization, checked
on the server (see api/v1/routes/consent_agent.py). It is then stored on the scan row and read
back from there by the worker. Nothing in a request body, query string or job payload is
trusted to pick or widen a profile: a client-supplied `max_pages` can only LOWER the page limit
(`effective_max_pages`), never raise it.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.config import Settings, get_settings

WEB = "web"
APPLICATION = "application"
PROFILE_NAMES = (WEB, APPLICATION)

#: Time added on top of the crawl for browser start-up and the two consent-interaction passes
#: (each bounded by its own per-navigation timeouts). Not a page budget -- a fixed allowance.
_PASS_ALLOWANCE_NAV_TIMEOUTS = 2
_STARTUP_ALLOWANCE_SECONDS = 120

#: Whole-scan ceilings for a profile with no crawl deadline. They are not scan budgets: the job
#: lease (jobs/queue.stale_threshold_for) and the outer asyncio timeout both need a finite number,
#: and a scan that is genuinely hung must eventually be aborted so the worker can take other jobs.
#: A healthy crawl is never expected to reach them. The web crawl is bounded to a handful of pages,
#: so its ceiling is 30 minutes (the old derived limit was 330 seconds, which a slow site exceeded);
#: an application scan has no page cap, so its ceiling is 24 hours.
_WEB_HARD_CEILING_SECONDS = 1_800.0

#: The most distinct pages a public (web-profile) scan may navigate to, across the whole scan. Every one is a page of the
#: entered main domain: subdomains and other websites are never crawled. Enforced in the crawler as well as in the settings,
#: so no configuration value can raise it.
MAX_WEB_PAGES = 5
_APP_HARD_CEILING_SECONDS = 86_400.0


@dataclass(frozen=True)
class ScanProfile:
    name: str
    #: Unique pages the pre-consent crawl may fetch. None means "no fixed page-count cap".
    max_pages: int | None
    #: Wall-clock seconds the crawl may keep STARTING new pages. After this it stops and the
    #: scan completes with what it has. None means no crawl deadline (application scans only).
    crawl_deadline_seconds: float | None
    #: Wall-clock seconds for the whole scan, crawl and consent passes together. The scan is
    #: aborted at this point regardless of what it is doing. Always finite, for every profile.
    hard_timeout_seconds: float
    #: An application scan runs against a domain the organisation has proven it controls;
    #: a public web scan follows the existing deployment-wide policy instead.
    requires_verified_domain: bool

    @property
    def page_cap_label(self) -> str:
        return "uncapped" if self.max_pages is None else str(self.max_pages)

    def effective_max_pages(self, requested: int | None) -> int | None:
        """The page limit after a caller's request, which may only tighten it.

        A request is a wish to scan LESS. `None` (no request) leaves the profile's own limit,
        and a request above the profile's cap is ignored rather than honoured -- that is the
        whole of "a caller cannot bypass the web limit by sending a larger page limit".
        """
        if requested is None:
            return self.max_pages
        if self.max_pages is None:
            return requested
        return min(requested, self.max_pages)

    def as_options(self, requested_max_pages: int | None = None) -> dict:
        """What is stored on the scan row, so the worker and the response read the same truth."""
        return {
            "profile": self.name,
            "max_pages": self.effective_max_pages(requested_max_pages),
            "max_seconds": None if self.crawl_deadline_seconds is None else int(self.crawl_deadline_seconds),
        }


def _profile(name: str, settings: Settings, *, max_pages: int | None, crawl_seconds: float | None,
             requires_verified_domain: bool) -> ScanProfile:
    passes = _PASS_ALLOWANCE_NAV_TIMEOUTS * settings.scanner_timeout_seconds
    if crawl_seconds is None:
        hard_timeout = _APP_HARD_CEILING_SECONDS if name == APPLICATION else _WEB_HARD_CEILING_SECONDS
    else:
        hard_timeout = crawl_seconds + passes + _STARTUP_ALLOWANCE_SECONDS
    return ScanProfile(
        name=name, max_pages=max_pages, crawl_deadline_seconds=crawl_seconds,
        hard_timeout_seconds=hard_timeout,
        requires_verified_domain=requires_verified_domain,
    )


def resolve_profile(name: str | None, settings: Settings | None = None) -> ScanProfile:
    """The profile for `name`. Anything but exactly "application" is the web profile.

    Failing SMALL is deliberate: a typo, a null, a value from an older scan row or a tampered
    options blob all land on the profile with the page cap and the deadline, never on the
    one without.
    """
    settings = settings or get_settings()
    if name == APPLICATION:
        cap = settings.consent_app_max_pages or None  # 0 -> no fixed page-count cap
        return _profile(
            APPLICATION, settings, max_pages=cap,
            crawl_seconds=float(settings.consent_app_max_seconds) or None,  # 0 -> no crawl deadline
            requires_verified_domain=True,
        )
    # The web profile is bounded by its PAGE limit, not by time: the old "pages x per-page timeout"
    # deadline stopped the crawl starting new pages after 150 s and aborted the whole scan at 330 s,
    # which a slow site exceeded. Each navigation still has its own per-page timeout.
    return _profile(
        WEB, settings, max_pages=settings.consent_web_max_pages,
        crawl_seconds=None,
        requires_verified_domain=False,
    )


def profile_for_scan_options(options: dict | None, settings: Settings | None = None) -> ScanProfile:
    """The profile a stored scan runs under, read from the options the server wrote.

    A scan row with no options (every scan created before profiles existed, and every console
    or scheduled scan) is a web scan.
    """
    name = options.get("profile") if isinstance(options, dict) else None
    return resolve_profile(name, settings)
