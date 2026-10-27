"""robots.txt is read the way RFC 9309 says, so the scanner visits what a site permits and nothing it forbids, and says which pages it did not visit."""

import pytest

from app.config import get_settings
from app.scanner.crawler import _crawl_full_site, _is_robots_disallowed
from app.scanner.failures import ScanNotPossible
from app.scanner.robots import RobotsRules, agent_token, parse_robots
from tests.test_navigation_resilience import (  # noqa: F401
    ROOT,
    SiteContext,
    links,
    quick,
    site_with,
)

UA = "ConsivaConsentAgent/0.1"


def rules(text, ua=UA):
    return parse_robots(text, ua)


class TestParsing:
    def test_the_agent_token_is_the_product_name(self):
        assert agent_token("ConsivaConsentAgent/0.1 (+https://x)") == "consivaconsentagent"

    def test_disallow_blocks_by_prefix_and_everything_else_is_allowed(self):
        r = rules("User-agent: *\nDisallow: /private\n")
        assert not r.allows("https://x.test/private/a") and not r.allows("/private")
        assert r.allows("https://x.test/privacy-policy") and r.allows("https://x.test/")

    def test_an_allow_inside_a_disallowed_directory_wins_by_being_longer(self):
        r = rules("User-agent: *\nDisallow: /legal/\nAllow: /legal/privacy\n")
        assert r.allows("https://x.test/legal/privacy") and not r.allows("https://x.test/legal/terms")

    def test_on_a_tie_allow_wins(self):
        assert rules("User-agent: *\nDisallow: /a\nAllow: /a\n").allows("/a")

    def test_wildcards_and_end_anchor(self):
        r = rules("User-agent: *\nDisallow: /*?sid=\nDisallow: /*.pdf$\n")
        assert not r.allows("https://x.test/a?sid=1") and not r.allows("https://x.test/files/x.pdf")
        assert r.allows("https://x.test/files/x.pdf.html") and r.allows("https://x.test/a?page=2")

    def test_an_empty_disallow_blocks_nothing(self):
        assert rules("User-agent: *\nDisallow:\n").allows("/anything")

    def test_a_group_naming_the_scanner_replaces_the_star_group(self):
        text = "User-agent: *\nDisallow: /\n\nUser-agent: ConsivaConsentAgent\nDisallow: /admin\n"
        r = rules(text)
        assert r.allows("/privacy") and not r.allows("/admin/x")
        assert not rules(text, "OtherBot/1").allows("/privacy")

    def test_several_user_agent_lines_share_one_group(self):
        r = rules("User-agent: googlebot\nUser-agent: consivaconsentagent\nDisallow: /x\n")
        assert not r.allows("/x")

    def test_comments_case_and_sitemaps(self):
        r = rules("# hi\nUSER-AGENT: *  # all\nDISALLOW: /a # no\nSitemap: https://x.test/s.xml\n")
        assert not r.allows("/a") and r.sitemaps == ["https://x.test/s.xml"]

    def test_no_rules_or_garbage_allows_everything(self):
        for text in ("", "garbage", "Disallow: /x\n"):           # a rule before any User-agent line belongs to no group
            assert rules(text).allows("/x")

    def test_a_site_that_blocks_everything_is_recognised(self):
        assert rules("User-agent: *\nDisallow: /\n").blocks_everything()
        assert not rules("User-agent: *\nDisallow: /a\n").blocks_everything()


class TestInTheCrawl:
    def test_the_legacy_prefix_set_still_works(self):
        assert _is_robots_disallowed("https://x.test/a/b", {"/a"}) and not _is_robots_disallowed("https://x.test/c", {"/a"})

    def test_parsed_rules_are_accepted(self):
        r = RobotsRules(rules=[(False, "/a"), (True, "/a/ok")])
        assert _is_robots_disallowed("https://x.test/a/x", r) and not _is_robots_disallowed("https://x.test/a/ok", r)

    async def test_pages_the_site_permits_are_scanned_and_the_forbidden_ones_are_recorded_not_assessed(self, quick):  # noqa: F811
        site = site_with({"https://x.test/legal/privacy": ("ok", 200, "<html><head><title>Privacy</title></head></html>")})
        site[ROOT] = ("ok", 200, links("/legal/privacy", "/legal/terms"))
        ctx = SiteContext(site)
        r = rules("User-agent: *\nDisallow: /legal/\nAllow: /legal/privacy\n")
        pages, *_rest, diagnostics = await _crawl_full_site(ctx, ROOT, get_settings(), disallowed_prefixes=r)
        visited = list(ctx.visited)
        assert any(u.endswith("/legal/privacy") for u in visited) and not any(u.endswith("/legal/terms") for u in visited)
        assert diagnostics["pages_blocked_robots"] == 1
        assert not any(p.url.endswith("/legal/terms") for p in pages)

    async def test_a_site_that_forbids_the_whole_scan_is_not_reported_as_scanned(self, quick):  # noqa: F811
        ctx = SiteContext(site_with({}))
        with pytest.raises(ScanNotPossible) as caught:
            await _crawl_full_site(ctx, ROOT, get_settings(), disallowed_prefixes=rules("User-agent: *\nDisallow: /\n"))
        assert caught.value.code == "robots_disallowed" and ctx.visited == []


class _Resp:
    def __init__(self, status, body="", location=None):
        self.status, self._body = status, body
        self.ok = 200 <= status < 300
        self.headers = {"location": location} if location else {}

    async def text(self):
        return self._body


class _RobotsContext:
    """Serves robots.txt URLs from a table; records what was requested and whether redirects were left to the caller."""

    def __init__(self, table):
        self.table, self.asked = table, []
        self.request = self

    async def get(self, url, timeout=0, max_redirects=0):
        assert max_redirects == 0, "redirects are followed by the scanner, hop by hop, never by the HTTP client"
        self.asked.append(url)
        r = self.table.get(url)
        if isinstance(r, Exception):
            raise r
        return r or _Resp(404)


@pytest.fixture
def safe_hosts(monkeypatch):
    async def ok(url):
        return ["93.184.216.34"]
    monkeypatch.setattr("app.scanner.crawler.assert_safe_url", ok)


class TestFetching:
    """What each robots.txt answer means (RFC 9309), and what happens when permission cannot be determined."""

    BLOCK = "User-agent: *\nDisallow: /private\nSitemap: https://x.test/s.xml\n"

    async def fetch(self, table, root="https://x.test/"):
        from app.scanner.crawler import _fetch_robots
        ctx = _RobotsContext(table)
        return await _fetch_robots(ctx, root), ctx

    @pytest.fixture
    def allow_policy(self, monkeypatch):
        s = get_settings().model_copy(update={"scanner_robots_unreadable_policy": "allow"})
        monkeypatch.setattr("app.scanner.crawler.get_settings", lambda: s)

    def test_the_default_policy_is_not_to_scan_on_a_guess(self):
        assert type(get_settings()).model_fields["scanner_robots_unreadable_policy"].default == "disallow"

    # -- readable --
    async def test_a_robots_txt_that_redirects_within_the_site_is_still_obeyed(self, safe_hosts):
        (r, sitemaps), ctx = await self.fetch({
            "https://x.test/robots.txt": _Resp(301, location="https://www.x.test/robots.txt"),
            "https://www.x.test/robots.txt": _Resp(200, self.BLOCK)})
        assert not r.allows("https://x.test/private/a") and sitemaps == ["https://x.test/s.xml"]
        assert ctx.asked == ["https://x.test/robots.txt", "https://www.x.test/robots.txt"]

    @pytest.mark.parametrize("status", [401, 403, 404, 410])
    async def test_a_4xx_means_the_site_publishes_no_rules_and_everything_is_allowed(self, safe_hosts, status):
        (r, _), _ctx = await self.fetch({"https://x.test/robots.txt": _Resp(status)})
        assert r.allows("/private")

    async def test_a_200_that_is_not_a_robots_file_restricts_nothing(self, safe_hosts):
        (r, _), _ctx = await self.fetch({"https://x.test/robots.txt": _Resp(200, "<html><body>Welcome</body></html>")})
        assert r.allows("/private")

    async def test_more_than_five_redirects_is_unavailable_not_unreadable(self, safe_hosts):
        table = {f"https://x.test/{'r' * i}": _Resp(302, location=f"https://x.test/{'r' * (i + 1)}") for i in range(1, 20)}
        table["https://x.test/robots.txt"] = _Resp(302, location="https://x.test/r")
        (r, _), ctx = await self.fetch(table)
        assert r.allows("/private") and len(ctx.asked) <= 6

    # -- permission cannot be determined: the default is to not scan --
    @pytest.mark.parametrize("answer", [_Resp(500), _Resp(503), _Resp(429), TimeoutError("Timeout 5000ms exceeded"), OSError("net::ERR_CONNECTION_RESET")])
    async def test_an_unreadable_robots_txt_stops_the_scan_by_default(self, safe_hosts, answer):
        from app.scanner.failures import RobotsUnreadable
        with pytest.raises(RobotsUnreadable) as caught:
            await self.fetch({"https://x.test/robots.txt": answer})
        assert "Nothing was assessed" in str(caught.value) and "x.test" in str(caught.value)
        assert getattr(caught.value, "retryable", True) is True          # the queue retries it with backoff

    async def test_a_redirect_to_another_site_cannot_be_read_so_it_stops_the_scan(self, safe_hosts):
        from app.scanner.failures import RobotsUnreadable
        with pytest.raises(RobotsUnreadable, match="another site"):
            await self.fetch({"https://shop.example.com/robots.txt": _Resp(302, location="https://evil.org/robots.txt")}, root="https://shop.example.com/")

    async def test_a_redirect_to_an_unsafe_address_is_never_requested_and_stops_the_scan(self, monkeypatch):
        from app.core.exceptions import ScanAuthorizationError
        from app.scanner.crawler import _fetch_robots
        from app.scanner.failures import RobotsUnreadable

        async def refuse(url):
            raise ScanAuthorizationError("blocked")
        monkeypatch.setattr("app.scanner.crawler.assert_safe_url", refuse)
        ctx = _RobotsContext({"https://x.test/robots.txt": _Resp(302, location="https://internal.x.test/robots.txt")})
        with pytest.raises(RobotsUnreadable):
            await _fetch_robots(ctx, "https://x.test/")
        assert ctx.asked == ["https://x.test/robots.txt"]

    @pytest.mark.parametrize("answer", [_Resp(503), TimeoutError("Timeout 5000ms exceeded")])
    async def test_with_the_allow_policy_the_scan_proceeds_unrestricted_and_the_log_says_so(self, safe_hosts, allow_policy, caplog, answer):
        import logging
        with caplog.at_level(logging.WARNING, logger="app.scanner.crawler"):
            (r, _), _ctx = await self.fetch({"https://x.test/robots.txt": answer})
        assert r.allows("/private")
        assert any("robots.txt" in m and "not applied" in m for m in caplog.messages)

    async def test_the_allow_policy_does_not_change_what_a_readable_file_forbids(self, safe_hosts, allow_policy):
        (r, _), _ctx = await self.fetch({"https://x.test/robots.txt": _Resp(200, self.BLOCK)})
        assert not r.allows("/private/x")


class TestDetails:
    def test_only_the_scanners_own_name_selects_its_group_not_a_prefix_of_it(self):
        assert rules("User-agent: c\nDisallow: /x\n").allows("/x")        # "c" is not this scanner; no group applies, so nothing is restricted

    def test_paths_are_case_sensitive(self):
        r = rules("User-agent: *\nDisallow: /Private\n")
        assert not r.allows("/Private") and r.allows("/private")

    def test_percent_encoding_is_compared_after_normalising(self):
        r = rules("User-agent: *\nDisallow: /a%62c\nDisallow: /d/%7euser\n")
        assert not r.allows("/abc") and not r.allows("/d/~user")


class TestWhyASiteCouldNotBeReached:
    @pytest.mark.parametrize("error,words", [
        ("net::ERR_NAME_NOT_RESOLVED at https://x.test/", "DNS"),
        ("net::ERR_CONNECTION_REFUSED at https://x.test/", "accepting connections"),
        ("net::ERR_PROXY_CONNECTION_FAILED", "proxy"),
        ("Timeout 30000ms exceeded.", "did not answer in time"),
        ("net::ERR_CONNECTION_RESET", "cut the connection"),
        ("net::ERR_CERT_DATE_INVALID", "certificate"),
    ])
    def test_the_explanation_names_the_likely_cause(self, error, words):
        from app.scanner.failures import explain_seed_failure
        assert words in str(explain_seed_failure("https://x.test/", RuntimeError(error), 3))

    def test_an_unrecognised_error_gets_no_invented_cause(self):
        from app.scanner.failures import likely_cause
        assert likely_cause("something odd") == ""


class TestACertificateProblemIsPermanentNotUnreadable:
    """Seen live on uat.consiva.ai: the robots.txt request failed with 'certificate has expired'. That is not a file to retry later; no retry can make the certificate valid."""

    @pytest.mark.parametrize("message", [
        "APIRequestContext.get: certificate has expired", "APIRequestContext.get: self signed certificate", "APIRequestContext.get: unable to verify the first certificate",
        "APIRequestContext.get: unable to get local issuer certificate", "net::ERR_CERT_AUTHORITY_INVALID",
    ])
    async def test_it_stops_the_scan_for_good_with_the_cause(self, safe_hosts, message):
        from app.scanner.crawler import _fetch_robots
        ctx = _RobotsContext({"https://x.test/robots.txt": OSError(message)})
        with pytest.raises(ScanNotPossible) as caught:
            await _fetch_robots(ctx, "https://x.test/")
        assert caught.value.code == "certificate_invalid" and caught.value.retryable is False
        text = str(caught.value)
        assert "Could not reach the site at all" in text and "valid security certificate" in text and "nothing was assessed" in text

    async def test_other_failures_are_still_retryable_unreadable(self, safe_hosts):
        from app.scanner.crawler import _fetch_robots
        from app.scanner.failures import RobotsUnreadable
        with pytest.raises(RobotsUnreadable):
            await _fetch_robots(_RobotsContext({"https://x.test/robots.txt": OSError("socket hang up")}), "https://x.test/")


class TestASlowRobotsTxt:
    """in.louisvuitton.com answered robots.txt with HTTP 200 after 8.3 s; a fixed 5 s timeout failed the scan."""

    class SlowThenFine(_RobotsContext):
        def __init__(self, table, timeouts_first):
            super().__init__(table)
            self.timeouts_first, self.timeouts_given, self.seen_timeouts = timeouts_first, 0, []

        async def get(self, url, timeout=0, max_redirects=0):
            self.seen_timeouts.append(timeout)
            if self.timeouts_given < self.timeouts_first:
                self.timeouts_given += 1
                raise TimeoutError("APIRequestContext.get: Timeout 15000ms exceeded.")
            return await super().get(url, timeout=timeout, max_redirects=max_redirects)

    def test_the_default_wait_is_15_seconds_and_configurable(self):
        f = type(get_settings()).model_fields["scanner_robots_timeout_seconds"]
        assert f.default == 15

    async def test_the_configured_timeout_is_used(self, safe_hosts):
        from app.scanner.crawler import _fetch_robots
        ctx = self.SlowThenFine({"https://x.test/robots.txt": _Resp(200, "User-agent: *\nDisallow: /mylv/\n")}, timeouts_first=0)
        await _fetch_robots(ctx, "https://x.test/")
        assert ctx.seen_timeouts == [get_settings().scanner_robots_timeout_seconds * 1000]

    async def test_one_timeout_is_retried_and_the_file_is_then_obeyed(self, safe_hosts):
        from app.scanner.crawler import _fetch_robots
        ctx = self.SlowThenFine({"https://x.test/robots.txt": _Resp(200, "User-agent: *\nDisallow: /mylv/\n")}, timeouts_first=1)
        rules, _ = await _fetch_robots(ctx, "https://x.test/")
        assert len(ctx.seen_timeouts) == 2 and not rules.allows("/mylv/x") and rules.allows("/eng-in/")

    async def test_two_timeouts_still_stop_the_scan_it_is_never_taken_as_permission(self, safe_hosts):
        from app.scanner.crawler import _fetch_robots
        from app.scanner.failures import RobotsUnreadable
        ctx = self.SlowThenFine({"https://x.test/robots.txt": _Resp(200, "")}, timeouts_first=2)
        with pytest.raises(RobotsUnreadable):
            await _fetch_robots(ctx, "https://x.test/")
        assert len(ctx.seen_timeouts) == 2, "one extra try, not a loop"

    async def test_a_non_timeout_error_is_not_retried(self, safe_hosts):
        from app.scanner.crawler import _fetch_robots
        from app.scanner.failures import RobotsUnreadable
        ctx = _RobotsContext({"https://x.test/robots.txt": OSError("socket hang up")})
        with pytest.raises(RobotsUnreadable):
            await _fetch_robots(ctx, "https://x.test/")
        assert len(ctx.asked) == 1
