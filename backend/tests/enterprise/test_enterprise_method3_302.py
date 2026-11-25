"""Method 3.0.2 fixes (owner, 25 Nov 2026), found on a live single-page site: results must follow what was observed, never report the opposite of the scan's own tests."""

import json

from app.enterprise.analysis.repo import EvRow
from app.enterprise.method3 import catalogue as cat
from app.enterprise.method3.assess import _cookie_service, _embedded_service, assess
from app.enterprise.method3.context import assemble
from app.enterprise.method3.engine import FAIL, NA, PASS

from tests.enterprise.test_enterprise_method3 import AS_OF, SITE, rows


def run(ev, fr, chunks):
    m = assemble(ev, fr, chunks, scan_id="s", root_url=SITE, as_of=AS_OF)
    return {r.parameter_id: r for r in assess(m, [])}


def with_(ev, *extra):
    return [*ev, *extra]


def test_a_further_page_that_does_not_load_is_not_a_cookie_wall():
    ev, fr, chunks = rows()
    rp = EvRow("rp", "consent", SITE, 0, None, "consent_test:reject_persistence", json.dumps(
        {"test": "reject_persistence", "tested": True, "further_pages": [{"url": SITE + "about", "loaded": False, "non_essential": []}]}))
    r = run(with_(ev, rp), fr, chunks)
    assert r["C05"].status == PASS and "not a cookie wall" in (r["C05"].limitation or "")
    # C14 has no loaded further page to judge: held for a person, never a Pass on a page that did not load
    assert r["C14"].status == NA and r["C14"].applicability == cat.HUMAN_REVIEW


def test_c17_is_not_applicable_when_accept_started_nothing():
    ev, fr, chunks = rows()
    wd = EvRow("wd", "consent", SITE, 0, None, "consent_test:withdrawal", json.dumps(
        {"test": "withdrawal", "tested": True, "reopen_control": "Cookie Preferences", "withdraw_control": "withdraw consent", "accepted_non_essential": [],
         "after_withdrawal": [], "further_pages": []}))
    r = run(with_(ev, wd), fr, chunks)
    assert r["C17"].status == NA and "nothing for withdrawal to stop" in r["C17"].explanation


def test_c11_is_not_applicable_without_policy_or_terms():
    ev, fr, chunks = rows()
    ev = [e for e in ev if not (e.kind == "page_text" and "privacy-policy" in e.source_url)]
    chunks = []
    r = run(ev, fr, chunks)
    assert r["C11"].status == NA


def test_banner_used_by_the_tests_but_unread_is_never_reported_missing():
    ev, fr, chunks = rows()
    ev = [EvRow(e.id, e.kind, e.source_url, e.page_number, e.frontier_id, e.detection_method, "Welcome to the shop. We sell tea.") if e.id == "5" else e for e in ev]
    r = run(ev, fr, chunks)
    for pid in ("C09", "C10"):
        assert r[pid].status == NA and r[pid].applicability == cat.HUMAN_REVIEW and "used by the consent tests" in r[pid].explanation
    assert "no consent banner" not in r["C07"].explanation.lower()
    assert r["C15"].status == PASS                     # Reject was found and clicked by the consent test


def test_x02_counts_embedded_services_and_third_party_tracking_cookies():
    ev, fr, chunks = rows()
    net = EvRow("loom", "network", SITE, 0, "f0", "browser_requests", json.dumps({"page_url": SITE, "consent_state": "pre_consent", "non_essential": [],
                                                                                    "hosts": [{"host": "www.loom.com", "third_party": True, "requests": 3}]}))
    r = run(with_(ev, net), fr, chunks)
    assert r["X02"].status != NA and "Loom" in (r["X02"].explanation + " ".join(r["X02"].evidence))


def test_service_names():
    assert _embedded_service("www.loom.com") == "Loom" and _embedded_service("assets.calendly.com") == "Calendly" and _embedded_service("example.com") is None
    assert _cookie_service({"name": "ajs_anonymous_id", "domain": "loom.com", "first_party": False}).endswith("(set by loom.com)")
    assert "(set by" not in _cookie_service({"name": "_ga", "domain": "shop.example.in", "first_party": True})


def test_g02_wording_never_reads_like_a_pass_on_a_fail():
    ev, fr, chunks = rows()
    r = run(ev, fr, chunks)
    g = r["G02"]
    if g.status != PASS:
        assert "is shown on the page." not in g.explanation
    assert g.status in (PASS, FAIL, "partial")


# ---- merged from the privacy-policy-agent copy (25 Nov 2026) ---------------------------------------------------------------------------------------------------------------

def test_the_banner_text_saved_by_the_consent_tests_is_read_by_the_banner_checks():
    """The homepage capture can miss a banner that draws late; the text the Accept / Reject test saved just before its click is the banner the checks read."""
    import json as _json
    from app.enterprise.analysis.repo import EvRow
    from app.enterprise.method3.assess import assess
    from app.enterprise.method3.context import assemble
    from tests.enterprise.test_enterprise_method3 import AS_OF, SITE, rows
    ev, fr, chunks = rows()
    ev = [EvRow(e.id, e.kind, e.source_url, e.page_number, e.frontier_id, e.detection_method,
                e.content.split("\n[shadow DOM]")[0] if e.id == "5" else e.content) for e in ev if e.detection_method != "consent_test:accept"]
    text = "We use cookies\nRead our Privacy Policy\nAccept All\nReject All"
    for name in ("accept", "reject"):
        ev.append(EvRow(f"t-{name}", "consent", SITE, 0, None, f"consent_test:{name}", _json.dumps({"test": name, "tested": True, "non_essential": [], "cookies": [], "banner_text": text})))
    out = {r.parameter_id: r for r in assess(assemble(ev, fr, chunks, scan_id="s", root_url=SITE, as_of=AS_OF), [])}
    assert out["C07"].status == "pass" and out["C15"].status == "pass" and not out["C07"].review


def test_report_matches_a_product_name_to_the_host_description():
    """X02 says "not named: Loom"; section 8 lists the host as "Loom video embed". The table must say "no", not "yes"."""
    from app.enterprise.method3.engine import score
    from app.enterprise.report.method3 import M3ReportData, render_html
    from tests.enterprise.test_enterprise_method3 import AS_OF, SITE, assemble, assess, rows
    ev, fr, chunks = rows()
    out = score(assess(assemble(ev, fr, chunks, scan_id="s", root_url=SITE, as_of=AS_OF), []), as_of=AS_OF)
    for x in out["lines"]:
        if x["parameter_id"] == "X02":
            x["explanation"] = "1 of 2 analytics, advertising or embedded services contacted are named in the policy; not named: Loom."
    d = M3ReportData("s", "o", SITE, {"started_at": None, "completed_at": None}, out, "r", pages=[{"url": SITE, "category": "other", "forms": 0}],
                     hosts=[{"host": "www.loom.com", "vendor": "Loom video embed", "category": "embedded content", "pages": 1, "requests": 3, "examples": [], "states": {"before a choice"}}])
    import re as _re
    row = _re.search(r"<tr><td class='mono'>www\.loom\.com</td>.*?</tr>", render_html(d), _re.S)
    assert row and row.group(0).endswith("<td>no</td></tr>")
