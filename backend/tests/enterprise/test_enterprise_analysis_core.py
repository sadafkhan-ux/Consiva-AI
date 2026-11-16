"""Page classification, chunking, quote verification and the model wrapper of the Enterprise text analysis. The model is always a fake."""
import asyncio

import pytest

from app.enterprise.analysis import chunking
from app.enterprise.analysis import constants as K
from app.enterprise.analysis import pages as pg
from app.enterprise.analysis import textrules as tr
from app.enterprise.analysis.extract import PageExtraction, extract_chunk, normalise, verify
from app.enterprise.analysis.llm import LLMFailure, LLMResult, call_structured
from app.enterprise.crawl import page_inspect as pi

from .test_enterprise_analysis_support import FakeLLM, POLICY

POLICY_HTML_HEADS = ["Privacy Policy", "Information we collect", "How we use your information", "Sharing with third parties", "Data retention", "Your rights", "Security", "Contact us"]


def cls(url, title="", heads=(), text=""):
    return pg.classify_page(pg.PageFacts(url, title, list(heads), text))


# ---- page classification ----------------------------------------------------------------------------------------------------------------------------------------------

def test_the_known_bug_a_blog_article_about_dpdp_is_not_the_privacy_policy():
    c = cls("https://x.com/blog/dpdp-act-2023-compliance-guide", "DPDP Act 2023: privacy policy compliance guide", ["Privacy policy requirements", "Data retention", "Your rights", "Security"], "privacy " * 800)
    assert c.category == pg.ARTICLE and c.strength == "high"


@pytest.mark.parametrize("url,title", [
    ("https://x.com/news/privacy-policy-changes", "Our privacy policy is changing: what you need to know"), ("https://x.com/insights/dpdp-readiness", "DPDP readiness guide"),
    ("https://x.com/press/new-privacy-notice", "Acme announces a new privacy notice"), ("https://x.com/articles/what-is-a-privacy-policy", "What is a privacy policy?"),
    ("https://x.com/2024/05/our-privacy-policy-explained", "Our privacy policy, explained"), ("https://x.com/resources/privacy-policy-template", "Privacy policy template: download it free")])
def test_editorial_paths_are_articles_when_neither_the_address_nor_the_title_names_a_policy(url, title):
    heads = ["Overview"] + POLICY_HTML_HEADS[1:]                # an article can discuss every policy topic; what it does not do is name itself a policy
    assert cls(url, title, heads, "x" * 5000).category == pg.ARTICLE


@pytest.mark.parametrize("url", [
    "https://x.com/resources/privacy-policy", "https://x.com/learn/privacy", "https://x.com/knowledge-base/privacy-notice", "https://x.com/news/legal/privacy-policy", "https://x.com/2024/05/privacy-policy"])
def test_a_real_policy_filed_under_an_editorial_or_dated_path_is_read_as_a_policy(url):
    """Owner's audit finding B1: an address under /resources or /learn used to make a policy an article for good, so it was never read and N1 could be wrongly not met. The address no longer
    decides alone: the slug or the title names a policy and the content has the policy's sections."""
    c = cls(url, "Privacy Policy", POLICY_HTML_HEADS, "We collect information. " * 300)
    assert c.category == pg.PRIVACY and not c.ambiguous


def test_an_editorial_path_that_names_a_policy_but_has_thin_content_goes_to_the_model_not_to_article():
    c = cls("https://x.com/resources/privacy-policy", "Privacy Policy", [], "Short page. Read more about privacy.")
    assert c.ambiguous and pg.PRIVACY in c.candidates and pg.ARTICLE in c.candidates


def test_headings_in_h4_to_h6_and_role_heading_count_as_headings():
    html = "<title>Privacy</title><h4>Information we collect</h4><h5>Your rights</h5><div role='heading'>Data retention</div><h2>Security</h2>"
    title, heads = pg.title_and_headings(html)
    assert heads == ["Information we collect", "Your rights", "Data retention", "Security"]


def test_a_policy_with_bold_text_headings_is_still_recognised_by_its_sections():
    text = "\n".join(f"{h}\n" + ("We explain this in plain words and in full detail here. " * 6) for h in ["Information we collect", "How we use your information", "Sharing with third parties", "Data retention", "Your rights", "Security"]) * 3
    c = cls("https://x.com/p/12", "Acme", [], text)
    assert c.category == pg.PRIVACY


@pytest.mark.parametrize("url,cat", [
    ("https://x.com/privacy-policy", pg.PRIVACY), ("https://x.com/privacy", pg.PRIVACY), ("https://x.com/legal/privacy-notice", pg.PRIVACY), ("https://x.com/data-protection-policy", pg.PRIVACY),
    ("https://x.com/privacy-policy.html", pg.PRIVACY), ("https://x.com/cookie-policy", pg.COOKIE), ("https://x.com/cookies", pg.COOKIE), ("https://x.com/terms-and-conditions", pg.TERMS),
    ("https://x.com/terms-of-use", pg.TERMS), ("https://x.com/grievance-redressal", pg.GRIEVANCE), ("https://x.com/contact-us", pg.CONTACT), ("https://x.com/about-us", pg.ABOUT),
    ("https://x.com/docs/Privacy_Policy.pdf", pg.PRIVACY)])
def test_policy_slugs_decide_the_category(url, cat):
    assert cls(url).category == cat


def test_a_policy_without_a_slug_needs_a_policy_title_and_policy_sections():
    c = cls("https://x.com/legal/page-1", "Privacy Policy | Acme", POLICY_HTML_HEADS, POLICY * 3)
    assert c.category == pg.PRIVACY


def test_a_page_that_merely_mentions_privacy_is_not_a_policy():
    c = cls("https://x.com/solutions/compliance", "Compliance software | Acme", ["Why choose us", "Privacy and DPDP made easy", "Pricing"], "We help you with privacy. " * 100)
    assert c.category != pg.PRIVACY


def test_title_that_only_contains_the_words_is_not_a_policy_name():
    c = cls("https://x.com/dpdp-ready", "Is your privacy policy DPDP ready?", ["Checklist"], "Posted on 4 June 2025. 5 min read. Share this. " + "text " * 600)
    assert c.category == pg.ARTICLE


def test_cues_of_an_article_without_a_blog_path_make_an_article():
    c = cls("https://x.com/understanding-consent", "Understanding consent", ["Intro", "Key takeaways"], "By Priya Sharma. Published on 2 Feb 2025. 6 min read. Related posts. Share this. " + "word " * 400)
    assert c.category == pg.ARTICLE and c.strength == "medium"


def test_an_ambiguous_page_is_flagged_for_the_model_not_promoted():
    c = cls("https://x.com/legal/info", "Privacy Policy", ["Overview"], "Coming soon.")
    assert c.ambiguous and c.category == pg.OTHER and pg.PRIVACY in c.candidates
    assert "Opening text" in pg.ambiguous_prompt(pg.PageFacts("https://x.com/legal/info", "Privacy Policy", [], "Coming soon."), c.candidates)


def test_product_contact_about_and_other():
    assert cls("https://x.com/products/widget", "Widget").category == pg.PRODUCT
    assert cls("https://x.com/reach", "Contact us", [], "Write to us. " * 20).category == pg.CONTACT
    assert cls("https://x.com/who", "About us", [], "We are a company. " * 20).category == pg.ABOUT
    assert cls("https://x.com/careers", "Careers").category == pg.OTHER


def test_title_and_headings_are_read_from_stored_html():
    t, h = pg.title_and_headings("<html><title> Privacy Policy | Acme </title><h1>Privacy <b>Policy</b></h1><h2>Your rights</h2></html>")
    assert t == "Privacy Policy | Acme" and h == ["Privacy Policy", "Your rights"]


# ---- chunking ---------------------------------------------------------------------------------------------------------------------------------------------------------

def _long():
    return "\n\n".join(f"Section {i}\n\n" + " ".join(f"Sentence {i}.{j} says something about data." for j in range(40)) for i in range(12))


def test_chunks_tile_the_source_with_no_loss_and_no_duplication():
    text = _long()
    chunks = chunking.chunk_text(text, max_chars=3000, overlap=200)
    assert len(chunks) > 3
    assert "".join(text[c.start:c.end] for c in chunks) == text
    assert [c.start for c in chunks[1:]] == [c.end for c in chunks[:-1]]
    assert all(c.count == len(chunks) for c in chunks) and [c.index for c in chunks] == list(range(len(chunks)))


def test_every_chunk_is_a_verbatim_slice_with_overlap_and_within_budget():
    text = _long()
    chunks = chunking.chunk_text(text, max_chars=3000, overlap=200)
    for c in chunks:
        assert c.text == text[c.text_start:c.end]
        assert len(c.text) <= 3000 + 200
    for prev, nxt in zip(chunks, chunks[1:]):
        assert nxt.text_start < nxt.start and nxt.text_start >= prev.start          # it repeats the tail of the previous chunk
        assert nxt.start - nxt.text_start <= 200 + 30                                   # snapped to a word start, so a little over at most
        assert text[nxt.text_start - 1].isspace()


def test_a_short_text_is_one_chunk_and_empty_text_is_none():
    assert len(chunking.chunk_text("A short policy.")) == 1
    assert chunking.chunk_text("   \n ") == []


def test_one_enormous_unbroken_run_is_still_split_without_loss():
    text = "word " * 5000
    chunks = chunking.chunk_text(text, max_chars=1000, overlap=50)
    assert "".join(text[c.start:c.end] for c in chunks) == text and len(chunks) >= 25


def test_chunks_split_at_headings_when_a_chunk_is_half_full():
    text = ("Intro paragraph. " * 60) + "\n\nYour rights\n\n" + ("Rights text. " * 60)
    chunks = chunking.chunk_text(text, max_chars=1600, overlap=100)
    assert any(text[c.start:c.end].lstrip().startswith("Your rights") for c in chunks[1:])


def test_the_chunk_budget_is_read_at_call_time(monkeypatch):
    monkeypatch.setattr(K, "CHUNK_MAX_CHARS", 1200)
    assert len(chunking.chunk_text(_long())) > 10


# ---- quote verification -----------------------------------------------------------------------------------------------------------------------------------------------

def _pe(*facts):
    return PageExtraction.model_validate({"facts": [{"topic": t, "value": v, "quote": q, "specific": True} for t, v, q in facts]})


def test_a_fact_whose_quote_is_in_the_text_is_kept_whitespace_normalised():
    text = "We keep data\nfor  twelve   months only."
    kept, dropped = verify(_pe(("retention", "12 months", "We keep data for twelve months only.")), text)
    assert dropped == 0 and kept[0]["quote"] == "We keep data for twelve months only."


def test_typographic_quotes_and_dashes_do_not_defeat_a_genuine_quote():
    kept, dropped = verify(_pe(("security", "x", "We don't sell data - ever")), "We don’t sell data – ever, to anyone.")
    assert dropped == 0 and len(kept) == 1


def test_an_invented_quote_is_discarded_and_counted():
    kept, dropped = verify(_pe(("security", "ISO", "We are ISO 27001 certified."), ("retention", "1y", "kept for one year")), "We keep records, kept for one year.")
    assert dropped == 1 and [k["topic"] for k in kept] == ["retention"]


def test_elided_or_tiny_quotes_are_not_evidence():
    text = "We collect names and addresses and phone numbers from you."
    kept, dropped = verify(_pe(("data_category", "x", "We collect names ... phone numbers"), ("data_category", "y", "We")), text)
    assert kept == [] and dropped == 2


def test_duplicate_facts_are_kept_once():
    kept, _ = verify(_pe(("security", "a", "We use encryption."), ("security", "b", "We  use encryption.")), "We use encryption. Always.")
    assert len(kept) == 1


async def test_extract_chunk_discards_what_the_model_invented():
    fake = FakeLLM(invent=True)
    ex = await extract_chunk(fake, url="https://acme.test/privacy-policy", kind="page_text", chunk_text=POLICY, index=0, count=1)
    assert ex.discarded == 1 and ex.raw == len(ex.facts) + 1
    assert all(normalise(f["quote"]) in normalise(POLICY) for f in ex.facts)
    assert ex.answer.provider == "claude"


# ---- the model wrapper ------------------------------------------------------------------------------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    monkeypatch.setattr(K, "LLM_BACKOFF_S", (0.0,))


async def test_malformed_json_is_retried_then_accepted():
    fake = FakeLLM(malformed_first=1)
    ans = await call_structured(fake, "s", "<<<PASSAGE\nWe use encryption.\nPASSAGE>>>", PageExtraction)
    assert ans.attempts == 2 and len(fake.calls) == 2


async def test_malformed_json_every_time_is_a_failure_not_an_answer():
    fake = FakeLLM(malformed_first=99)
    with pytest.raises(LLMFailure) as e:
        await call_structured(fake, "s", "u", PageExtraction)
    assert e.value.attempts == K.LLM_ATTEMPTS and len(fake.calls) == K.LLM_ATTEMPTS


async def test_an_api_error_is_retried_then_a_failure():
    fake = FakeLLM(fail_if=lambda *a: True)
    with pytest.raises(LLMFailure) as e:
        await call_structured(fake, "s", "<<<PASSAGE\nx\nPASSAGE>>>", PageExtraction)
    assert "provider unavailable" in str(e.value)


async def test_a_timeout_is_a_failure():
    async def slow(system, user, schema):
        await asyncio.sleep(5)
    with pytest.raises(LLMFailure):
        await call_structured(slow, "s", "u", PageExtraction, attempts=1, timeout_s=0.05)


async def test_dicts_and_json_strings_are_validated_against_the_schema():
    async def as_dict(s, u, sc):
        return {"facts": []}

    async def as_json(s, u, sc):
        return LLMResult('{"facts": []}', "nvidia", "m")
    assert (await call_structured(as_dict, "s", "u", PageExtraction)).parsed.facts == []
    a = await call_structured(as_json, "s", "u", PageExtraction)
    assert (a.provider, a.model) == ("nvidia", "m")


# ---- text readers ------------------------------------------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text,days", [("within 30 days", 30), ("within thirty (30) working days", 30), ("two weeks", 14), ("48 hours", 2), ("within 3 months", 90), ("1 year", 365)])
def test_durations_are_read_as_days(text, days):
    assert tr.parse_days(text) == [days]


def test_vague_periods_give_no_number():
    assert tr.parse_days("within a reasonable time") == [] and tr.parse_days("promptly") == []


def test_language_detection():
    assert tr.detect_language(POLICY)["language"] == "en"
    assert tr.detect_language("हम आपका नाम और ईमेल पता एकत्र करते हैं। यह हमारी गोपनीयता नीति है।")["language"] == "eighth_schedule"
    assert tr.detect_language("Nous collectons votre nom et votre adresse électronique pour répondre à vos demandes et améliorer nos services.")["language"] == "other"


def test_vendor_aliases_and_cookie_prefixes():
    assert tr.names_any("we use google analytics", tr.vendor_tokens("Google Analytics", "www.google-analytics.com"))
    assert tr.names_any("we use google for ads", tr.vendor_tokens(None, "securepubads.g.doubleclick.net"))
    assert not tr.names_any("we use matomo", tr.vendor_tokens("Hotjar", "static.hotjar.com"))
    assert tr.cookie_declared("_ga_ABC123XYZ9", "", "we set _ga_ and _gid cookies")
    assert tr.cookie_declared("sid", "", "the sid cookie keeps you signed in")
    assert not tr.cookie_declared("zzz_track", "", "we set a session cookie")


# ---- the form evidence now carries what the C3 and N8 readings need ---------------------------------------------------------------------------------------------------

def test_forms_record_preticked_controls_and_the_wording_beside_them():
    raw = [{"action": "/x", "method": "post", "privacy_link": False, "text": "Join", "label_text": "Email | Send me offers",
            "fields": [{"type": "email", "name": "e"}, {"type": "checkbox", "name": "offers", "label": "Send me offers", "checked": True},
                       {"type": "radio", "name": "r", "checked": False}]}]
    f = pi.normalize_forms(raw, "https://x.test/")[0]
    assert [x["name"] for x in f["preticked"]] == ["offers"] and f["label_text"] == "Email | Send me offers" and len(f["consent_controls"]) == 1


def test_the_dom_script_reads_checked_state_and_label_text():
    assert "checked:" in pi._DOM_JS and "label_text" in pi._DOM_JS
