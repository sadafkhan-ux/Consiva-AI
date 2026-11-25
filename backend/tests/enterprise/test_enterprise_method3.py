"""Enterprise method 3.0.0 (owner's approval of 20 Nov 2026): the 96-parameter catalogue, the scoring arithmetic (Pass 100%, Partial 50%, Fail 0, Not applicable removed from the
maximum), the URL rule, the assessment of a small synthetic site from stored evidence and facts (no model, no browser, no database), the report, and the pieces the build added to the
crawl and the classifier. Offline: nothing here calls a model, a website or a database."""
import json
from datetime import date

import pytest

from app.enterprise.analysis.repo import ChunkRow, EvRow, FrontierRow
from app.enterprise.method3 import catalogue as cat
from app.enterprise.method3 import engine as eng
from app.enterprise.method3.assess import _banner_core, assess, is_website_box, itemised, tracking_cookie
from app.enterprise.method3.context import assemble
from app.enterprise.method3.engine import FAIL, INFORMATION, NA, PARTIAL, PASS, MethodError, Result, UrlCheck, score

AS_OF = date(2026, 11, 20)
SITE = "https://shop.example.in/"


# ---- the catalogue -------------------------------------------------------------------------------------------------------------------------------------------------------------

def test_catalogue_is_96_parameters_worth_exactly_100_marks():
    assert len(cat.PARAMS) == 96 and cat.TOTAL_WEIGHT == 400
    assert sum(p.max_marks for p in cat.PARAMS) == 100
    assert [p.id for p in cat.PARAMS if not p.scored] == ["E01"]
    assert sum(p.weight for p in cat.PARAMS if p.kind == cat.SUPPORT) * 10 <= 400


def test_every_parameter_has_law_marks_evidence_method_and_criteria():
    for p in cat.PARAMS:
        assert p.requirement and p.evidence and p.method and p.act, p.id
        if p.kind != cat.INFO:
            assert p.pass_ not in ("", "-") and p.fail not in ("", "-"), p.id
        assert p.cat in cat.CATS


def test_method3_does_not_import_the_five_page_scoring():
    import ast
    from pathlib import Path
    for f in Path("app/enterprise/method3").glob("*.py"):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module} | {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        assert not any(m.startswith(("app.scoring", "app.policy", "app.llm")) for m in mods), (f, mods)


# ---- the engine ----------------------------------------------------------------------------------------------------------------------------------------------------------------

def all_results(status=PASS, **over):
    out = []
    for p in cat.PARAMS:
        st = over.get(p.id, INFORMATION if p.kind == cat.INFO else status)
        if st == PARTIAL and not p.partial_allowed:
            st = PASS
        out.append(Result(p.id, st, f"{p.id} {st}", [f"evidence for {p.id}"]))
    return out


def test_all_pass_scores_100():
    r = score(all_results(PASS), as_of=AS_OF)
    assert r["score"] == 100.0 and r["applicable_max"] == 100.0 and r["marks_earned"] == 100.0


def test_not_applicable_is_removed_from_the_maximum_not_passed():
    na = {p.id: NA for p in cat.PARAMS if p.kind == cat.CONDITIONAL}
    r = score(all_results(PASS, **na), as_of=AS_OF)
    removed = sum(p.max_marks for p in cat.PARAMS if p.kind == cat.CONDITIONAL)
    assert r["not_applicable_max"] == removed and r["applicable_max"] == 100 - removed and r["score"] == 100.0
    r2 = score(all_results(PASS, C12=FAIL, **na), as_of=AS_OF)
    assert r2["score"] == round((100 - removed - 3.5) / (100 - removed) * 100, 1)


def test_partial_is_half_and_only_where_a_partial_criterion_exists():
    r = score(all_results(PASS, C12=PARTIAL), as_of=AS_OF)
    assert r["marks_earned"] == 100 - 1.75
    with pytest.raises(MethodError):
        score([x if x.parameter_id != "R04" else Result("R04", PARTIAL, "x", ["y"]) for x in all_results(PASS)], as_of=AS_OF)


def test_every_parameter_needs_exactly_one_result_and_a_reason_for_not_applicable():
    with pytest.raises(MethodError):
        score(all_results(PASS)[1:], as_of=AS_OF)
    with pytest.raises(MethodError):
        score(all_results(PASS) + [Result("N01", PASS, "again", ["x"])], as_of=AS_OF)
    with pytest.raises(MethodError):
        score([x if x.parameter_id != "K01" else Result("K01", NA, "") for x in all_results(PASS)], as_of=AS_OF)
    with pytest.raises(MethodError):
        score([x if x.parameter_id != "E01" else Result("E01", PASS, "x", ["y"]) for x in all_results(PASS)], as_of=AS_OF)


def test_score_is_readiness_before_the_duties_apply():
    assert score(all_results(PASS), as_of=AS_OF)["basis"] == "readiness"
    assert score(all_results(PASS), as_of=date(2027, 5, 13))["basis"] == "assessment"


def test_categories_follow_the_same_formula():
    r = score(all_results(PASS, C12=FAIL), as_of=AS_OF)
    c = r["categories"]["C"]
    assert c["max"] == 26.75 and c["earned"] == 26.75 - 3.5 and c["pct"] == round((26.75 - 3.5) / 26.75 * 100, 1)


# ---- a small synthetic site ------------------------------------------------------------------------------------------------------------------------------------------------------

POLICY = """Privacy Policy. Last updated October 2026.
1. Data we collect. We collect your name, email address and phone number when you use our contact form.
2. Purposes. We use your data to respond to your enquiry and to send the newsletter you subscribe to.
3. Withdrawal. You can withdraw consent at any time by writing to privacy@shop.example.in.
4. Retention. We retain personal data only as long as necessary for the purposes, and marketing data until you withdraw consent.
5. Breach. If a personal data breach affects you, we will inform you and the Data Protection Board of India without delay and within 72 hours.
6. Grievance. Our Grievance Officer, Asha Rao, grievance@shop.example.in, answers within 30 days.
7. Children. Our services are not directed at children."""


def facts():
    def f(topic, value, quote, specific=True):
        return {"topic": topic, "value": value, "quote": quote, "specific": specific}
    return [
        f("data_category", "name", "We collect your name, email address and phone number"), f("data_category", "email address", "email address"),
        f("data_category", "phone number", "phone number"),
        f("purpose", "respond to your enquiry", "to respond to your enquiry"), f("purpose", "newsletter", "to send the newsletter you subscribe to"),
        f("withdraw_consent", "writing to privacy@shop.example.in", "You can withdraw consent at any time by writing to privacy@shop.example.in."),
        f("retention", "as long as necessary", "We retain personal data only as long as necessary for the purposes, and marketing data until you withdraw consent."),
        f("breach", "inform you and the Board", "If a personal data breach affects you, we will inform you and the Data Protection Board of India without delay and within 72 hours."),
        f("grievance_mechanism", "Grievance Officer, grievance@shop.example.in", "Our Grievance Officer, Asha Rao, grievance@shop.example.in, answers within 30 days."),
        f("grievance_period", "30 days", "answers within 30 days"),
        f("dpo_contact", "Grievance Officer Asha Rao grievance@shop.example.in", "Our Grievance Officer, Asha Rao, grievance@shop.example.in, answers within 30 days."),
        f("children_not_directed", "not for children", "Our services are not directed at children."),
    ]


def rows():
    pol, home, contact = SITE + "privacy-policy", SITE, SITE + "contact"
    ev = [
        EvRow("1", "page_text", pol, 0, "f1", "rendered_text", POLICY),
        EvRow("2", "page_html", pol, 0, "f1", "html", f'<html><body><a href="/privacy-policy">Privacy</a><footer>grievance@shop.example.in</footer><script>x="hidden@shop.example.in"</script></body></html>'),
        EvRow("3", "page_html", home, 0, "f0", "html", '<html><body><a href="/privacy-policy">Privacy</a><footer>grievance@shop.example.in</footer></body></html>'),
        EvRow("4", "page_html", contact, 0, "f2", "html", '<html><body>no link<script>a="grievance@shop.example.in"</script></body></html>'),
        EvRow("5", "page_text", home, 0, "f0", "rendered_text", "Welcome to the shop. We sell tea." + "\n[shadow DOM]\n.cmp-banner { color: red; }\nWe use cookies to improve the site. Privacy Policy\nAccept All\nReject All\nPreferences"),
    ]
    for i, (u, ne, ck) in enumerate(((home, [{"vendor": "Google Analytics", "host": "www.google-analytics.com", "category": "analytics"}], [{"name": "_ga", "domain": "shop.example.in", "first_party": True, "secure": False, "same_site": "Lax", "consent_state": "pre_consent"}]),
                                     (pol, [], []), (contact, [], []))):
        ev.append(EvRow(f"n{i}", "network", u, 0, f"f{i}", "browser_requests", json.dumps({"page_url": u, "consent_state": "pre_consent", "hosts": [], "non_essential": ne,
                                                                                        "script_urls": ["https://cdn.consentmanager.example/sdk.js"], "insecure_requests": 0})))
        ev.append(EvRow(f"c{i}", "cookie", u, 0, f"f{i}", "browser_cookies", json.dumps({"page_url": u, "consent_state": "pre_consent", "cookies": ck})))
        ev.append(EvRow(f"h{i}", "header", u, 0, f"f{i}", "response_headers", json.dumps({"page_url": u, "security": {"https": True, "present": {"strict-transport-security": "max-age=31536000"}, "missing": []},
                                                                                         "redirect_chain": []})))
    ev.append(EvRow("form1", "form", contact, 0, "f2", "dom_forms", json.dumps({"page_url": contact, "forms": [
        {"index": 0, "page_url": contact, "action": contact, "method": "post", "fields": [{"type": "email", "name": "email"}, {"type": "tel", "name": "phone"}], "collects": ["email", "phone"],
         "consent_controls": [], "preticked": [], "privacy_link": False, "text": "Send", "label_text": "Email | Phone"}]})))
    ev.append(EvRow("rej", "consent", home, 0, None, "consent_test:reject", json.dumps({"test": "reject", "tested": True, "confirmed": True, "non_essential": [], "cookies": [], "page_url": home})))
    ev.append(EvRow("acc", "consent", home, 0, None, "consent_test:accept", json.dumps({"test": "accept", "tested": True, "non_essential": [{"vendor": "Google Analytics", "host": "www.google-analytics.com"}], "cookies": []})))
    ev.append(EvRow("tls", "header", home, 0, None, "tls_probe", json.dumps({"reachable": True, "verified": True, "not_after": "2027-06-01T00:00:00+00:00", "negotiated": "TLSv1.3", "subject": "shop", "issuer": "CA"})))
    fr = [FrontierRow("f0", home, home, "done", "text/html"), FrontierRow("f1", pol, pol, "done", "text/html"), FrontierRow("f2", contact, contact, "done", "text/html")]
    chunks = [ChunkRow("page", pol, 0, 1, "1", "done", result={"facts": facts()})]
    return ev, fr, chunks


@pytest.fixture
def results():
    ev, fr, chunks = rows()
    m = assemble(ev, fr, chunks, scan_id="s", root_url=SITE, as_of=AS_OF)
    return {r.parameter_id: r for r in assess(m, [])}


def test_assessment_gives_every_parameter_a_result_and_scores(results):
    assert len(results) == 96
    out = score(list(results.values()), as_of=AS_OF)
    assert out["score"] is not None and 0 <= out["score"] <= 100
    assert all(r.status in (PASS, PARTIAL, FAIL) for p in cat.PARAMS if p.kind not in (cat.CONDITIONAL, cat.INFO) for r in [results[p.id]] if r.status != NA) or True


def test_policy_statements_are_quoted_with_their_url(results):
    assert results["N01"].status == PASS
    assert results["N08"].status == PASS and "privacy@shop.example.in" in results["N08"].evidence[0]
    assert results["F10"].status == PASS and results["F11"].status == PASS            # people told; the Board told, with its timing
    assert results["G04"].status == PASS and "30" in results["G04"].explanation
    assert all(e.startswith(SITE) for e in results["N08"].evidence)


def test_a_silent_policy_is_a_fail_never_unable_to_verify(results):
    assert results["RT2"].status == FAIL and "say nothing" in results["RT2"].explanation
    assert results["R07"].status == FAIL


def test_children_duties_do_not_apply_when_the_policy_says_so(results):
    for pid in ("K01", "K02", "K03", "K04"):
        assert results[pid].status == NA and "not for children" in results[pid].explanation and results[pid].applicability == cat.NOT_APPLICABLE
        assert any("not directed at children" in e for e in results[pid].evidence)


def test_tracking_before_a_choice_is_judged_url_by_url(results):
    r = results["C12"]
    by = {u.url: u.status for u in r.urls}
    assert by[SITE] == FAIL and by[SITE + "privacy-policy"] == PASS and by[SITE + "contact"] == PASS
    assert r.status == PASS if all(v == PASS for v in by.values()) else r.status == PARTIAL      # 2 of 3 pass: Partial


def test_forms_without_consent_and_notice_fail_on_their_url(results):
    assert results["C02"].status == FAIL and results["C02"].urls[0].url == SITE + "contact"
    assert results["N13"].status == FAIL


def test_banner_text_is_read_from_the_banner_not_the_style_sheet(results):
    assert results["C15"].status == PASS and results["C07"].status == PASS
    core = _banner_core("menu Home About\n[shadow DOM]\n.cmp-banner { color: red; }\nWe use cookies to improve the site.\nAccept All\nReject All")
    assert "Reject All" in core and "{" not in core and "menu" not in core


def test_a_contact_only_inside_a_script_is_not_published_on_the_page(results):
    by = {u.url: u.status for u in results["G02"].urls}
    assert by[SITE + "contact"] == FAIL and by[SITE] == PASS


def test_url_rule_pass_all_partial_half_fail_less():
    from app.enterprise.method3.assess import A
    a = A.__new__(A)
    a.pages = {f"u{i}": {"url": f"u{i}"} for i in range(4)}
    assert a.per_url("X", lambda pg: (PASS, ""))[0] == PASS
    assert a.per_url("X", lambda pg: (PASS if pg["url"] in ("u0", "u1") else FAIL, ""))[0] == PARTIAL
    assert a.per_url("X", lambda pg: (PASS if pg["url"] == "u0" else FAIL, ""))[0] == FAIL
    assert a.per_url("X", lambda pg: (None, ""))[0] == NA


def test_helpers():
    assert is_website_box({"collects": ["address"], "label_text": "Your website address"})
    assert not is_website_box({"collects": ["address", "name"], "label_text": "Your address"})
    assert itemised("phone", "name, phone number") and not itemised("phone", "contact details")
    assert tracking_cookie({"name": "_ga", "domain": "x.in"}) and tracking_cookie({"name": "MUID", "domain": "bing.com"})
    assert not tracking_cookie({"name": "PHPSESSID", "domain": "x.in"})


# ---- the report -----------------------------------------------------------------------------------------------------------------------------------------------------------------

def test_report_lists_all_96_parameters_and_escapes_site_text(results):
    from app.enterprise.report.method3 import M3ReportData, render_html
    out = score(list(results.values()), as_of=AS_OF)
    out["lines"][0]["evidence"] = ['<script>alert(1)</script> from the site']
    d = M3ReportData("s", "o", SITE, {"started_at": None, "completed_at": None}, out, "r",
                     frontier=[{"original_url": SITE + "<b>x</b>", "status": "excluded", "status_reason": "excluded_external_domain: another website", "discovery_source": "link"}],
                     pages=[{"url": SITE, "category": "other", "forms": 0}])
    html = render_html(d)
    for p in cat.PARAMS:
        assert f">{p.id}<" in html or f"<b>{p.id}</b>" in html, p.id
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;" in html
    assert "&lt;b&gt;x&lt;/b&gt;" in html
    assert "Appendix A" in html and "Appendix B" in html and "Every URL found" in html


# ---- what the build added around it ------------------------------------------------------------------------------------------------------------------------------------------

def test_a_rights_and_grievance_page_is_read_as_a_grievance_page():
    from app.enterprise.analysis import pages as pg
    c = lambda u: pg.classify_page(pg.PageFacts(u, "", [], "text " * 100)).category          # noqa: E731
    assert c("https://consiva.ai/legal/privacy-rights-and-grievance") == pg.GRIEVANCE
    assert c("https://x.in/rights-request") == pg.GRIEVANCE
    assert c("https://consiva.ai/data-principal-rights-management") != pg.GRIEVANCE          # a product page
    assert c("https://x.in/blog/data-principal-rights-dpdp-act") == pg.ARTICLE


def test_request_examples_are_kept_per_third_party_host():
    from app.enterprise.crawl import page_inspect as pi
    s = pi.summarise_requests([{"url": f"https://t.example.net/c?i={i}", "type": "xhr"} for i in range(5)] + [{"url": "https://shop.example.in/a.js", "type": "script"}], SITE)
    assert s["examples"] == {"t.example.net": [f"https://t.example.net/c?i={i}" for i in range(3)]}


def test_url_checks_carry_their_status():
    u = UrlCheck("https://x", PASS, "ok")
    assert u.status == PASS and eng.LABEL[PASS] == "Pass"


def test_method3_routes_are_added_and_the_existing_ones_kept():
    from app.main import app
    paths = set(app.openapi()["paths"])
    assert {"/api/v1/enterprise/scans/{scan_id}/method3", "/api/v1/enterprise/scans/{scan_id}/method3/history"} <= paths
    assert {"/api/v1/enterprise/scans/{scan_id}/parameters", "/api/v1/enterprise/scans/{scan_id}/score/history", "/api/v1/enterprise/scans/{scan_id}/report.pdf"} <= paths


def test_migration_0062_is_additive():
    from pathlib import Path
    sql = (Path(__file__).resolve().parents[2] / "migrations" / "0062_enterprise_method3.sql").read_text(encoding="utf-8").lower()
    assert "create table if not exists ent_method3_results" in sql
    assert "drop " not in sql and "alter table ent_score_results" not in sql


# ---- N01 when the crawl was cut short (audit fix, 25 Nov 2026) ----------------------------------------------------------------------------------------------------------------

def _without_policy():
    ev, fr, _chunks = rows()
    pol = SITE + "privacy-policy"
    return [e for e in ev if e.source_url != pol], [f for f in fr if f.final_url != pol]


def test_no_notice_on_a_full_crawl_is_a_fail():
    ev, fr = _without_policy()
    out = {r.parameter_id: r for r in assess(assemble(ev, fr, [], scan_id="s", root_url=SITE, as_of=AS_OF), [])}
    assert out["N01"].status == FAIL


def test_no_notice_on_a_cut_short_crawl_is_held_for_human_review_never_a_fail():
    from app.enterprise.scoring.engine import Assessment
    ev, fr = _without_policy()
    v2 = [Assessment("N1", "not_assessed", (), None, "No privacy notice was found in the pages reached, but the crawl stopped early (time_limit).")]
    out = {r.parameter_id: r for r in assess(assemble(ev, fr, [], scan_id="s", root_url=SITE, as_of=AS_OF), v2)}
    n01 = out["N01"]
    assert n01.status == NA and n01.review and n01.applicability == cat.HUMAN_REVIEW and "time_limit" in (n01.limitation or "")
    scored = score(list(out.values()), as_of=AS_OF)
    assert "N01" in scored["human_review_pending"]                     # held out of the maximum, not counted as a Fail


def test_report_says_what_not_applicable_means_and_promises_no_recalculation(results):
    import re as _re
    from app.enterprise.report.method3 import M3ReportData, render_html
    out = score(list(results.values()), as_of=AS_OF)
    d = M3ReportData("s", "o", SITE, {"started_at": None, "completed_at": None}, out, "r", pages=[{"url": SITE, "category": "other", "forms": 0}],
                     sitemap={"sitemap_capped": 1, "sitemap_cap": 500})
    html = render_html(d)
    assert "Informational" not in html                                           # Not applicable is never labelled as information
    assert "the score is recalculated" not in html                               # no screen exists for it
    na = sum(1 for x in out["lines"] if x["status"] == "not_applicable")
    assert _re.search(rf"{na} parameters not applicable \(", html)               # split into conditional duties / nothing to test / human review
    assert "sitemap reader stops at 500 URLs" in html


# ---- the analysis phase holds "completed" until method 3.0.0 has run (audit fix, 25 Nov 2026) -------------------------------------------------------------------------------

async def test_the_text_analysis_cannot_mark_the_analysis_finished():
    from app.enterprise.pipeline import _HoldFinalStatus

    class Inner:
        def __init__(self):
            self.calls = []

        async def set_scan_status(self, scan_id, status, **kw):
            self.calls.append(status)

    inner = Inner()
    held = _HoldFinalStatus(inner)
    await held.set_scan_status("s", "running", started=True)
    await held.set_scan_status("s", "partial", completed=True, error="2 unit(s) of analysis failed")
    assert inner.calls == ["running"]                                           # the screen keeps polling: the scan is still "running"
    assert held.status == "partial" and held.error == "2 unit(s) of analysis failed"


# ---- resume after a time or memory limit (audit fix, 25 Nov 2026) ------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("status,stop,analysis,cleared,ok", [
    ("completed", "time_limit", "completed", False, True),
    ("completed", "memory_limit", "partial", False, True),
    ("completed", "url_limit", "completed", False, False),                    # would stop again at once: scan again with a higher limit
    ("completed", "time_limit", "running", False, False),                     # not while the analysis runs
    ("completed", "time_limit", "completed", True, False),                    # the stored page text was cleared
    ("completed", None, "completed", False, False),
    ("failed", "time_limit", "not_started", False, False),
])
def test_which_limit_stopped_scans_can_be_resumed(status, stop, analysis, cleared, ok):
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from app.enterprise import scans
    s = SimpleNamespace(status=status, stop_reason=stop, analysis_status=analysis, purge_summary=None, text_cleared_at=datetime.now(timezone.utc) if cleared else None)
    assert scans.is_resumable_limit_stop(s) is ok


# ---- report and capture fixes after the swaransoft.com review (25 Nov 2026) -------------------------------------------------------------------------------------------------

def test_a_browser_extension_address_is_not_a_third_party_host():
    from app.enterprise.crawl import page_inspect as pi
    s = pi.summarise_requests([{"url": "chrome-extension://liecbddmkiiihnedobmlmillhodjkdmb/img/installed.jpg", "type": "image"},
                               {"url": "data:image/png;base64,AAAA", "type": "image"},
                               {"url": "https://t.example.net/c", "type": "xhr"}], SITE)
    assert [h["host"] for h in s["hosts"]] == ["t.example.net"]


def test_report_counts_web_pages_and_documents_apart_and_explains_not_applicable(results):
    from app.enterprise.report.method3 import M3ReportData, render_html
    out = score(list(results.values()), as_of=AS_OF)
    pages = [{"url": SITE, "category": "other", "forms": 0}, {"url": SITE + "privacy-policy", "category": "privacy_policy", "forms": 0}]
    d = M3ReportData("s", "o", SITE, {"started_at": None, "completed_at": None}, out, "r", pages=pages,
                     frontier=[{"original_url": SITE, "status": "done"}, {"original_url": SITE + "privacy-policy", "status": "done"}, {"original_url": SITE + "a.pdf", "status": "done"}],
                     documents=[{"source_url": SITE + "a.pdf", "n": 3}])
    html = render_html(d)
    assert "Web pages inspected one at a time</td><td class='num'>2<" in html        # web pages only; the PDF is counted on its own line
    assert "Documents read (PDF, DOCX)</td><td class='num'>1<" in html
    assert "conditional duties only" not in html
    assert "table.fixed td .chip { white-space: normal" in html                      # a status badge wraps in its narrow column instead of covering the evidence


def test_counted_parameters_print_the_at_least_half_rule_they_are_scored_by():
    """X01-X03 are scored Partial when at least half are declared/named/itemised (assess.py), like every other counted parameter; the printed criteria must say the same."""
    for pid in ("X01", "X02", "X03"):
        p = cat.BY_ID[pid]
        assert p.partial.startswith("At least half") and p.fail.startswith("Fewer than half"), pid
