"""A consent tool whose own script cannot be loaded never shows a banner. The scan says so, with the error, instead of only "no banner was shown to the scanner".

Seen live: https://api.consiva.ai/sdk.js failed with net::ERR_CERT_DATE_INVALID (an expired certificate) on swaransoft.com, gignaati.com, hireflow.gignaati.com and
masterclass.gignaati.com; the report blamed the scanner's vantage point.
"""

import pytest

from app.config import get_settings
from app.rules.consent_observation import cmp_script_failure, script_failure_note
from app.rules.consent_rules import build_consent_context, evaluate_consent_rules
from app.scanner import crawler
from app.scoring.dpdp_score import assess
from tests._browser import run_with_browser
from tests.test_consent_consistency import TODAY, evidence, results, script
from tests.test_report_pdf_api import needs_chromium

from app.rules.tracker_catalog import CMP_CATALOG  # isort: skip
from app.scanner.consent_signal_detector import detect_consent_signal  # isort: skip

FAILURE = {"url": "https://api.consiva.ai/sdk.js", "error": "net::ERR_CERT_DATE_INVALID"}


def failed_tool_evidence(cmp=None, failure=FAILURE):
    cmp = cmp or CMP_CATALOG[0]
    signal = detect_consent_signal(scripts=[script(cmp)], visible_text="", detected_global_vars=[], control_text="")
    ev = evidence(signal, accept="cmp_not_automatable", reject="cmp_not_automatable")
    if failure:
        ev["consent_signals"][0]["evidence"]["cmp_script_failure"] = failure
    return ev


class TestTheSharedHelpers:
    def test_a_recorded_failure_is_read_and_nothing_else_is(self):
        assert cmp_script_failure({"cmp_script_failure": FAILURE}) == FAILURE
        assert cmp_script_failure({"cmp_script_failure": {"url": "https://x/s.js"}})["error"] == "the request failed"
        for bad in (None, {}, {"cmp_script_failure": None}, {"cmp_script_failure": {"error": "x"}}, {"cmp_script_failure": "oops"}):
            assert cmp_script_failure(bad) is None

    def test_the_note_names_the_script_the_error_and_what_to_check(self):
        note = script_failure_note({"cmp_script_failure": FAILURE})
        assert "https://api.consiva.ai/sdk.js" in note and "ERR_CERT_DATE_INVALID" in note and "no banner" in note and "certificate" in note
        assert script_failure_note({}) == ""


@pytest.mark.parametrize("cmp", CMP_CATALOG, ids=lambda c: c.cmp_vendor)
class TestEveryConsumerSaysWhy:
    def test_the_consent_rule_states_the_cause(self, cmp):
        found = {f.rule_id: f for f in evaluate_consent_rules(failed_tool_evidence(cmp))}
        assert "ERR_CERT_DATE_INVALID" in found["R-009"].summary and "could not be loaded" in found["R-009"].summary
        assert found["R-009"].confidence == "low" or found["R-009"].risk_level == "medium"

    def test_the_score_still_says_unable_to_verify_and_gives_the_cause(self, cmp):
        by_id = results(assess(failed_tool_evidence(cmp), TODAY))
        for rid in ("DPDP-C3", "DPDP-C4", "DPDP-C5"):
            assert by_id[rid].status == "unable_to_verify", rid
            assert any("ERR_CERT_DATE_INVALID" in o for o in by_id[rid].observations), rid

    def test_without_a_failure_the_wording_is_unchanged(self, cmp):
        by_id = results(assess(failed_tool_evidence(cmp, failure=None), TODAY))
        assert not any("could not be loaded" in o for o in by_id["DPDP-C4"].observations)
        assert build_consent_context(failed_tool_evidence(cmp, failure=None)).banner_not_displayed


@needs_chromium
class TestTheCrawlerRecordsIt:
    """A real browser, with the network replaced: the page is served, the consent tool's script fails."""

    def fetch(self, script_behaviour):
        html = '<html><head><title>x</title><script src="https://cdn.cookielaw.org/scripttemplates/otSDKStub.js"></script></head><body><a href="/privacy">Privacy</a></body></html>'

        async def go():
            from playwright.async_api import async_playwright
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True)
                context = await browser.new_context()

                async def handle(route):
                    url = route.request.url
                    if url.startswith("https://shop.test/"):
                        await route.fulfill(status=200, content_type="text/html", body=html)
                    elif "cookielaw.org" in url:
                        await script_behaviour(route)
                    else:
                        await route.fulfill(status=200, content_type="text/plain", body="")
                await context.route("**/*", handle)
                result = await crawler._fetch_one_page(context, "https://shop.test/", "https://shop.test/", get_settings().model_copy(update={"scanner_timeout_seconds": 20}))
                await browser.close()
                return result
        return run_with_browser(go)

    def test_a_script_that_cannot_be_fetched_is_recorded_with_the_error(self):
        async def abort(route):
            await route.abort("connectionfailed")
        result = self.fetch(abort)
        assert [f["url"] for f in result["cmp_script_failures"]] == ["https://cdn.cookielaw.org/scripttemplates/otSDKStub.js"]
        assert "ERR_" in result["cmp_script_failures"][0]["error"]

    def test_a_script_answered_with_an_http_error_is_recorded(self):
        async def not_found(route):
            await route.fulfill(status=404, content_type="text/plain", body="nope")
        result = self.fetch(not_found)
        assert result["cmp_script_failures"] and result["cmp_script_failures"][0]["error"] == "HTTP 404"

    def test_a_script_that_loads_is_not_a_failure(self):
        async def ok(route):
            await route.fulfill(status=200, content_type="application/javascript", body="/* ok */")
        assert self.fetch(ok)["cmp_script_failures"] == []
