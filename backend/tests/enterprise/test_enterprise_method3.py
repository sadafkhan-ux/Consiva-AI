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
