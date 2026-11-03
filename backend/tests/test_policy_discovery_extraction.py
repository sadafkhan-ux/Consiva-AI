"""Policy discovery and extraction, offline: HTML fixtures and mock HTTP only. A discovered address is not a read page, a failed read is not a missing policy, and a cut text is said to be cut."""

import asyncio
import uuid

import httpx
import pytest

import app.policy.analysis as pa
from app.db.models import Policy
from app.policy.analysis import PRIMARY_DOC_CHARS, collect_documents, fetch_policy_text
from app.scanner.page_parser import parse_page
from app.scanner.policy_detector import POLICY_TEXT_LIMIT, TRUNCATION_MARK, detect_policies, policy_text_state

SITE = "example.test"
BASE = "https://example.test/"


def _links(html: str):
    return parse_page(html, BASE).links


def _detect(html: str, texts: dict[str, str] | None = None):
    return detect_policies(_links(html), texts or {}, SITE)


def _pol(url: str, ptype: str, text: str | None):
    return Policy(url=url, policy_type=ptype, extracted_text_ref=text)


async def _no_fetch(url: str):
    return None


# ---- discovery -------------------------------------------------------------------------------------------------------------------

def test_a_footer_link_is_discovered():
    recs = _detect('<body><main>Hi</main><footer><a href="/legal/privacy">Privacy Notice</a></footer></body>')
    assert [(r.url, r.policy_type) for r in recs] == [("https://example.test/legal/privacy", "privacy_policy")]


def test_a_navigation_link_is_discovered():
    recs = _detect('<nav><a href="/privacy-policy">Privacy Policy</a><a href="/about">About</a></nav>')
    assert [r.policy_type for r in recs] == ["privacy_policy"]


def test_a_nested_policy_url_is_discovered():
    recs = _detect('<a href="/company/legal/en/privacy-policy/">Our policy</a>')
    assert recs and recs[0].url.endswith("/company/legal/en/privacy-policy/")


def test_a_link_with_only_an_aria_label_is_discovered():
    recs = _detect('<a href="/p" aria-label="Privacy Policy"><img src="x.svg"></a>')
    assert [r.policy_type for r in recs] == ["privacy_policy"]


def test_upper_case_link_text_is_discovered():
    assert _detect('<a href="/x1">PRIVACY POLICY</a>')[0].policy_type == "privacy_policy"


def test_a_policy_on_another_domain_is_not_this_sites_policy():
    assert _detect('<a href="https://other.example/privacy">Privacy</a>') == []


def test_the_same_page_linked_twice_or_with_and_without_www_is_listed_once():
    recs = _detect('<a href="https://www.example.test/privacy">Privacy</a><a href="https://example.test/privacy">Privacy policy</a><a href="/privacy">Privacy</a>')
    assert len(recs) == 1


def test_no_candidate_gives_no_record_and_not_an_error():
    assert _detect('<a href="/pricing">Pricing</a><a href="/contact">Contact</a>') == []
    assert _detect("<p>No links at all</p>") == []


def test_text_read_under_the_other_spelling_of_the_address_is_found():
    # the crawl followed a redirect and recorded the page under the www-less spelling; the footer linked the www spelling
    recs = _detect('<a href="https://www.example.test/privacy">Privacy</a>', {"https://example.test/privacy": "policy text " * 40})
    assert recs[0].extracted_text_ref and "policy text" in recs[0].extracted_text_ref


# ---- extraction ------------------------------------------------------------------------------------------------------------------

def test_tables_lists_and_nested_elements_keep_their_text():
    html = ("<body><h1>Privacy</h1><table><tr><th>Data</th><th>Purpose</th></tr><tr><td>Email address</td><td>To send the receipt</td></tr></table>"
            "<ul><li>We keep records <b>for seven years</b></li><li><div><span>nested <em>deeply</em></span></div></li></ul></body>")
    text = parse_page(html, BASE).full_text
    for needle in ("Email address", "To send the receipt", "for seven years", "nested", "deeply"):
        assert needle in text


def test_unicode_and_multilingual_text_is_kept_whole():
    html = "<body><p>गोपनीयता नीति: हम आपका ईमेल पता एकत्र करते हैं। தனியுரிமைக் கொள்கை. Datenschutz: Größe, Straße. プライバシーポリシー 😀</p></body>"
    text = parse_page(html, BASE).full_text
    for needle in ("गोपनीयता नीति", "தனியுரிமைக் கொள்கை", "Größe, Straße", "プライバシーポリシー", "😀"):
        assert needle in text


def test_script_and_style_text_is_not_policy_text():
    text = parse_page("<body><p>The policy.</p><script>var secret = 1;</script><style>.a{color:red}</style></body>", BASE).full_text
    assert "The policy." in text and "secret" not in text and "color" not in text


def test_the_fallback_text_holds_a_long_policy_not_a_5000_character_excerpt():
    page = parse_page("<body>" + "<p>clause text number one. </p>" * 1700 + "<p>FINAL RETENTION CLAUSE</p></body>", BASE)
    assert len(page.visible_text) == 5000              # the short excerpt other code uses
    assert "FINAL RETENTION CLAUSE" in page.full_text  # the policy fallback is not cut there


def test_a_text_over_the_storage_limit_is_marked_as_cut_and_the_analysis_says_so():
    recs = _detect('<a href="/privacy">Privacy</a>', {"https://example.test/privacy": "w " * POLICY_TEXT_LIMIT})
    stored = recs[0].extracted_text_ref
    assert stored.endswith("[Policy text truncated at 60,000 characters.]")
    assert policy_text_state(stored) == "partial"
    docs, _ = asyncio.run(collect_documents([_pol("https://example.test/privacy", "privacy_policy", stored)], _no_fetch))
    assert docs[0].truncated is True
    assert "truncated at" not in docs[0].text          # the mark is for us, not for the model
    assert len(docs[0].text) <= PRIMARY_DOC_CHARS


def test_a_text_under_the_limit_is_stored_whole_and_not_marked():
    recs = _detect('<a href="/privacy">Privacy</a>', {"https://example.test/privacy": "full policy " * 100})
    assert TRUNCATION_MARK.strip() not in recs[0].extracted_text_ref
    assert policy_text_state(recs[0].extracted_text_ref) == "extracted"


@pytest.mark.parametrize("text,state", [(None, "empty"), ("", "empty"), ("   ", "empty"), ("x" * 199, "empty"), ("x" * 200, "extracted"), ("x" * 300 + TRUNCATION_MARK, "partial")])
def test_policy_text_state_boundaries(text, state):
    assert policy_text_state(text) == state


def test_a_discovered_address_with_no_text_is_not_a_read_page():
    recs = _detect('<a href="/privacy">Privacy</a>')                  # linked, never crawled: no text
    assert recs[0].extracted_text_ref is None and policy_text_state(recs[0].extracted_text_ref) == "empty"
    docs, notes = asyncio.run(collect_documents([_pol(recs[0].url, "privacy_policy", None)], _no_fetch))
    assert docs == [] and any("could not be read" in n for n in notes)       # said as a read failure, not as a missing policy


def test_duplicate_pages_with_the_same_text_are_read_once():
    t = "the same legal text " * 30
    docs, notes = asyncio.run(collect_documents([_pol("https://example.test/privacy", "privacy_policy", t), _pol("https://example.test/legal/privacy-copy", "privacy_policy", t)], _no_fetch))
    assert len(docs) == 1 and any("same text" in n for n in notes)


# ---- the plain-HTTP fallback, against mock responses ----------------------------------------------------------------------------

def _mock_http(monkeypatch, handler):
    async def safe(url):
        return None
    monkeypatch.setattr(pa, "assert_safe_url", safe)
    real = httpx.AsyncClient
    monkeypatch.setattr(pa.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))


HTML = "<html><body><h1>Privacy</h1>" + "<p>We keep personal data only as long as needed.</p>" * 20 + "</body></html>"


@pytest.mark.parametrize("status", [403, 404, 429, 500, 503])
def test_an_error_status_gives_no_text(monkeypatch, status):
    _mock_http(monkeypatch, lambda req: httpx.Response(status, text="denied"))
    assert asyncio.run(fetch_policy_text("https://example.test/privacy")) is None


def test_an_unsupported_content_type_gives_no_text(monkeypatch):
    _mock_http(monkeypatch, lambda req: httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF-1.4 " + b"x" * 500))
    assert asyncio.run(fetch_policy_text("https://example.test/privacy.pdf")) is None


def test_a_redirect_is_followed_to_the_page(monkeypatch):
    def handler(req):
        if req.url.path == "/privacy":
            return httpx.Response(301, headers={"location": "/legal/privacy-policy"})
        return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"}, text=HTML)
    _mock_http(monkeypatch, handler)
    text = asyncio.run(fetch_policy_text("https://example.test/privacy"))
    assert text and "as long as needed" in text


def test_a_redirect_loop_ends_without_text(monkeypatch):
    _mock_http(monkeypatch, lambda req: httpx.Response(302, headers={"location": "/privacy"}))
    assert asyncio.run(fetch_policy_text("https://example.test/privacy")) is None


def test_a_page_with_almost_no_text_is_not_a_policy(monkeypatch):
    _mock_http(monkeypatch, lambda req: httpx.Response(200, headers={"content-type": "text/html"}, text="<html><body><div id='root'></div><script>app()</script></body></html>"))
    assert asyncio.run(fetch_policy_text("https://example.test/privacy")) is None      # a JavaScript shell: nothing to read


def test_the_fallback_is_used_only_when_the_crawl_captured_nothing(monkeypatch):
    _mock_http(monkeypatch, lambda req: httpx.Response(200, headers={"content-type": "text/html"}, text=HTML))
    docs, _ = asyncio.run(collect_documents([_pol("https://example.test/privacy", "privacy_policy", None)]))
    assert docs and docs[0].source == "fetch"
    docs2, _ = asyncio.run(collect_documents([_pol("https://example.test/privacy", "privacy_policy", "crawled text " * 40)]))
    assert docs2[0].source == "crawl"


def test_a_failed_fallback_is_a_note_not_an_exception(monkeypatch):
    def refuse(req):
        raise httpx.ConnectError("refused")
    _mock_http(monkeypatch, refuse)
    docs, notes = asyncio.run(collect_documents([_pol("https://example.test/privacy", "privacy_policy", None)]))
    assert docs == [] and notes


# ---- the evidence API says what was extracted -----------------------------------------------------------------------------------

def test_the_evidence_response_carries_the_extraction_state_of_each_policy():
    from app.api.v1.routes.consent_scans import EvidencePolicyItem
    from app.db.repositories.scan_repository import _serialize_evidence

    def mk(url, ptype, text):
        return type("P", (), {"id": uuid.uuid4(), "url": url, "policy_type": ptype, "extracted_text_ref": text})()

    out = _serialize_evidence([], [], [], [], [], [mk("https://a/p", "privacy_policy", "x" * 500), mk("https://a/c", "cookie_policy", None), mk("https://a/l", "privacy_policy", "y" * 300 + TRUNCATION_MARK)], [])
    items = [EvidencePolicyItem(**p) for p in out["policies"]]
    assert [(i.text_state, i.text_characters > 0) for i in items] == [("extracted", True), ("empty", False), ("partial", True)]
