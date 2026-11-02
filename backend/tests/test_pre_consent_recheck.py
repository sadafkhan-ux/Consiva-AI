"""The clean re-check of what fired before consent (FR-20261027-D200 against -1DB8: one website, two results).

The crawl reads several pages at once; the Consiva SDK gives its banner 800 ms to load and, when it misses that, draws none and lets the tags run. So an observation made under
load is confirmed by loading each page alone in a fresh context. Every crawl observation is kept; the re-check adds an answer, and the score reads it:

  fires again            -> Not Met, reproducible
  does not fire again    -> Requires Review (Partially Met), the first observation kept as evidence
  could not be loaded    -> unknown, so the original result (Not Met) stands"""

import asyncio
from types import SimpleNamespace

from app.config import get_settings
from app.rules.consent_rules import evaluate_consent_rules
from app.scanner import crawler
from app.scanner.schemas import CookieRecord, PageRecord, TrackerRecord
from app.scoring.dpdp_score import assess
from app.scoring.review import build_human_review
from tests.test_metric_consistency import TODAY, cookie, evidence, tracker

GA = "https://www.google-analytics.com/g/collect?v=2&tid=G-X"
PAGES = [PageRecord(local_id="p0", url="https://x.test/", discovered_via="seed"), PageRecord(local_id="p1", url="https://x.test/terms", discovered_via="sitemap"),
         PageRecord(local_id="p2", url="https://x.test/dead", discovered_via="failed")]


# ── the scanner side ────────────────────────────────────────────────────────────

def _tracker(states):
    return TrackerRecord(local_id="t", script_src=GA, consent_states=states)


def _run(coro):
    return asyncio.run(coro)


def test_nothing_is_rechecked_when_nothing_fired_before_consent(monkeypatch):
    async def boom(*_a, **_k):
        raise AssertionError("no page should be loaded")

    monkeypatch.setattr(crawler, "_recheck_one_page", boom)
    assert _run(crawler._recheck_pre_consent(None, PAGES, [], [_tracker(["post_accept"])], "https://x.test/", get_settings(), set())) is None


def test_every_scanned_page_is_rechecked_one_after_another_and_failed_attempts_are_not(monkeypatch):
    running, peak, seen = 0, 0, []

    async def fake(_browser, url, *_a):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0)
        running -= 1
        seen.append(url)
        return {"url": url, "loaded": True, "fired": url.endswith("/terms"), "requests": 1, "cookies": 0}

    monkeypatch.setattr(crawler, "_recheck_one_page", fake)
    out = _run(crawler._recheck_pre_consent(None, PAGES, [], [_tracker(["pre_consent", "post_accept"])], "https://x.test/", get_settings(), set()))
    assert seen == ["https://x.test/", "https://x.test/terms"] and peak == 1
    assert out["reproduced"] is True and out["complete"] is True and out["crawl_hits"] == 1
    assert out["checked_at"]


def test_a_page_that_did_not_load_makes_the_recheck_incomplete(monkeypatch):
    async def fake(_browser, url, *_a):
        return {"url": url, "loaded": url != "https://x.test/terms", "fired": False, "requests": 0, "cookies": 0}

    monkeypatch.setattr(crawler, "_recheck_one_page", fake)
    out = _run(crawler._recheck_pre_consent(None, PAGES, [], [_tracker(["pre_consent"])], "https://x.test/", get_settings(), set()))
    assert out["reproduced"] is False and out["complete"] is False


def test_the_cookie_database_classification_is_not_needed_to_trigger_a_recheck(monkeypatch):
    """A tracking cookie the scanner's own catalogue knows (_ga) is a hit by itself."""
    async def fake(_browser, url, *_a):
        return {"url": url, "loaded": True, "fired": False, "requests": 0, "cookies": 0}

    monkeypatch.setattr(crawler, "_recheck_one_page", fake)
    ga = CookieRecord(local_id="c", name="_ga", domain="x.test", consent_states=["pre_consent"])
    assert _run(crawler._recheck_pre_consent(None, PAGES, [ga], [], "https://x.test/", get_settings(), set()))["crawl_hits"] == 1


class _Page:
    def __init__(self, requests):
        self._requests, self._handler = requests, None

    def on(self, _event, handler):
        self._handler = handler

    async def wait_for_timeout(self, _ms):
        for url, kind in self._requests:
            self._handler(SimpleNamespace(url=url, resource_type=kind))


class _Context:
    def __init__(self, log, requests):
        self.log, self._requests = log, requests

    async def route(self, *_a):
        pass

    async def new_page(self):
        return _Page(self._requests)

    async def cookies(self):
        return []

    async def close(self):
        self.log.append("closed")


class _Browser:
    def __init__(self, requests):
        self.log, self._requests = [], requests

    async def new_context(self, **_k):
        self.log.append("new")
        return _Context(self.log, self._requests)


def _patch_page_loading(monkeypatch):
    async def ok(*_a, **_k):
        return None, "success", None, 1

    async def nothing(*_a, **_k):
        return None

    monkeypatch.setattr(crawler, "_navigate_with_retry", ok)
    monkeypatch.setattr(crawler, "_wait_for_load_event", nothing)
    monkeypatch.setattr(crawler, "_progressive_scroll", nothing)


def test_each_load_gets_its_own_fresh_context_that_is_closed(monkeypatch):
    _patch_page_loading(monkeypatch)
    browser = _Browser([(GA, "xhr")])
    for url in ("https://x.test/", "https://x.test/terms"):
        out = _run(crawler._recheck_one_load(browser, url, "https://x.test/", get_settings(), set()))
        assert out["loaded"] and out["fired"] and out["requests"] == 1
    assert browser.log == ["new", "closed", "new", "closed"]


def test_a_clean_load_that_fires_nothing_is_recorded_as_not_fired(monkeypatch):
    _patch_page_loading(monkeypatch)
    browser = _Browser([("https://x.test/app.css", "stylesheet")])
    out = _run(crawler._recheck_one_page(browser, "https://x.test/", "https://x.test/", get_settings(), set()))
    assert out["loaded"] is True and out["fired"] is False and (out["loads"], out["fired_loads"]) == (1, 0)
    assert browser.log == ["new", "closed"]          # nothing fired, so no extra loads were spent


def test_a_page_that_fires_every_time_is_loaded_three_times_and_fired(monkeypatch):
    _patch_page_loading(monkeypatch)
    browser = _Browser([(GA, "xhr")])
    out = _run(crawler._recheck_one_page(browser, "https://x.test/", "https://x.test/", get_settings(), set()))
    assert (out["loads"], out["fired_loads"], out["fired"]) == (3, 3, True)
    assert browser.log == ["new", "closed"] * 3


def test_a_single_blip_is_recorded_as_a_blip_not_as_a_repeat(monkeypatch):
    """swaransoft.com, 28 Oct 2026: the first clean load fired and none of the next eight did."""
    _patch_page_loading(monkeypatch)
    answers = iter([[(GA, "xhr")], [], []])

    class Flaky(_Browser):
        async def new_context(self, **_k):
            self.log.append("new")
            return _Context(self.log, next(answers))

    out = _run(crawler._recheck_one_page(Flaky([]), "https://x.test/", "https://x.test/", get_settings(), set()))
    assert (out["loads"], out["fired_loads"], out["fired"]) == (3, 1, False)


def test_a_page_that_fires_in_two_of_three_loads_counts_as_firing(monkeypatch):
    _patch_page_loading(monkeypatch)
    answers = iter([[(GA, "xhr")], [(GA, "xhr")], []])

    class Flaky(_Browser):
        async def new_context(self, **_k):
            self.log.append("new")
            return _Context(self.log, next(answers))

    out = _run(crawler._recheck_one_page(Flaky([]), "https://x.test/", "https://x.test/", get_settings(), set()))
    assert (out["loads"], out["fired_loads"], out["fired"]) == (3, 2, True)


def test_a_page_that_cannot_be_loaded_is_unknown_not_clean(monkeypatch):
    async def fail(*_a, **_k):
        return None, "failed", RuntimeError("boom"), 2

    monkeypatch.setattr(crawler, "_navigate_with_retry", fail)
    out = _run(crawler._recheck_one_page(_Browser([]), "https://x.test/", "https://x.test/", get_settings(), set()))
    assert out["loaded"] is False and out["fired"] is False and out["loads"] == 0


# ── the score side ──────────────────────────────────────────────────────────────

def _scan(recheck):
    ev = evidence([tracker(i) for i in range(6)], [cookie(i) for i in range(3)])
    if recheck is not None:
        ev["consent_signals"][0]["evidence"]["pre_consent_recheck"] = recheck
    return ev


def _page(fired, loaded=True):
    return {"url": "https://x.test/", "loaded": loaded, "fired": fired, "loads": 3 if fired else 1, "fired_loads": 3 if fired else 0, "requests": int(fired), "cookies": 0}


def _c1(ev):
    return next(r for r in assess(ev, TODAY).requirements if r.id == "DPDP-C1")


def test_without_a_recheck_the_result_is_what_it_always_was():
    c1 = _c1(_scan(None))
    assert c1.status == "not_met" and c1.points_earned == 0


def test_a_reproduced_hit_is_not_met_and_says_so():
    c1 = _c1(_scan({"crawl_hits": 9, "reproduced": True, "complete": True, "pages": [_page(True), _page(False)]}))
    assert c1.status == "not_met" and "reproducible" in c1.observations[0] and "1 of 2" in c1.observations[0] and "3 of 4 clean load(s) fired" in c1.observations[0]


def test_a_hit_that_does_not_repeat_needs_review_and_keeps_its_evidence():
    c1 = _c1(_scan({"crawl_hits": 9, "reproduced": False, "complete": True, "pages": [_page(False), _page(False)]}))
    assert c1.status == "partially_met" and c1.points_earned == 2.5
    assert "6 analytics/marketing request(s) and 3 analytics/marketing cookie(s)" in c1.observations[0]
    assert "did not fire in most clean loads" in c1.observations[0] and "0 of 2 clean load(s) fired" in c1.observations[0] and len(c1.evidence_ids) == 9


def test_an_incomplete_recheck_does_not_clear_anything():
    c1 = _c1(_scan({"crawl_hits": 9, "reproduced": False, "complete": False, "pages": [_page(False), _page(False, loaded=False)]}))
    assert c1.status == "not_met"


def test_items_the_scanner_did_not_put_to_the_recheck_are_not_cleared():
    """The report classified 9 items; the scanner's re-check was only told about 4."""
    c1 = _c1(_scan({"crawl_hits": 4, "reproduced": False, "complete": True, "pages": [_page(False)]}))
    assert c1.status == "not_met"


def test_the_rule_finding_states_the_recheck_result():
    summary = next(f for f in evaluate_consent_rules(_scan({"crawl_hits": 9, "reproduced": False, "complete": True, "pages": [_page(False)]})) if f.rule_id == "R-001").summary
    assert "did not fire again when each of the 1 page(s) was loaded alone" in summary
    summary = next(f for f in evaluate_consent_rules(_scan({"crawl_hits": 9, "reproduced": True, "complete": True, "pages": [_page(True)]})) if f.rule_id == "R-001").summary
    assert "fired again when 1 of 1 page(s)" in summary


def test_a_review_item_explains_the_one_off_instead_of_calling_it_unclassified():
    from app.rules.consent_rules import build_consent_context

    ev = _scan({"crawl_hits": 9, "reproduced": False, "complete": True, "pages": [_page(False)]})
    score = assess(ev, TODAY)
    review = build_human_review(build_consent_context(ev), score.requirements, TODAY, 5)
    ids = [i.id for i in review.items]
    assert "pre-consent-not-reproduced" in ids and "unclassified-before-consent" not in ids


def test_the_scores_stay_the_same_for_the_same_evidence():
    ev = _scan({"crawl_hits": 9, "reproduced": False, "complete": True, "pages": [_page(False)]})
    assert assess(ev, TODAY).readiness.score_percent == assess(ev, TODAY).readiness.score_percent
