"""Deep inspection, the parts that need no browser: cookies, requests, headers, soft 404 and forms. The browser parts are exercised by a real scan (see the scan record)."""
import ast
from pathlib import Path

from app.enterprise.crawl import page_inspect as pi

SITE = "https://www.example.com/"


def cookie(**kw):
    return {"name": "_ga", "domain": ".example.com", "path": "/", "value": "GA1.2.123456", "expires": 1900000000, "secure": True, "httpOnly": False, "sameSite": "Lax", **kw}


def test_a_cookie_is_its_name_domain_and_path_and_a_dot_makes_no_difference():
    rows = pi.normalize_cookies([cookie(domain=".example.com"), cookie(domain="example.com"), cookie(domain="EXAMPLE.com")], SITE)
    assert len(rows) == 1 and rows[0]["identity"] == "_ga|example.com|/"


def test_a_different_path_or_domain_is_a_different_cookie():
    rows = pi.normalize_cookies([cookie(), cookie(path="/shop"), cookie(domain=".ads.net")], SITE)
    assert {r["identity"] for r in rows} == {"_ga|example.com|/", "_ga|example.com|/shop", "_ga|ads.net|/"}


def test_the_value_is_never_kept_only_its_length_and_a_short_fingerprint():
    row = pi.normalize_cookies([cookie(value="user-12345-secret")], SITE)[0]
    assert "user-12345-secret" not in str(row) and row["value_length"] == 17 and len(row["value_fingerprint"]) == 16


def test_session_cookies_first_party_and_attributes_are_recorded():
    rows = {r["name"]: r for r in pi.normalize_cookies([cookie(name="sid", expires=-1, httpOnly=True, sameSite=None), cookie(name="_fbp", domain=".facebook.com")], SITE)}
    assert rows["sid"]["session"] is True and rows["sid"]["expires"] is None and rows["sid"]["http_only"] is True and rows["sid"]["same_site"] == "unspecified"
    assert rows["sid"]["first_party"] is True and rows["_fbp"]["first_party"] is False
    assert rows["sid"]["consent_state"] == "pre_consent"


def test_requests_are_counted_by_host_with_third_parties_first():
    entries = [{"url": "https://www.example.com/a.js", "type": "script"}, {"url": "https://cdn.example.com/b.png", "type": "image"},
               {"url": "https://www.google-analytics.com/collect", "type": "xhr"}, {"url": "https://www.google-analytics.com/collect?x=1", "type": "xhr"}, {"url": "data:image/png;base64,AA", "type": "image"}]
    s = pi.summarise_requests(entries, SITE)
    assert s["total_requests"] == 4 and s["third_party_hosts"] == 1 and s["third_party_requests"] == 2
    assert s["hosts"][0]["host"] == "www.google-analytics.com" and s["hosts"][0]["requests"] == 2
    own = {h["host"]: h["third_party"] for h in s["hosts"]}
    assert own["cdn.example.com"] is False          # a subdomain of the site is the site's own


def test_security_headers_record_presence_and_absence_without_a_verdict():
    s = pi.security_headers({"Strict-Transport-Security": "max-age=1", "Content-Type": "text/html"}, "https://x.test/")
    assert s["https"] is True and s["present"] == {"strict-transport-security": "max-age=1"}
    assert "content-security-policy" in s["missing"] and "strict-transport-security" not in s["missing"]
    assert pi.security_headers({}, "http://x.test/")["https"] is False


def test_a_soft_404_is_a_200_that_reads_as_not_found_and_has_little_else():
    assert pi.is_soft_404("Page not found", "Sorry, we couldn't find that.", 200) is True
    assert pi.is_soft_404("Oops", "404. The page you asked for does not exist.", 200) is True


def test_a_real_page_or_a_real_error_status_is_not_a_soft_404():
    assert pi.is_soft_404("Privacy Policy", "We collect your name. " * 100, 200) is False
    assert pi.is_soft_404("About", "Our story. Not found anywhere else: our people. " + "x" * 2000, 200) is False        # long page
    assert pi.is_soft_404("Not found", "short", 404) is False                                                              # a real 404 is handled as an HTTP failure


def test_forms_name_what_they_collect_and_keep_hidden_fields_apart():
    raw = [{"action": "/contact", "method": "POST", "privacy_link": True, "text": "Contact us",
            "fields": [{"type": "text", "name": "full_name"}, {"type": "email", "name": "e"}, {"type": "tel", "name": "p"}, {"type": "checkbox", "name": "agree", "label": "I agree"},
                       {"type": "hidden", "name": "csrf"}, {"type": "textarea", "name": "message"}]}]
    f = pi.normalize_forms(raw, "https://x.test/contact")[0]
    assert f["collects"] == ["email", "message", "name", "phone"] and f["hidden_field_count"] == 1 and len(f["fields"]) == 5
    assert f["method"] == "post" and f["privacy_link"] is True and len(f["consent_controls"]) == 1 and f["action_host"] == ""


def test_the_inspection_module_depends_on_no_scoring_and_no_frozen_logic_beyond_a_domain_helper():
    tree = ast.parse((Path(page_inspect_path := pi.__file__)).read_text(encoding="utf-8"))
    imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module} | {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not any(m.startswith(("app.scoring", "app.rules", "app.policy", "app.llm")) for m in imported)
    assert {m for m in imported if m.startswith("app.")} == {"app.scanner._domain"}


def test_clear_text_requests_are_counted_once_each_and_shown_as_examples():
    entries = [{"url": "http://cdn.example.com/a.js#x", "type": "script"}, {"url": "http://cdn.example.com/a.js", "type": "script"}, {"url": "https://www.example.com/b.js", "type": "script"}]
    s = pi.summarise_requests(entries, SITE)
    assert s["insecure_requests"] == 1 and s["insecure_examples"] == ["http://cdn.example.com/a.js"]
    assert pi.summarise_requests([{"url": "https://a.example.com/x", "type": "script"}], SITE)["insecure_requests"] == 0
