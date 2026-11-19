"""Deep sequential scanning (owner's requirements, 19 Nov 2026): the homepage first with the 5-page agent's first-page sequence, then privacy policies, then the rest, ONE URL at a time;
the blocking rule; evidence fingerprints; consent sessions one at a time; the same passage read by the model once per scan; the report after the workflow."""
import ast
import asyncio
import inspect as pyinspect
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.enterprise.analysis import constants as K
from app.enterprise.analysis.pipeline import run_text_analysis
from app.enterprise.consent import run as consent_run
from app.enterprise.crawl import inspect as deep
from app.enterprise.crawl import runner
from app.monitoring import blocks

from .test_enterprise_analysis_support import AS_OF, ORG, POLICY, SCAN, FakeLLM, FakeRepo, ev, site

RUNNER = Path(runner.__file__).read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(K, "LLM_BACKOFF_S", (0.0,))


# ---- strictly sequential, homepage first ------------------------------------------------------------------------------------------------------------------------------

def _run_source() -> str:
    tree = ast.parse(RUNNER)
    fn = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "run")
    return ast.get_source_segment(RUNNER, fn)


def test_the_crawl_opens_one_url_at_a_time_whatever_the_concurrency_option():
    src = _run_source()
    assert "asyncio.gather" not in src and "limit=concurrency" not in src
    assert "claim_next(db, scan_id, worker_id, limit=1)" in src
    assert 'options.get("concurrency"' not in src                      # the option stays in the scan record for the API, but the crawl never reads it


def test_the_homepage_is_claimed_and_its_consent_tests_run_before_any_other_page():
    src = _run_source()
    seed, consent, loop = src.index("frontier.claim_seed("), src.index("_keep_consent("), src.index("frontier.claim_next(")
    assert seed < consent < loop


def test_the_consent_tests_run_one_browser_session_at_a_time():
    assert "max_parallel=1" in pyinspect.getsource(runner._keep_consent)


async def test_run_consent_suite_with_max_parallel_one_never_overlaps_sessions(monkeypatch):
    live, peak = 0, 0

    async def fake(*a, **k):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0.01)
        live -= 1
        return {"tested": True}

    for name in ("observe_state", "run_reject_persistence", "_withdrawal", "run_purpose_test", "_comparison"):
        monkeypatch.setattr(consent_run, name, fake)
    results = await consent_run.run_consent_suite(None, None, "https://x.test/", [], max_parallel=1)
    assert len(results) == 6 and peak == 1


def test_every_page_uses_the_5_page_agents_first_page_sequence():
    src = pyinspect.getsource(deep.deep_inspect)
    order = ["_navigate_with_retry(", "_wait_for_load_event(", "_TRACKER_SETTLE_MS", "_page_is_a_block_page(", "_page_says_it_was_not_found(", "_capture_homepage_screenshot(",
             "_progressive_scroll(", "expand_sections(", "_FORM_CHECKED_JS", "_STORAGE_KEYS_JS", "_CMP_GLOBAL_VAR_CANDIDATES", "_collect_iframe_signal_evidence(", "_settled_text("]
    positions = [src.index(s) for s in order]
    assert positions == sorted(positions)                           # the 5-page order, then the Enterprise extras
    assert "_watch_set_cookies(" in src and "_drain_set_cookie_reads(" in src
    assert "deep_inspect(" in pyinspect.getsource(runner._process) and "render(" not in pyinspect.getsource(runner._process).replace("deep_inspect(", "")


def test_the_5_page_agent_is_only_imported_never_changed():
    """The deep inspection calls the 5-page helpers from app.scanner; it defines none of them itself (the frozen guard test checks the 5-page files are unchanged)."""
    tree = ast.parse(Path(deep.__file__).read_text(encoding="utf-8"))
    imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module == "app.scanner.crawler" for a in n.names}
    assert {"_navigate_with_retry", "_progressive_scroll", "_capture_homepage_screenshot", "_settled_text"} <= imported
    defined = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert not (defined & imported)


# ---- blocking ---------------------------------------------------------------------------------------------------------------------------------------------------------

ACCESS = blocks.BlockVerdict(True, blocks.ACCESS_BLOCKED, "the site answered HTTP 403 Forbidden instead of a page", None)
RATE = blocks.BlockVerdict(True, blocks.ACCESS_BLOCKED, "the site answered HTTP 429 Too Many Requests instead of a page", None)
CAPTCHA = blocks.BlockVerdict(True, blocks.BOT_BLOCKED, "a CAPTCHA or bot-verification page", None)


@pytest.mark.parametrize("verdict,inspected,in_a_row,seed,stop", [
    (ACCESS, 0, 1, True, True),        # the homepage refused: stop
    (ACCESS, 3, 1, False, True),       # before 10 inspected pages, the first refusal stops at once
    (ACCESS, 9, 1, False, True),
    (ACCESS, 10, 1, False, False),     # after 10: one refused page is that page's outcome
    (ACCESS, 25, 2, False, False),
    (ACCESS, 25, 3, False, True),      # three in a row: the whole site is refusing
    (RATE, 25, 1, False, True),        # rate limiting is website-wide
    (CAPTCHA, 25, 1, False, True),     # so is a challenge or CAPTCHA
])
def test_the_blocking_rule(verdict, inspected, in_a_row, seed, stop):
    assert runner.should_stop_for_block(verdict=verdict, inspected=inspected, in_a_row=in_a_row, seed=seed) is stop


def test_403_429_and_captcha_are_recognised_as_blocking():
    assert deep._verdict_for_failure(403, None).blocked and deep._verdict_for_failure(429, None).blocked
    assert blocks.detect_block(200, {}, "Just a moment...", "Checking your browser before accessing").blocked
    assert deep._verdict_for_failure(500, None) is None and deep._verdict_for_failure(None, TimeoutError("timed out")) is None


def test_a_blocked_stop_names_the_url_and_leaves_every_unreached_url_with_an_outcome():
    src = _run_source()
    assert 'f"{v.kind} at {b[\'url\']}' in src and src.count("mark_unreached(") == 3       # blocked, stopped by a limit, and a homepage that redirects to another host


def test_a_scan_stopped_by_blocking_can_be_resumed_and_any_other_final_scan_cannot():
    from app.enterprise import scans
    blocked = SimpleNamespace(status="failed", stop_reason="bot_blocked")
    assert scans.is_blocked_stop(blocked) and scans.is_blocked_stop(SimpleNamespace(status="failed", stop_reason="access_blocked"))
    assert not scans.is_blocked_stop(SimpleNamespace(status="failed", stop_reason=None))
    assert not scans.is_blocked_stop(SimpleNamespace(status="completed", stop_reason="url_limit"))
    from app.enterprise.api import scans as api
    src = pyinspect.getsource(api.resume)
    assert "is_blocked_stop" in src and "frontier.reopen_blocked" in src and "scans.reopen_blocked" in src


# ---- evidence fingerprints --------------------------------------------------------------------------------------------------------------------------------------------

def test_fingerprints_say_what_is_new_on_each_page_and_never_count_a_cookie_twice():
    fp = runner.Fingerprints()
    page1 = fp.of_page([{"name": "_ga", "domain": ".x.test", "path": "/"}], [{"url": "https://www.googletagmanager.com/gtm.js", "type": "script"}],
                       [{"vendor": "Google Analytics", "host": "www.google-analytics.com"}])
    first = fp.new_on(page1)
    assert first == {"cookie": ["_ga|x.test|/"], "tracker": ["google analytics"], "script": ["www.googletagmanager.com"], "script_url": ["https://www.googletagmanager.com/gtm.js"]}
    page2 = fp.of_page([{"name": "_ga", "domain": "x.test", "path": "/"}, {"name": "sid", "domain": "x.test", "path": "/"}], [], [])
    assert fp.new_on(page2) == {"cookie": ["sid|x.test|/"], "tracker": [], "script": [], "script_url": []}          # _ga again is the same cookie; sid is new on this page


def test_a_page_whose_evidence_is_not_in_the_database_is_not_counted_as_done():
    src = pyinspect.getsource(runner._process)
    assert src.index("await db.commit()", src.index("frontier.mark_done(")) < src.index("frontier.missing_evidence(") < src.index("frontier.demote_unverified(")


# ---- the same passage is read by the model once per scan ----------------------------------------------------------------------------------------------------------------

async def test_an_identical_passage_at_two_addresses_is_sent_to_the_model_once():
    base_llm = FakeLLM()
    await run_text_analysis(SCAN, ORG, llm=base_llm, as_of=AS_OF, repo=FakeRepo(*site()))
    base = len([c for c in base_llm.calls if c[0] == "PageExtraction"])
    dup = [ev("page_text", "https://acme.test/privacy", POLICY), ev("page_html", "https://acme.test/privacy", "<title>Privacy Policy | Acme</title><h1>Privacy Policy</h1>")]
    llm = FakeLLM()
    out = await run_text_analysis(SCAN, ORG, llm=llm, as_of=AS_OF, repo=FakeRepo(*site(extra=dup)))
    calls = len([c for c in llm.calls if c[0] == "PageExtraction"])
    assert out.chunks_shared_in_scan >= 1 and calls == base               # the second copy shared the first reading
    assert out.status == "completed"


# ---- the report after the workflow -------------------------------------------------------------------------------------------------------------------------------------

async def test_the_report_is_printed_only_after_crawl_and_analysis(monkeypatch):
    from app.enterprise import jobs
    order = []

    async def crawl(scan_id, org_id):
        order.append("crawl")
        return "completed"

    async def analyse(scan_id, org_id):
        order.append("analysis")
        return "completed"

    async def report(scan_id, org_id):
        order.append("report")

    monkeypatch.setattr(jobs.runner, "run", crawl)
    monkeypatch.setattr(jobs.pipeline, "run_analysis_phase", analyse)
    monkeypatch.setattr(jobs, "_generate_report", report)
    blocked_with_pages = False

    async def was_blocked(scan_id, org_id):
        return blocked_with_pages

    monkeypatch.setattr(jobs, "_blocked_with_pages", was_blocked)
    await jobs.run_enterprise_scan({"scan_id": str(SCAN), "org_id": str(ORG)})
    assert order == ["crawl", "analysis", "report"]

    order.clear()

    async def blocked(scan_id, org_id):
        order.append("crawl")
        return "failed"

    monkeypatch.setattr(jobs.runner, "run", blocked)
    await jobs.run_enterprise_scan({"scan_id": str(SCAN), "org_id": str(ORG)})
    assert order == ["crawl"]                                            # a failed crawl with nothing inspected is not scored or reported

    order.clear()
    blocked_with_pages = True
    await jobs.run_enterprise_scan({"scan_id": str(SCAN), "org_id": str(ORG)})
    assert order == ["crawl", "analysis", "report"]                      # blocked after some pages: those pages are analysed and reported, labelled blocked
