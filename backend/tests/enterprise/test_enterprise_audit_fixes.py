"""The Enterprise fixes from the audit of 16 Nov 2026: accordions that were never opened, oversize pages, duplicates, per-page tracking, dropped URLs.

The browser tests use the local Chromium on a page built in memory (`set_content`): no network, no customer data. They are skipped, not failed, where no browser is installed.
"""
import asyncio
import sys
import types

import pytest

from app.enterprise.analysis import pages as pg
from app.enterprise.analysis import pipeline as pl
from app.enterprise.analysis import sources as sr
from app.enterprise.analysis.chunking import Chunk
from app.enterprise.analysis.repo import ChunkRow
from app.enterprise.crawl import discovery, runner
from app.enterprise.crawl import page_inspect as pi
from app.enterprise.evidence import store as ev


# ---- B3: a page over the evidence limit is kept in parts, never dropped -------------------------------------------------------------------------------------------

def test_a_text_over_the_limit_is_split_into_parts_that_join_back_to_exactly_the_text():
    text = ("First paragraph about data we collect. " * 40 + "\n\n") * 60
    parts = ev.split_for_storage(text, limit_bytes=5000)
    assert len(parts) > 1
    assert all(len(p.encode("utf-8")) <= 5000 for p in parts)
    assert "".join(parts) == text                                   # nothing is lost between parts


def test_a_text_with_multibyte_characters_is_split_by_bytes_not_characters():
    text = "गोपनीयता नीति " * 2000
    parts = ev.split_for_storage(text, limit_bytes=4000)
    assert all(len(p.encode("utf-8")) <= 4000 for p in parts) and "".join(parts) == text


def test_a_text_that_fits_is_one_part():
    assert ev.split_for_storage("short", limit_bytes=100) == ["short"]


# ---- notes: what limited the read of a page ------------------------------------------------------------------------------------------------------------------------

def _page(**kw):
    base = dict(scroll={"stopped": "bottom"}, expanded={"opened": 3, "candidates": 3}, dom={"shadow": [], "iframes": [], "frame_text": []})
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_a_page_read_in_full_has_no_notes():
    assert runner._inspection_notes(_page(), "https://acme.com/") == []


def test_every_limit_that_applied_is_named():
    page = _page(scroll={"stopped": "deadline"}, expanded={"opened": 30, "candidates": 45},
                 dom={"shadow": ["x" * pi.SHADOW_TEXT_LIMIT], "iframes": [{"src": "https://vendor.example.net/policy"}, {"src": "https://acme.com/frame"}], "frame_text": [{"src": "https://acme.com/frame", "text": "t"}]})
    notes = " | ".join(runner._inspection_notes(page, "https://www.acme.com/"))
    assert "deadline" in notes and "15 of 45 collapsed sections were not opened" in notes and "cut at" in notes
    assert "1 iframe(s) from other sites were not read" in notes and "vendor.example.net" in notes


# ---- tracking: why a page was or was not analysed -----------------------------------------------------------------------------------------------------------------

def _loaded(*pairs):
    loaded = sr.Loaded()
    for url, cat, reasons, unresolved in pairs:
        page = sr.Page(url, cat, "high", reasons, "T", [], (), unresolved=unresolved)
        page.frontier_ids.add(f"fid-{url}")
        loaded.pages[url] = page
    return loaded


def _work(url, n, statuses):
    work, chunks = [], {}
    for i in range(n):
        src = sr.Src(f"e{i}", url, "page_text", 0, "x", None, pg.PRIVACY)
        row = ChunkRow("page", url, i, n, f"e{i}")
        work.append(pl._Work(row, src, Chunk(i, n, 0, 1, 0, "x")))
        chunks[row.key] = ChunkRow("page", url, i, n, f"e{i}", statuses[i], error="boom" if statuses[i] == "failed" else None)
    return work, chunks


def test_a_page_the_model_never_saw_says_why():
    loaded = _loaded(("https://a.com/blog/x", pg.ARTICLE, ("editorial path",), False), ("https://a.com/p", pg.OTHER, (), True))
    out = pl._page_tracking(loaded, {}, [])
    assert "classified as blog article (editorial path)" in out["fid-https://a.com/blog/x"]["note"]
    assert "could not be classified" in out["fid-https://a.com/p"]["note"]
    assert out["fid-https://a.com/p"]["created"] == 0 and out["fid-https://a.com/p"]["analysed"] == 0


def test_a_page_with_failed_passages_says_how_many_and_why():
    url = "https://a.com/privacy"
    work, chunks = _work(url, 3, ["done", "failed", "done"])
    out = pl._page_tracking(_loaded((url, pg.PRIVACY, ("slug",), False)), chunks, work)[f"fid-{url}"]
    assert (out["created"], out["analysed"]) == (3, 2) and "1 of 3 passages could not be analysed (boom)" in out["note"]


def test_a_page_analysed_in_full_has_no_note():
    url = "https://a.com/privacy"
    work, chunks = _work(url, 2, ["done", "done"])
    out = pl._page_tracking(_loaded((url, pg.PRIVACY, ("slug",), False)), chunks, work)[f"fid-{url}"]
    assert (out["created"], out["analysed"], out["note"]) == (2, 2, None)


# ---- B7: URLs that could not be recorded are counted --------------------------------------------------------------------------------------------------------------

def test_links_after_the_record_cap_are_counted_not_dropped_without_a_trace(monkeypatch):
    calls = []

    async def fake_add(db, scan_id, url, **kw):
        calls.append(url)
        from app.enterprise.crawl import frontier
        return frontier.URL_LIMIT if len(calls) > 2 else frontier.ADDED

    monkeypatch.setattr(discovery.frontier, "add_url", fake_add)
    links = [f"https://acme.com/p{i}" for i in range(6)] + ["https://other.org/x", "mailto:a@b.c"]
    out = asyncio.run(discovery.add_links(None, None, "https://acme.com/", links, parent_depth=0, max_urls=2, max_depth=9))
    assert out["added"] == 2 and out["limit"] == 4                  # the 4 in-scope links from the first refusal on; the other site and the mailto: are not counted


# ---- the browser: accordions (B2), shadow DOM and iframes (B4) ----------------------------------------------------------------------------------------------------------

ACCORDION = """
<html><body>
<details><summary>Information we collect</summary><p>PANEL-COLLECT</p></details>
<details><summary>Your right to delete your data</summary><p>PANEL-DELETE</p></details>
<button aria-expanded="false" aria-controls="p3" onclick="document.getElementById('p3').hidden=false;this.setAttribute('aria-expanded','true')">How we send data to partners</button>
<div id="p3" hidden>PANEL-SEND</div>
<button aria-expanded="false" aria-controls="p4" onclick="document.getElementById('p4').hidden=false;this.setAttribute('aria-expanded','true')">Remove your information</button>
<div id="p4" hidden>PANEL-REMOVE</div>
<button aria-expanded="false" onclick="document.body.setAttribute('data-out','1')">Log out</button>
<details><summary>Outer section</summary><details><summary>Inner section</summary><p>PANEL-INNER</p></details></details>
</body></html>
"""


@pytest.fixture
def browser_page():
    pw = pytest.importorskip("playwright.async_api")

    async def go(html, body):
        async with pw.async_playwright() as p:
            try:
                browser = await p.chromium.launch()
            except Exception as exc:                                  # no browser installed here
                pytest.skip(f"no Chromium: {exc}")
            page = await browser.new_page()
            await page.set_content(html)
            try:
                return await body(page)
            finally:
                await browser.close()
    def run(html, body):
        # A browser needs an event loop that can start a subprocess. Another test may have set a selector-loop policy on Windows, so the loop is made here rather than taken from the policy.
        loop = asyncio.ProactorEventLoop() if sys.platform == "win32" else asyncio.new_event_loop()
        try:
            return loop.run_until_complete(go(html, body))
        finally:
            loop.close()
    return run


def test_policy_sections_about_deleting_removing_or_sending_data_are_opened(browser_page):
    async def body(page):
        result = await pi.expand_sections(page)
        return result, await page.evaluate("document.body.innerText"), await page.evaluate("document.body.getAttribute('data-out')")
    result, text, logged_out = browser_page(ACCORDION, body)
    for panel in ("PANEL-COLLECT", "PANEL-DELETE", "PANEL-SEND", "PANEL-REMOVE"):
        assert panel in text, panel
    assert logged_out is None                                        # a control that logs the visitor out is still left alone
    assert result["opened"] >= 4


def test_a_section_inside_a_section_is_opened_by_the_next_round(browser_page):
    async def body(page):
        result = await pi.expand_sections(page)
        return result, await page.evaluate("document.body.innerText")
    result, text = browser_page(ACCORDION, body)
    assert "PANEL-INNER" in text and result["rounds"] >= 2


def test_long_shadow_dom_text_is_kept_past_the_old_20000_character_cut(browser_page):
    html = "<html><body><div id=h></div><script>const r=document.getElementById('h').attachShadow({mode:'open'});r.innerHTML='<p>'+'policy words '.repeat(5000)+'END-MARK</p>';</script></body></html>"

    async def body(page):
        return await pi.collect_dom(page, "https://acme.com/")
    dom = browser_page(html, body)
    assert len(dom["shadow"][0]) > 60000 and dom["shadow"][0].endswith("END-MARK")
