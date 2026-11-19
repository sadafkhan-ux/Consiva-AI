"""Exact-host scanning, complete URL discovery before inspection, privacy policies reviewed first, changed-evidence detection, and blocked scans labelled (owner's requirements,
19 Nov 2026). Everything here runs without a browser, a database or a model: the browser, the frontier and the redirect reader are replaced by small in-memory fakes, so each test shows
what the code DOES with a given input."""
import ast
import asyncio
import inspect as pyinspect
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.enterprise.crawl import discovery, frontier, guard, host, redirects, runner, scope
from app.monitoring import blocks

ROOT = "https://example.com/"


# ---- the host rule ----------------------------------------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("url,expected", [
    ("https://example.com/about", host.SAME),
    ("https://EXAMPLE.com./about", host.SAME),               # case and a trailing dot are the same host
    ("http://example.com:80/x", host.SAME),                  # a default port is no port
    ("https://example.com:443/x", host.SAME),
    ("https://www.example.com/", host.SUBDOMAIN),            # the www. twin is another host when example.com was submitted
    ("https://blog.example.com/", host.SUBDOMAIN), ("https://shop.example.com/", host.SUBDOMAIN), ("https://support.example.com/", host.SUBDOMAIN),
    ("https://app.example.com/", host.SUBDOMAIN), ("https://admin.example.com/", host.SUBDOMAIN), ("https://a.b.example.com/", host.SUBDOMAIN),
    ("https://example.com:8443/", host.SUBDOMAIN),           # another port is another origin of the same site
    ("https://example.com.evil.com/", host.EXTERNAL),        # not a suffix test
    ("https://evil-example.com/", host.EXTERNAL),
    ("https://notexample.com/", host.EXTERNAL),
    ("https://other.org/", host.EXTERNAL),
    ("mailto:a@example.com", None), ("javascript:void(0)", None), ("ftp://example.com/", None),
])
def test_only_the_exact_submitted_host_is_the_site(url, expected):
    assert host.where(url, ROOT) == expected


def test_an_internationalised_host_is_compared_in_its_ascii_form():
    assert host.where("https://xn--bcher-kva.de/x", "https://bücher.de/") == host.SAME
    assert host.where("https://BÜCHER.de/x", "https://xn--bcher-kva.de/") == host.SAME
    assert host.where("https://shop.bücher.de/", "https://bücher.de/") == host.SUBDOMAIN


def test_submitting_the_www_host_excludes_the_bare_domain_too():
    assert host.where("https://example.com/", "https://www.example.com/") == host.SUBDOMAIN
    assert host.where("https://www.example.com/a", "https://www.example.com/") == host.SAME


def test_exclusion_reasons_name_the_kind_the_host_and_the_allowed_host():
    sub = host.exclusion("https://blog.example.com/x", ROOT, context="linked from a scanned page")
    ext = host.exclusion("https://other.org/y", ROOT)
    assert sub.startswith("excluded_subdomain: ") and "blog.example.com" in sub and "only example.com is scanned" in sub and "linked from a scanned page" in sub
    assert ext.startswith("excluded_external_domain: ") and "other.org" in ext
    assert host.exclusion("https://example.com/z", ROOT) is None


def test_scope_classifies_off_host_urls_and_same_site_is_the_exact_host():
    assert scope.classify("https://blog.example.com/a", ROOT) == scope.SUBDOMAIN
    assert scope.classify("https://other.org/a", ROOT) == scope.EXTERNAL
    assert scope.classify("https://example.com/privacy", ROOT) == scope.PAGE
    assert scope.same_site("https://example.com/x", ROOT) and not scope.same_site("https://www.example.com/x", ROOT)


def test_the_frontier_key_keeps_www_and_bare_hosts_apart_but_still_normalises_the_rest():
    assert frontier.key_of("https://www.example.com/About/") != frontier.key_of("https://example.com/About/")
    assert frontier.key_of("https://WWW.example.com/a#x") == frontier.key_of("https://www.example.com/a")
    assert frontier.key_of("https://example.com/a?utm_source=x") == frontier.key_of("https://example.com/a")


# ---- recording what is not scanned --------------------------------------------------------------------------------------------------------------------------------------

def _fake_add(recorded: list):
    async def add(db, scan_id, url, *, depth, source, max_urls=None, max_depth=None, exclude_reason=None):
        recorded.append({"url": url, "depth": depth, "source": source, "reason": exclude_reason})
        return frontier.EXCLUDED if exclude_reason else frontier.ADDED
    return add


def test_links_to_subdomains_and_other_websites_are_recorded_as_excluded_never_queued(monkeypatch):
    recorded: list = []
    monkeypatch.setattr(discovery.frontier, "add_url", _fake_add(recorded))
    links = ["https://example.com/privacy", "https://blog.example.com/post", "https://www.example.com/x", "https://other.org/y", "https://example.com.evil.com/", "mailto:a@b.c"]
    out = asyncio.run(discovery.add_links(None, None, ROOT, links, parent_depth=0, max_urls=0, max_depth=9))
    by = {r["url"]: r["reason"] for r in recorded}
    assert by["https://example.com/privacy"] is None and out["added_urls"] == ["https://example.com/privacy"]
    assert by["https://blog.example.com/post"].startswith("excluded_subdomain") and by["https://www.example.com/x"].startswith("excluded_subdomain")
    assert by["https://other.org/y"].startswith("excluded_external_domain") and by["https://example.com.evil.com/"].startswith("excluded_external_domain")
    assert "mailto:a@b.c" not in by and out["off_host"] == 4


def test_off_host_sitemap_entries_are_recorded_and_their_sitemap_files_never_fetched(monkeypatch):
    recorded: list = []
    asked: list = []
    monkeypatch.setattr(discovery.frontier, "add_url", _fake_add(recorded))

    async def fake_sitemaps(context, root_url, sitemaps, same_site):
        for sm in ("https://cdn.other.org/sitemap.xml", "https://example.com/sitemap.xml"):     # a sitemap file on another host is checked before it is fetched
            asked.append((sm, same_site(sm)))
        return [u for u in ("https://example.com/a", "https://shop.example.com/b") if same_site(u)]

    monkeypatch.setattr(discovery, "discover_sitemap_urls", fake_sitemaps)
    rules = SimpleNamespace(sitemaps=[])
    out = asyncio.run(discovery.seed(None, None, None, ROOT, rules, max_urls=0, max_depth=9))
    assert asked == [("https://cdn.other.org/sitemap.xml", False), ("https://example.com/sitemap.xml", True)]
    by = {r["url"]: r["reason"] for r in recorded}
    assert by[ROOT] is None and by["https://example.com/a"] is None
    assert by["https://shop.example.com/b"].startswith("excluded_subdomain") and by["https://cdn.other.org/sitemap.xml"].startswith("excluded_external_domain")
    assert out["off_host"] == 2


# ---- the browser guard ----------------------------------------------------------------------------------------------------------------------------------------------------

class _Route:
    def __init__(self, url: str, *, navigation: bool, top: bool) -> None:
        main = SimpleNamespace(parent_frame=None)
        child = SimpleNamespace(parent_frame=main)
        self.request = SimpleNamespace(url=url, is_navigation_request=lambda: navigation, frame=main if top else child)
        self.aborted = False

    async def abort(self, reason=None):
        self.aborted = True


def test_the_browser_guard_stops_top_level_navigation_off_the_host_and_lets_embedded_content_load(monkeypatch):
    passed: list = []

    async def ssrf_guard(route):
        passed.append(route.request.url)

    monkeypatch.setattr(guard, "_guard_navigation", ssrf_guard)
    stopped: list = []
    handler = guard.host_guard(ROOT, stopped)
    cases = {
        "top_sub": _Route("https://blog.example.com/", navigation=True, top=True),
        "top_ext": _Route("https://other.org/", navigation=True, top=True),
        "top_same": _Route("https://example.com/next", navigation=True, top=True),
        "iframe_ext": _Route("https://consent.cmp.example/banner", navigation=True, top=False),     # a consent banner in an iframe: the page's own content
        "script_ext": _Route("https://www.googletagmanager.com/gtm.js", navigation=False, top=True),
    }
    for r in cases.values():
        asyncio.run(handler(r))
    assert cases["top_sub"].aborted and cases["top_ext"].aborted
    assert not any(cases[k].aborted for k in ("top_same", "iframe_ext", "script_ext"))
    assert set(passed) == {"https://example.com/next", "https://consent.cmp.example/banner", "https://www.googletagmanager.com/gtm.js"}    # still through the SSRF guard
    assert [s["url"] for s in stopped] == ["https://blog.example.com/", "https://other.org/"] and stopped[0]["reason"].startswith("excluded_subdomain")


def test_every_browser_context_the_scan_opens_has_the_host_guard():
    src = Path(runner.__file__).read_text(encoding="utf-8")
    assert "_guard_navigation" not in src.replace("host_guard", "")                       # no context is left with only the SSRF guard
    assert src.count('.route("**/*", host_guard(root_url') == 3                        # the shared context, each page's context, the discovery context
    assert "make_session_tools(browser, settings, host_guard(root_url)" in src        # every consent-test session


# ---- redirects --------------------------------------------------------------------------------------------------------------------------------------------------------

class _Requests:
    def __init__(self, answers: dict) -> None:
        self.answers, self.asked = answers, []

    async def head(self, url, *, timeout, max_redirects):
        assert max_redirects == 0                                                      # never followed by the HTTP client: every hop is checked here
        self.asked.append(url)
        a = self.answers[url]
        if isinstance(a, Exception):
            raise a
        return SimpleNamespace(status=a[0], headers={"location": a[1]} if a[1] else {})


@pytest.fixture
def no_ssrf(monkeypatch):
    async def ok(url):
        return ["93.184.216.34"]
    monkeypatch.setattr(redirects, "assert_safe_url", ok)


def test_a_redirect_to_another_host_is_found_before_navigation_and_not_followed(no_ssrf):
    req = _Requests({"https://example.com/shop": (301, "https://shop.example.com/"), "https://shop.example.com/": (200, None)})
    out = asyncio.run(redirects.check(req, "https://example.com/shop", ROOT))
    assert out.off_host == "https://shop.example.com/" and req.asked == ["https://example.com/shop"]       # the other host is never contacted


def test_a_same_host_redirect_chain_is_followed_to_its_end(no_ssrf):
    req = _Requests({"https://example.com/a": (302, "/b"), "https://example.com/b": (301, "https://example.com/c"), "https://example.com/c": (200, None)})
    out = asyncio.run(redirects.check(req, "https://example.com/a", ROOT))
    assert out.off_host is None and out.final_url == "https://example.com/c" and [h["status"] for h in out.hops] == [302, 301, 200]


def test_an_unreadable_chain_is_unknown_not_safe(no_ssrf):
    req = _Requests({"https://example.com/a": RuntimeError("HEAD not allowed")})
    out = asyncio.run(redirects.check(req, "https://example.com/a", ROOT))
    assert out.off_host is None and out.final_url is None and "HEAD not allowed" in out.error      # the page is opened and its final address checked after loading


def test_a_script_navigation_stopped_by_the_guard_is_excluded_with_its_real_destination():
    # found in the real-browser run: when the guard aborts a page's own JavaScript navigation, Chromium shows chrome-error://chromewebdata/; the reason must name where the page tried to go
    reason = runner.off_host_reason("https://example.com/partners", "chrome-error://chromewebdata/", [{"url": "https://partner.example.com/landing"}], ROOT)
    assert reason.startswith("excluded_subdomain") and "partner.example.com" in reason and "the browser guard stopped it" in reason
    plain = runner.off_host_reason("https://example.com/r", "https://other.org/", [], ROOT)
    assert plain.startswith("excluded_external_domain") and "https://example.com/r redirected to https://other.org/" in plain


def test_the_final_address_is_checked_again_after_loading():
    src = pyinspect.getsource(runner._process)
    assert src.index("redirects.check(") < src.index("deep_inspect(") < src.index("if not scope.same_site(page.final_url, root_url)")
    assert src.index("if not scope.same_site(page.final_url, root_url)") < src.index("_keep_page_evidence(")                # nothing from another host is stored


# ---- phase 1: the complete inventory before any deep inspection ---------------------------------------------------------------------------------------------------------

class _Frontier:
    """An in-memory frontier with the same identities and outcomes as the real one."""

    def __init__(self) -> None:
        self.rows: dict[str, SimpleNamespace] = {}

    def add(self, url, depth, reason=None):
        k = frontier.key_of(url)
        if k in self.rows:
            return frontier.DUPLICATE
        self.rows[k] = SimpleNamespace(id=uuid.uuid4(), original_url=url, depth=depth, status="excluded" if reason else "pending", status_reason=reason)
        return frontier.EXCLUDED if reason else frontier.ADDED

    def by_id(self, rid):
        return next(r for r in self.rows.values() if r.id == rid)

    def status(self, url):
        return self.rows[frontier.key_of(url)]


class _DB:
    def __init__(self, f: _Frontier) -> None:
        self.f = f

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, stmt, params=None):
        rows = sorted((r for r in self.f.rows.values() if r.status == "pending"), key=lambda r: r.depth)
        return SimpleNamespace(all=lambda: rows)

    async def commit(self):
        pass


@pytest.fixture
def phase1(monkeypatch):
    f = _Frontier()
    f.add(ROOT, 0)
    pages: dict[str, dict] = {}
    visits: list[str] = []
    audits: list = []

    async def add_url(db, scan_id, url, *, depth, source, max_urls=None, max_depth=None, exclude_reason=None):
        return f.add(url, depth, exclude_reason)

    async def find_row(db, scan_id, url):
        return f.rows.get(frontier.key_of(url))

    async def mark_excluded(db, rid, reason):
        r = f.by_id(rid)
        r.status, r.status_reason = "excluded", reason

    async def nothing(*a, **k):
        return None

    async def counts(db, scan_id):
        out = {"pending": 0, "in_progress": 0, "done": 0, "failed": 0, "excluded": 0}
        for r in f.rows.values():
            out[r.status] += 1
        return out

    async def read_links(ctx, url, *, settings, is_home):
        visits.append(url)
        return pages[url]

    async def no_redirect(rc, url, root_url):
        return redirects.RedirectCheck(url=url, final_url=url)

    async def audit(scan_id, event, detail):
        audits.append((event, detail))

    async def not_cancelled(scan_id, org_id):
        return False

    for name, fn in (("add_url", add_url), ("find_row", find_row), ("mark_excluded", mark_excluded), ("add_not_recorded", nothing), ("counts", counts)):
        monkeypatch.setattr(frontier, name, fn)
    monkeypatch.setattr(runner, "async_session_factory", lambda: _DB(f))
    monkeypatch.setattr(runner, "_read_links", read_links)
    monkeypatch.setattr(runner.redirects, "check", no_redirect)
    monkeypatch.setattr(runner, "_audit", audit)
    monkeypatch.setattr(runner, "_is_cancelled", not_cancelled)
    runner._BLOCKS.pop("scan", None)

    class _Ctx:
        async def route(self, *a):
            pass

        async def close(self):
            pass

    class _Browser:
        async def new_context(self, **k):
            return _Ctx()

    class _Limiter:
        async def acquire(self):
            pass

    async def go():
        return await runner._discover_inventory(scan_id="scan", org_id="org", root_url=ROOT, rules=SimpleNamespace(allows=lambda u: True), browser=_Browser(),
                                                settings=runner.get_settings(), request_context=None, limiter=_Limiter(), guard=SimpleNamespace(check=lambda: None),
                                                max_urls=0, max_depth=9)

    return SimpleNamespace(f=f, pages=pages, visits=visits, audits=audits, go=go)


def _loaded(*links, final=None):
    return {"outcome": "loaded", "verdict": None, "error": None, "final_url": final, "links": list(links)}


def test_phase_1_records_the_whole_inventory_one_page_at_a_time_before_any_inspection(phase1):
    phase1.pages.update({
        ROOT: _loaded("https://example.com/privacy", "https://example.com/about", "https://blog.example.com/x", "https://www.example.com/z", "https://other.org/y", final=ROOT),
        "https://example.com/privacy": _loaded("https://example.com/terms", final="https://example.com/privacy"),
        "https://example.com/about": _loaded("https://example.com/privacy", "https://example.com/careers", final="https://example.com/about"),
        "https://example.com/terms": _loaded(final="https://example.com/terms"),
        "https://example.com/careers": _loaded(final="https://example.com/careers"),
    })
    out = asyncio.run(phase1.go())
    assert phase1.visits == [ROOT, "https://example.com/privacy", "https://example.com/about", "https://example.com/terms", "https://example.com/careers"]  # breadth first, each once
    pending = {r.original_url for r in phase1.f.rows.values() if r.status == "pending"}
    assert pending == {ROOT, "https://example.com/privacy", "https://example.com/about", "https://example.com/terms", "https://example.com/careers"}   # nothing inspected yet: all wait for phase 2/3
    reasons = {r.original_url: r.status_reason for r in phase1.f.rows.values() if r.status == "excluded"}
    assert reasons["https://blog.example.com/x"].startswith("excluded_subdomain") and reasons["https://www.example.com/z"].startswith("excluded_subdomain")
    assert reasons["https://other.org/y"].startswith("excluded_external_domain")
    assert out["off_host"] == 3 and not out["block"] and out["stopped"] is None
    assert [e for e, _ in phase1.audits] == [runner.DISCOVERY_DONE] and phase1.audits[0][1]["inventory"]["pending"] == 5


def test_phase_1_stops_at_once_when_the_site_blocks_before_ten_pages_and_does_not_mark_discovery_done(phase1):
    verdict = blocks.BlockVerdict(True, blocks.ACCESS_BLOCKED, "the site answered HTTP 403 Forbidden instead of a page", None)
    phase1.pages.update({
        ROOT: _loaded("https://example.com/a", "https://example.com/b", final=ROOT),
        "https://example.com/a": {"outcome": "blocked", "verdict": verdict, "error": "HTTP 403", "final_url": None, "links": []},
        "https://example.com/b": _loaded(final="https://example.com/b"),
    })
    out = asyncio.run(phase1.go())
    assert out["block"] and phase1.visits == [ROOT, "https://example.com/a"] and phase1.audits == []
    assert phase1.f.status("https://example.com/a").status == "pending" and runner._BLOCKS["scan"]["url"] == "https://example.com/a"   # kept, to be resumed


def test_phase_1_a_homepage_that_redirects_to_the_www_host_excludes_it_and_scans_nothing_else(phase1):
    phase1.pages[ROOT] = _loaded("https://www.example.com/a", final="https://www.example.com/")
    out = asyncio.run(phase1.go())
    assert out["seed_off_host"] == "https://www.example.com/"
    seed = phase1.f.status(ROOT)
    assert seed.status == "excluded" and seed.status_reason.startswith("excluded_subdomain") and "redirected to https://www.example.com/" in seed.status_reason
    assert phase1.audits == []


def test_phase_1_a_same_host_redirect_is_inspected_at_its_destination_once(phase1):
    phase1.pages.update({
        ROOT: _loaded("https://example.com/old", final=ROOT),
        "https://example.com/old": _loaded(final="https://example.com/new"),
        "https://example.com/new": _loaded(final="https://example.com/new"),
    })
    asyncio.run(phase1.go())
    assert phase1.f.status("https://example.com/old").status == "excluded" and phase1.f.status("https://example.com/old").status_reason.startswith("redirect:")
    assert phase1.f.status("https://example.com/new").status == "pending"


def test_phase_1_runs_before_the_homepage_is_deeply_inspected_and_is_not_repeated_once_done():
    src = ast.get_source_segment(Path(runner.__file__).read_text(encoding="utf-8"),
                                 next(n for n in ast.parse(Path(runner.__file__).read_text(encoding="utf-8")).body if isinstance(n, ast.AsyncFunctionDef) and n.name == "run"))
    assert src.index("_audit_has(scan_id, DISCOVERY_DONE)") < src.index("_discover_inventory(") < src.index("frontier.claim_seed(")
    assert src.index("_discover_inventory(") < src.index("slug_triage.triage_safe(") < src.index("frontier.claim_seed(")       # the policy triage sees the whole inventory


# ---- phase 2: privacy policies reviewed before any other page --------------------------------------------------------------------------------------------------------

def test_the_privacy_review_runs_when_the_last_privacy_page_is_done_and_before_the_next_page_is_claimed():
    src = pyinspect.getsource(runner.run)
    loop = src[src.index("while go and block_stop is None:"):]
    assert loop.index("_privacy_pages_left(scan_id) == 0") < loop.index("_privacy_review(scan_id, org_id)") < loop.index("frontier.claim_next(")
    assert "privacy_reviewed = await _audit_has(scan_id, PRIVACY_REVIEWED)" in src                 # not repeated on a resumed scan


def test_the_interim_review_never_writes_the_final_status_or_assessments_but_keeps_its_readings():
    calls: list = []

    class Inner:
        async def upsert_chunks(self, *a, **k):
            calls.append("upsert_chunks")

        async def save_assessments(self, *a, **k):
            calls.append("save_assessments")
            return 5

    repo = runner._InterimRepo(Inner())

    async def go():
        await repo.upsert_chunks(1, [])
        assert await repo.save_assessments(1, []) == 0
        await repo.set_scan_status(1, "completed", completed=True)
        await repo.set_frontier_status(1, {})
        await repo.set_frontier_tracking(1, {})

    asyncio.run(go())
    assert calls == ["upsert_chunks"]                     # the readings are kept for the final analysis to reuse; nothing final is written


def test_no_privacy_page_found_is_recorded_not_guessed(monkeypatch):
    audits: list = []

    class _Rows:
        def __init__(self):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def execute(self, *a, **k):
            return SimpleNamespace(all=lambda: [])

    async def audit(scan_id, event, detail):
        audits.append((event, detail))

    monkeypatch.setattr(runner, "async_session_factory", lambda: _Rows())
    monkeypatch.setattr(runner, "_audit", audit)
    asyncio.run(runner._privacy_review("scan", "org"))
    assert audits[0][0] == runner.PRIVACY_REVIEWED and "no privacy policy page was found" in audits[0][1]["note"]


# ---- new and changed evidence ---------------------------------------------------------------------------------------------------------------------------------------

def test_a_cookie_seen_again_with_another_lifetime_or_flags_is_changed_not_new():
    fp = runner.Fingerprints()
    a = {"identity": "sid|example.com|/", "secure": False, "http_only": True, "same_site": "Lax", "first_party": True, "lifetime": "session"}
    assert fp.changed_on([a]) == []
    assert fp.changed_on([dict(a)]) == []                                                       # identical: an existing cookie
    assert fp.changed_on([{**a, "lifetime": "1_year_or_more"}]) == ["sid|example.com|/"]      # same identity, another lifetime: changed
    assert fp.changed_on([{**a, "lifetime": "1_year_or_more", "secure": True}]) == ["sid|example.com|/"]


def test_the_lifetime_class_does_not_change_between_two_loads_seconds_apart():
    c = {"expires": 1_000_000 + 400 * 86_400, "session": False}
    assert runner.cookie_lifetime(c, now=1_000_000) == runner.cookie_lifetime(c, now=1_000_030) == "1_year_or_more"
    assert runner.cookie_lifetime({"expires": None, "session": True}) == "session"


def test_script_identity_drops_the_query_string_like_the_5_page_agent():
    assert runner.script_identity("https://WWW.GoogleTagManager.com/gtm.js?id=GTM-1&cb=9") == runner.script_identity("https://www.googletagmanager.com/gtm.js?id=GTM-1&cb=10")


# ---- blocked scans in the report ------------------------------------------------------------------------------------------------------------------------------------

def test_a_blocked_scan_is_labelled_blocked_and_incomplete_in_the_report():
    from app.enterprise.report.html import render_html

    from .test_enterprise_report import build, decl, visible_text
    urls = {**decl()["urls"], "blocked": 1, "excluded_subdomain": 3, "excluded_external_domain": 7, "pending": 9}
    d = build(declaration=decl(crawl_status="failed", completion="failed", stop_reason="access_blocked", blocked=True, urls=urls))
    text = visible_text(render_html(d))
    assert "Blocked, incomplete" in text and "not a website-wide assessment" in text and "blocked and incomplete" in text
    assert "Excluded: subdomain or other host of the site" in text and "Excluded: other websites" in text and "Blocked by the website" in text


def test_coverage_says_when_a_scan_was_blocked():
    from app.enterprise import coverage
    src = pyinspect.getsource(coverage.coverage_declaration)
    assert '"blocked": scan["status"] == "failed" and (scan["stop_reason"] or "") in BLOCK_STOPS' in src
    assert "last_error like 'BLOCKED%'" in pyinspect.getsource(coverage)
