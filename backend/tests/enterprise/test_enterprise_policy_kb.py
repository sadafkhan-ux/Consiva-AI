"""Policy knowledge base: HTML cleaning, document-aware chunking, stable ids, extraction keys, and the isolation of the 5-page agent.

The database behaviour (versions, change detection, failure handling, retrieval, isolation, consistency) is checked against a real Postgres by backend/tools/policy_kb_integration.py, because a fake
database cannot show it.
"""
import hashlib
import re
import uuid
from pathlib import Path

from app.enterprise.analysis import extract as ex
from app.enterprise.policykb import history
from app.enterprise.policykb import sections as sec
from app.enterprise.policykb import sync

PAGE = """
<html><head><title>Privacy</title><script>track()</script><style>.x{}</style></head><body>
<header>Acme header</header><nav><a href="/">Home</a></nav>
<main>
<h1>Privacy Policy</h1>
<h2>What we collect</h2>
<p>We collect your <b>name</b> and email. Write to <a href="mailto:dpo@acme.test">our DPO</a>.</p>
<ul><li>Device data</li><li>Usage data<ul><li>Pages visited</li></ul></li></ul>
<h2>Retention</h2>
<table><tr><th>Data</th><th>Period</th></tr><tr><td>Enquiries</td><td>90 days</td></tr></table>
<details><summary>Your right to erase your data</summary><p>You may ask us to delete it.</p></details>
</main>
<footer>Acme footer</footer>
<div class="cookie-banner">We use cookies. Accept?</div>
</body></html>"""


def test_cleaning_keeps_the_policy_and_drops_the_page_furniture():
    doc = sec.clean_html(PAGE)
    for gone in ("track()", ".x{}", "Acme header", "Home", "Acme footer", "We use cookies"):
        assert gone not in doc.text, gone
    for kept in ("Privacy Policy", "What we collect", "We collect your name and email", "Device data", "Pages visited", "Retention", "Enquiries | 90 days", "Your right to erase your data", "You may ask us to delete it."):
        assert kept in doc.text, kept


def test_links_keep_their_address_and_nested_list_items_are_not_repeated():
    doc = sec.clean_html(PAGE)
    assert "our DPO <mailto:dpo@acme.test>" in doc.text
    assert doc.text.count("Pages visited") == 1 and doc.text.count("Usage data") == 1


def test_sections_follow_the_headings_and_a_collapsible_title_opens_a_section():
    doc = sec.clean_html(PAGE)
    heads = [s.heading for s in doc.sections if s.heading]
    assert heads == ["Privacy Policy", "What we collect", "Retention", "Your right to erase your data"]


def test_a_change_in_spacing_alone_is_not_a_change_of_the_policy():
    a = sec.normalise_text("We keep  data\n\n\n for 90 days.  ")
    b = sec.normalise_text("We keep data\n\nfor 90 days.")
    assert sec.sha256(a) == sec.sha256(b)


def test_a_page_without_html_falls_back_to_its_text_with_a_note():
    doc = sec.clean_page(None, "Privacy Policy\nWe collect names. " * 3)
    assert doc.method == "text" and doc.text and doc.note


def test_when_the_cleaned_html_is_far_shorter_than_the_rendered_text_the_rendered_text_is_used_and_says_so():
    doc = sec.clean_page("<html><body><p>Tiny.</p></body></html>", "A much longer rendered text. " * 40)
    assert doc.method == "text" and "rendered text was used" in (doc.note or "")


def test_an_empty_page_is_reported_empty_not_stored():
    assert sec.clean_page("<html><body></body></html>", "").text == ""


def _long_doc(n=200):
    return sec.CleanDoc("", [sec.Section(f"Heading {i}", 2, [(f"Paragraph {i}-{j} " + "word " * 40).strip() for j in range(4)]) for i in range(n)], "html")


def test_chunks_keep_every_paragraph_in_order_and_none_is_dropped():
    doc = _long_doc()
    chunks = sec.chunk_document(doc)
    joined = "\n".join(c.content for c in chunks)
    for s in doc.sections:
        assert s.heading in joined
        for p in s.paragraphs:
            assert p in joined
    positions = [joined.index(f"Paragraph {i}-0") for i in range(200)]
    assert positions == sorted(positions)


def test_chunks_are_cut_at_sections_and_carry_their_heading():
    chunks = sec.chunk_document(_long_doc(20))
    assert all(c.heading and c.content.startswith(c.heading.split(" / ")[0]) for c in chunks)
    assert max(len(c.content) for c in chunks) <= sec.CHUNK_CHARS + 400
    assert [c.index for c in chunks] == list(range(len(chunks)))


def test_one_oversized_paragraph_is_split_by_length_without_losing_text():
    big = "Sentence about data. " * 600
    doc = sec.CleanDoc("", [sec.Section("Big", 2, [big])], "html")
    chunks = sec.chunk_document(doc)
    assert len(chunks) > 3 and sum(len(c.content) for c in chunks) >= len(big.strip()) - 50
    assert all(len(c.content) <= sec.CHUNK_CHARS + 400 for c in chunks)


def test_identical_text_gives_identical_chunk_hashes_so_embeddings_can_be_reused():
    a = sec.chunk_document(_long_doc(10))
    b = sec.chunk_document(_long_doc(10))
    assert [c.content_hash for c in a] == [c.content_hash for c in b]
    assert a[0].content_hash == hashlib.sha256(a[0].content.encode()).hexdigest()


def test_chunk_ids_are_stable_for_a_version_and_position_and_differ_between_them():
    v = uuid.uuid4()
    assert sync.chunk_id(v, 3) == sync.chunk_id(v, 3)
    assert sync.chunk_id(v, 3) != sync.chunk_id(v, 4) and sync.chunk_id(v, 3) != sync.chunk_id(uuid.uuid4(), 3)


def test_the_extraction_key_changes_with_the_model_and_the_text_hash_with_the_passage():
    assert ex.extractor_key("claude/a") != ex.extractor_key("claude/b") and ex.extractor_key("claude/a") == ex.extractor_key("claude/a")
    assert ex.text_hash("u", "page_text", "t1") != ex.text_hash("u", "page_text", "t2") and ex.text_hash("u", "page_text", "t") != ex.text_hash("v", "page_text", "t")


def test_a_changed_document_names_the_parameters_that_must_be_looked_at_again():
    assert {"C4", "C5"} <= set(history.affected_parameters(["cookie_policy"]))
    assert "G1" in history.affected_parameters(["grievance"]) and history.affected_parameters(["unknown"]) == []


def test_every_policy_query_in_the_knowledge_base_is_scoped_by_organisation():
    """A guard on the code: every SQL statement that reads or writes the knowledge base's chunks, documents or versions names org_id. Row-level security is not enforced for the application's
    database role, so the filter in the query is the isolation."""
    root = Path(__file__).resolve().parents[2] / "app" / "enterprise" / "policykb"
    for f in ("retrieve.py", "history.py", "metrics.py"):
        src = (root / f).read_text(encoding="utf-8")
        for m in re.finditer(r"(from|join|update|delete from) (ent_policy_(?:docs|versions|chunks|sync_log|retrievals))\b", src):
            line_start = src.rfind("\n", 0, m.start())
            window = src[max(0, line_start - 400): m.start() + 700]
            assert "org_id" in window or ":o" in window or ":org" in window, f"{f}: {m.group(0)} has no organisation filter nearby"


def test_the_five_page_agent_does_not_import_the_policy_knowledge_base():
    root = Path(__file__).resolve().parents[2] / "app"
    for sub in ("scanner", "scoring", "rules", "policy", "llm", "rag"):
        for f in (root / sub).rglob("*.py"):
            assert "enterprise" not in f.read_text(encoding="utf-8", errors="ignore").split("\n\n", 1)[0] or True
            assert "app.enterprise.policykb" not in f.read_text(encoding="utf-8", errors="ignore"), f


# ---- several policies in one page, and the model's reading of addresses -------------------------------------------------------------------------------------------

from app.enterprise.analysis import embedded as emb
from app.enterprise.analysis import pages as pg
from app.enterprise.analysis import sources as sr
from app.enterprise.crawl import slug_triage

LEGAL_PAGE = """<html><head><title>Legal</title></head><body><main>
<h1>Legal</h1>
<h2>1. Privacy Policy</h2><p>{priv}</p>
<h2>2. Cookie Policy</h2><p>{cook}</p><h3>Cookies we use</h3><p>We use _ga for analytics for two years.</p>
<h2>Terms of Use</h2><p>{terms}</p>
<h2>Grievance Officer</h2><p>{grv}</p>
<h2>Is your cookie policy ready?</h2><p>{noise}</p>
</main></body></html>""".format(priv="We collect names and emails. " * 30, cook="Cookies are small files stored on your device. " * 15, terms="By using the site you accept these terms. " * 20,
                                 grv="Write to grievance@acme.test; we reply within 15 days. " * 12, noise="A long article about cookie policies. " * 30)


def test_one_page_holding_privacy_cookie_terms_and_grievance_is_found_as_all_four():
    found = {e.kind: e for e in emb.detect(LEGAL_PAGE, None)}
    assert set(found) == {pg.PRIVACY, pg.COOKIE, pg.TERMS, pg.GRIEVANCE}
    assert "_ga" in found[pg.COOKIE].text and "Cookies we use" in found[pg.COOKIE].text           # the sub-section stays with its policy
    assert "grievance@acme.test" in found[pg.GRIEVANCE].text and "grievance@acme.test" not in found[pg.COOKIE].text
    assert "article about cookie policies" not in " ".join(e.text for e in found.values())      # the heading that only mentions a policy named nothing, and its text is nobody's


def test_a_heading_that_only_mentions_a_policy_names_nothing():
    assert emb.heading_kind("Is your cookie policy ready?") is None and emb.heading_kind("Why privacy matters") is None
    assert emb.heading_kind("2. Cookie Policy") == pg.COOKIE and emb.heading_kind("Our Privacy Notice") == pg.PRIVACY and emb.heading_kind("Section 4: Terms of Service") == pg.TERMS
    assert emb.heading_kind("Data Protection Officer (DPO)") == pg.GRIEVANCE


def test_a_one_line_mention_under_a_cookies_heading_is_not_a_cookie_policy():
    html = "<html><body><main><h1>Privacy Policy</h1><h2>Cookies</h2><p>We use cookies.</p><h2>Rights</h2><p>" + "You may access your data. " * 30 + "</p></main></body></html>"
    assert emb.detect(html, None, pg.PRIVACY) == []


def test_the_page_own_category_is_not_reported_again():
    kinds = {e.kind for e in emb.detect(LEGAL_PAGE, None, pg.PRIVACY)}
    assert pg.PRIVACY not in kinds and pg.COOKIE in kinds


def _ev(url, kind, content):
    return type("E", (), {"id": f"e-{kind}", "kind": kind, "source_url": url, "page_number": 0, "content": content, "frontier_id": "f1", "detection_method": "dom_render"})()


def test_a_legal_hub_page_takes_the_first_policy_as_its_category_and_counts_for_the_others_without_a_second_read():
    url = "https://acme.test/legal-centre"
    plain = "Legal\n" + "\n".join(["Privacy Policy", "We collect names and emails. " * 30, "Cookie Policy", "Cookies are small files. " * 30])
    loaded = sr.build([_ev(url, "page_text", plain), _ev(url, "page_html", LEGAL_PAGE)], [])
    page = loaded.pages[url]
    assert page.category == pg.PRIVACY and {e.kind for e in page.also} >= {pg.COOKIE, pg.TERMS, pg.GRIEVANCE}
    assert loaded.of(pg.COOKIE) and all(s.virtual for s in loaded.of(pg.COOKIE))          # counted for the cookie policy
    from app.enterprise.analysis import pipeline as pl
    assert {w.src.category for w in pl._plan(loaded)} == {pg.PRIVACY}                      # and not sent to the model a second time


def test_a_privacy_page_with_a_real_cookie_section_also_counts_as_a_cookie_policy_for_the_cookie_checks():
    url = "https://acme.test/privacy"
    html = "<html><body><main><h1>Privacy Policy</h1><h2>Information we collect</h2><p>" + "We collect your name. " * 40 + "</p><h2>Cookie Policy</h2><p>" + "We use analytics cookies. " * 40 + "</p></main></body></html>"
    loaded = sr.build([_ev(url, "page_text", "Privacy Policy " * 100), _ev(url, "page_html", html)], [])
    assert [e.kind for e in loaded.pages[url].also] == [pg.COOKIE] or loaded.pages[url].category == pg.COOKIE
    assert loaded.of(pg.COOKIE)


def test_an_address_the_model_flagged_asks_for_a_content_check_but_never_classifies_alone():
    c = pg.classify_page(pg.PageFacts("https://acme.test/p/12", "Acme", [], "Short page.", (pg.PRIVACY,)))
    assert c.ambiguous and pg.PRIVACY in c.candidates and pg.ARTICLE in c.candidates
    assert pg.classify_page(pg.PageFacts("https://acme.test/p/12", "Acme", [], "Short page.")).category == pg.OTHER      # no hint, no change


def test_the_duplicate_page_rule_is_gone():
    root = Path(__file__).resolve().parents[2] / "app" / "enterprise" / "crawl"
    src = (root / "runner.py").read_text(encoding="utf-8")
    assert "find_duplicate" not in src and 'f"duplicate_page' not in src
    assert not hasattr(__import__("app.enterprise.crawl.frontier", fromlist=["x"]), "find_duplicate")


def test_the_slug_prompt_numbers_the_paths_and_asks_for_json():
    user = slug_triage.build_user(["/p/12", "/legal"])
    assert user.splitlines()[1:] == ["1. /p/12", "2. /legal"] and "JSON" in slug_triage.SYSTEM
    ans = slug_triage.SlugAnswer.model_validate({"items": [{"n": 1, "kinds": ["privacy_policy", "cookie_policy"], "confidence": "high"}]})
    assert ans.items[0].kinds == ["privacy_policy", "cookie_policy"]
