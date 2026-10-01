"""Page-coverage contracts for the pre_consent crawl.

The 25-page ceiling these cover was never a technical limit -- there is no hidden cap
anywhere in the fetch path -- but it WAS the effective one for every deployment, and
raising it alone would not have bought proportional coverage while four spellings of
one URL could each consume a slot of the budget. So these tests pin both halves: the
limit is configuration and is honoured, and the budget is spent on distinct pages.

Driven through a fake BrowserContext serving a synthetic site rather than a real
browser: the properties under test (what gets queued, what consumes budget, what is
counted as scanned vs failed vs blocked) are all crawler-loop logic, and a real site
cannot be made to reliably produce a robots-disallowed path or a 40-page link graph
on demand. Live verification of the real end-to-end numbers is a separate exercise.
"""

import io
import re
import tokenize
from urllib.parse import urlparse

import pytest

from app.config import get_settings
from app.scanner.crawler import _crawl_full_site
from app.scanner.url_normalize import normalize_url, priority

ROOT = "https://example.com"


class _FakeResponse:
    def __init__(self, status: int = 200):
        self.status = status


class _FakeFrame:
    def __init__(self, html: str = "", url: str = ""):
        self._html = html
        self.url = url

    async def content(self):
        return self._html


class _FakePage:
    """Enough of a Playwright Page for _fetch_one_page. `pages` maps a normalised URL
    to the HTML served there; anything absent raises, standing in for a dead link."""

    def __init__(self, pages: dict[str, str], failures: dict[str, Exception], requests: list):
        self._pages = pages
        self._failures = failures
        self._requests = requests
        self._url = None
        self._closed = False
        self.main_frame = _FakeFrame()
        self.frames = [self.main_frame]

    def on(self, event, handler):
        pass

    async def goto(self, url, timeout=None, wait_until=None):
        key = normalize_url(url)
        if key in self._failures:
            raise self._failures[key]
        if key not in self._pages:
            raise RuntimeError(f"net::ERR_ABORTED at {url}")
        self._url = key
        self._requests.append(key)
        return _FakeResponse(200)

    async def wait_for_timeout(self, ms):
        return None

    async def content(self):
        return self._pages[self._url]

    async def evaluate(self, script, arg=None):
        if arg is not None:            # the CMP global-var probe
            return []
        if "scrollBy" in script:       # _progressive_scroll -- report "at bottom" at once
            return True
        return ""                      # _SHADOW_DOM_TEXT_JS

    def is_closed(self):
        return self._closed

    async def close(self):
        self._closed = True


class _FakeContext:
    def __init__(self, pages: dict[str, str], failures: dict[str, Exception] | None = None):
        self.pages = {normalize_url(u): html for u, html in pages.items()}
        self.failures = {normalize_url(u): e for u, e in (failures or {}).items()}
        self.navigations: list[str] = []

    async def new_page(self):
        return _FakePage(self.pages, self.failures, self.navigations)

    async def cookies(self):
        return []


def _links(*hrefs: str) -> str:
    return "<html><body>" + "".join(f'<a href="{h}">x</a>' for h in hrefs) + "</body></html>"


async def _crawl(context, settings=None, disallowed=None, sitemap=None):
    settings = settings or get_settings()
    return await _crawl_full_site(
        context, ROOT, settings, disallowed_prefixes=disallowed or set(), sitemap_urls=sitemap
    )


# ── 1-4. the limit is configuration, is honoured, and 25 is not it ───────────────

def test_the_shipped_default_page_limit_is_no_longer_25():
    """The regression this whole change exists for. 25 was a config value, not a
    technical bound, but it was the effective ceiling on every deployment."""
    assert get_settings().scanner_max_pages > 25


def test_no_hidden_second_page_cap_exists_in_the_fetch_path():
    """A higher configured limit is worthless if a literal 25 survives somewhere in the
    crawl. Asserted against the source because a hidden cap is, by definition, not
    reachable through the public contract until a scan is already large enough to hit
    it -- at which point it looks like a slow site rather than a bug."""
    from pathlib import Path

    crawler_src = Path(__file__).resolve().parent.parent / "app" / "scanner" / "crawler.py"
    # Tokenised rather than line-matched: this file documents its own history at length,
    # so "25" appears in prose in both comments and docstrings. Only an actual NUMBER
    # token in executable code is a cap.
    offenders = []
    for token in tokenize.generate_tokens(io.StringIO(crawler_src.read_text()).readline):
        if token.type == tokenize.NUMBER and token.string == "25":
            offenders.append(f"line {token.start[0]}: {token.line.strip()}")
    assert not offenders, f"literal 25 in crawler.py code: {offenders}"

    # ...and the same for the value the crawl actually reads.
    assert get_settings().scanner_max_pages != 25


@pytest.mark.parametrize("limit", [3, 7, 30])
async def test_the_configured_page_limit_is_what_actually_bounds_the_crawl(limit):
    """Honoured exactly, at values both below and well above the old 25."""
    pages = {f"{ROOT}/": _links(*[f"/p{i}" for i in range(60)])}
    for i in range(60):
        pages[f"{ROOT}/p{i}"] = _links()
    context = _FakeContext(pages)

    settings = get_settings().model_copy(update={"scanner_max_pages": limit})
    records, *_rest, diagnostics = await _crawl(context, settings)

    assert diagnostics["pages_attempted"] == limit
    assert diagnostics["pages_scanned"] == limit
    assert len(context.navigations) == limit
    assert diagnostics["page_limit"] == limit


async def test_more_than_twenty_five_pages_are_actually_crawled():
    """The end the whole change is for: a site with enough pages gets more than 25 of
    them fetched, with real PageRecords to show for it."""
    pages = {f"{ROOT}/": _links(*[f"/p{i}" for i in range(80)])}
    for i in range(80):
        pages[f"{ROOT}/p{i}"] = _links()
    context = _FakeContext(pages)

    records, *_rest, diagnostics = await _crawl(context)

    assert diagnostics["pages_scanned"] > 25
    scanned = [p for p in records if p.discovered_via in ("seed", "link", "sitemap")]
    assert len(scanned) == diagnostics["pages_scanned"] > 25


# ── 5-6. failed and blocked pages are reported, and reported SEPARATELY ──────────

async def test_failed_pages_are_counted_apart_from_scanned_ones():
    from playwright.async_api import Error as PlaywrightError

    pages = {f"{ROOT}/": _links("/ok", "/dead"), f"{ROOT}/ok": _links()}
    context = _FakeContext(pages, failures={f"{ROOT}/dead": PlaywrightError("net::ERR_CONNECTION_RESET")})

    records, *_rest, diagnostics = await _crawl(context)

    assert diagnostics["pages_scanned"] == 2          # / and /ok
    assert diagnostics["pages_failed"] == 1           # /dead
    assert diagnostics["pages_attempted"] == 3        # all three cost a navigation
    dead = next(p for p in records if p.url.endswith("/dead"))
    assert dead.discovered_via == "failed"            # present, not silently dropped
    assert dead.http_status is None


async def test_robots_blocked_pages_are_recorded_but_do_not_spend_the_page_budget():
    """The §4 distinction. A disallowed URL costs no navigation, so charging it against
    the fetch budget -- as the previous implementation did -- meant a site with a
    restrictive robots.txt silently got a smaller real crawl than its configured limit."""
    pages = {f"{ROOT}/": _links("/admin/a", "/admin/b", "/public")}
    pages[f"{ROOT}/public"] = _links()
    context = _FakeContext(pages)

    records, *_rest, diagnostics = await _crawl(context, disallowed={"/admin"})

    assert diagnostics["pages_blocked_robots"] == 2
    assert diagnostics["pages_scanned"] == 2                 # / and /public
    assert diagnostics["pages_attempted"] == 2               # the two blocked cost nothing
    blocked = [p for p in records if p.discovered_via == "robots_disallowed"]
    assert len(blocked) == 2
    assert len(context.navigations) == 2                     # never actually fetched


async def test_a_restrictive_robots_txt_no_longer_shrinks_the_real_crawl():
    """Directly the behaviour change: with a 5-page budget and 3 disallowed URLs, the
    old accounting would have fetched 2 pages. It must now still fetch 5."""
    pages = {f"{ROOT}/": _links("/no/a", "/no/b", "/no/c", *[f"/y{i}" for i in range(10)])}
    for i in range(10):
        pages[f"{ROOT}/y{i}"] = _links()
    context = _FakeContext(pages)

    settings = get_settings().model_copy(update={"scanner_max_pages": 5})
    _records, *_rest, diagnostics = await _crawl(context, settings, disallowed={"/no"})

    assert diagnostics["pages_scanned"] == 5
    assert diagnostics["pages_blocked_robots"] == 3


# ── 7. duplicate spellings must not consume budget ──────────────────────────────

async def test_duplicate_url_spellings_do_not_each_consume_a_page_slot():
    """Four spellings of one page -- trailing slash, fragment, UTM tag, reordered query
    -- are one crawl entry. This is the other half of the coverage fix: without it a
    raised limit is spent re-fetching bytes already fetched."""
    pages = {
        f"{ROOT}/": _links(
            "/about", "/about/", "/about#team", "/about?utm_source=nav",
            "/x?a=1&b=2", "/x?b=2&a=1",
        ),
        f"{ROOT}/about": _links(),
        f"{ROOT}/x?a=1&b=2": _links(),
    }
    context = _FakeContext(pages)

    _records, *_rest, diagnostics = await _crawl(context)

    assert diagnostics["pages_scanned"] == 3           # /, /about, /x
    assert diagnostics["pages_skipped_duplicate"] == 4  # the 4 redundant spellings
    assert sorted(context.navigations) == [f"{ROOT}/", f"{ROOT}/about", f"{ROOT}/x?a=1&b=2"]


def test_normalisation_keeps_urls_that_genuinely_differ_apart():
    """The conservative direction matters as much as the dedup: a query parameter the
    server actually reads must never be stripped, or real pages vanish from the crawl."""
    assert normalize_url(f"{ROOT}/p?id=1") != normalize_url(f"{ROOT}/p?id=2")
    assert normalize_url(f"{ROOT}/a") != normalize_url(f"{ROOT}/b")
    assert normalize_url(f"{ROOT}/p?page=2") != normalize_url(f"{ROOT}/p")
    # ...while the inert ones do collapse
    assert normalize_url(f"{ROOT}/p?utm_source=x") == normalize_url(f"{ROOT}/p")
    assert normalize_url(f"{ROOT}/p/") == normalize_url(f"{ROOT}/p#frag")


# ── 8. sitemap-derived URLs ──────────────────────────────────────────────────────

async def test_sitemap_urls_are_crawled_and_attributed_to_the_sitemap():
    """Link-following alone only reaches what an already-fetched page links to. A
    sitemap URL that nothing links to must still be crawled, and must be recorded as
    having come from the sitemap rather than mislabelled as a followed link."""
    pages = {f"{ROOT}/": _links(), f"{ROOT}/orphan": _links()}
    context = _FakeContext(pages)

    records, *_rest, diagnostics = await _crawl(context, sitemap=[f"{ROOT}/orphan"])

    orphan = next(p for p in records if p.url.endswith("/orphan"))
    assert orphan.discovered_via == "sitemap"
    assert diagnostics["pages_from_sitemap"] == 1
    assert diagnostics["pages_scanned"] == 2


async def test_offsite_sitemap_entries_are_refused():
    """A sitemap is attacker-controlled input from the scanned site. The same-domain
    boundary applies to what it publishes exactly as it does to a followed link."""
    pages = {f"{ROOT}/": _links()}
    context = _FakeContext(pages)

    _records, *_rest, diagnostics = await _crawl(
        context, sitemap=["https://evil.example/x", f"{ROOT}/ok"]
    )

    assert diagnostics["pages_from_sitemap"] == 1  # only the same-site one was queued
    assert all("evil.example" not in u for u in context.navigations)


# ── discovery / attempted / scanned are distinct numbers ────────────────────────

async def test_the_diagnostics_distinguish_discovered_attempted_and_scanned():
    """§15: these were one number before, which is what made "25 pages" impossible to
    interpret. Discovery must exceed the budget without inflating what was scanned."""
    pages = {f"{ROOT}/": _links(*[f"/p{i}" for i in range(40)])}
    for i in range(40):
        pages[f"{ROOT}/p{i}"] = _links()
    context = _FakeContext(pages)

    settings = get_settings().model_copy(update={"scanner_max_pages": 10})
    _records, *_rest, d = await _crawl(context, settings)

    assert d["pages_discovered"] == 41           # seed + 40 links, all distinct
    assert d["pages_attempted"] == 10            # the budget
    assert d["pages_scanned"] == 10
    assert d["pages_not_attempted_budget_exhausted"] == 31
    assert d["pages_discovered"] > d["pages_attempted"] >= d["pages_scanned"]


# ── priority ordering ────────────────────────────────────────────────────────────

def test_compliance_relevant_pages_outrank_blog_noise():
    assert priority(f"{ROOT}/") == 0
    assert priority(f"{ROOT}/privacy-policy") < priority(f"{ROOT}/about")
    assert priority(f"{ROOT}/about") < priority(f"{ROOT}/blog/post-1")
    assert priority(f"{ROOT}/cookie-policy") < priority(f"{ROOT}/some/random/page")


async def test_a_tight_budget_spends_itself_on_the_pages_that_carry_evidence():
    """With room for only a few pages, the crawl must reach the privacy policy rather
    than three blog posts that happened to be linked first."""
    pages = {f"{ROOT}/": _links("/blog/a", "/blog/b", "/blog/c", "/privacy", "/contact")}
    for path in ("/blog/a", "/blog/b", "/blog/c", "/privacy", "/contact"):
        pages[f"{ROOT}{path}"] = _links()
    context = _FakeContext(pages)

    settings = get_settings().model_copy(update={"scanner_max_pages": 3})
    _records, *_rest, _d = await _crawl(context, settings)

    fetched = {urlparse(u).path for u in context.navigations}
    assert "/privacy" in fetched
    assert "/contact" in fetched
    assert not any(p.startswith("/blog") for p in fetched)
