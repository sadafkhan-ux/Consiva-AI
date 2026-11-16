"""The technical parameters TS1 to TS6: each rule, at its boundaries, on plain evidence."""
from datetime import date

from app.enterprise.assess.technical import FIVE_HEADERS, assess_technical
from app.enterprise.scoring.engine import MET, NOT_APPLICABLE, NOT_ASSESSED, NOT_MET, PARTLY
from app.enterprise.security.tls import describe_cert

AS_OF = date(2026, 11, 13)


def page(i, https=True, headers=FIVE_HEADERS, chain=None):
    return (f"p{i}", {"page_url": f"https://x.test/{i}", "redirect_chain": chain or [], "security": {"https": https, "present": {h: "v" for h in headers}, "missing": []}})


def net(i, insecure=0):
    return (f"n{i}", {"insecure_requests": insecure, "insecure_examples": ["http://cdn.x.test/a.js"] if insecure else []})


def cook(i, secure=True, same_site="Lax", ident="a|x.test|/"):
    return (f"c{i}", {"cookies": [{"identity": ident, "secure": secure, "same_site": same_site}]})


def tls(**kw):
    base = {"reachable": True, "verified": True, "negotiated": "TLSv1.3", "tls13_supported": True, "not_after": "2027-03-25T12:00:00+00:00"}
    return ("t1", {**base, **kw})


def run(**kw):
    args = {"pages": [page(1), page(2)], "cookies": [], "networks": [net(1), net(2)], "tls": tls(), "as_of": AS_OF, **kw}
    return {a.parameter_id: a for a in assess_technical(**args)}


def test_everything_in_order_is_met_where_there_is_something_to_assess():
    r = run(cookies=[cook(1)], pages=[page(1, chain=[{"url": "http://x.test/a", "status": 301}]), page(2)])
    assert {k: v.state for k, v in r.items()} == {"TS1": MET, "TS2": MET, "TS3": MET, "TS4": MET, "TS5": MET, "TS6": MET}


def test_ts1_a_page_over_http_or_a_clear_text_request_is_not_met():
    assert run(pages=[page(1), page(2, https=False)])["TS1"].state == NOT_MET
    r = run(networks=[net(1), net(2, insecure=2)])["TS1"]
    assert r.state == NOT_MET and "http://cdn.x.test/a.js" in r.note


def test_ts1_evidence_that_predates_mixed_content_recording_is_not_assessed_not_passed():
    assert run(networks=[("n", {"total_requests": 3})])["TS1"].state == NOT_ASSESSED
    assert run(networks=[])["TS1"].state == NOT_ASSESSED
    assert run(pages=[], networks=[])["TS1"].state == NOT_ASSESSED


def test_ts2_the_certificate_boundary_is_more_than_30_days():
    assert run(tls=tls(not_after="2026-12-14T00:00:00+00:00"))["TS2"].state == MET          # 31 days
    assert run(tls=tls(not_after="2026-12-13T00:00:00+00:00"))["TS2"].state == NOT_MET      # exactly 30
    assert run(tls=tls(verified=False, verify_error="certificate has expired"))["TS2"].state == NOT_MET
    assert run(tls=None)["TS2"].state == NOT_ASSESSED and run(tls=tls(reachable=False))["TS2"].state == NOT_ASSESSED


def test_ts3_tls_12_and_13_are_met_older_is_not():
    assert run(tls=tls(negotiated="TLSv1.2", tls13_supported=False))["TS3"].state == MET
    assert run(tls=tls(negotiated="TLSv1.1"))["TS3"].state == NOT_MET
    assert run(tls=None)["TS3"].state == NOT_ASSESSED


def test_ts4_counts_only_headers_every_page_sent():
    assert run(pages=[page(1), page(2, headers=FIVE_HEADERS[:4])])["TS4"].state == PARTLY          # one page lacks Permissions-Policy: 4 of 5
    assert run(pages=[page(1, headers=FIVE_HEADERS[:3]), page(2, headers=FIVE_HEADERS[:3])])["TS4"].state == PARTLY
    assert run(pages=[page(1, headers=FIVE_HEADERS[:2])])["TS4"].state == NOT_MET
    assert run(pages=[page(1, headers=())])["TS4"].state == NOT_MET
    assert run(pages=[])["TS4"].state == NOT_ASSESSED


def test_ts5_no_cookie_is_not_applicable_and_a_missing_attribute_counts_against():
    assert run(cookies=[])["TS5"].state == NOT_APPLICABLE
    assert run(cookies=[cook(1, secure=False)])["TS5"].state == NOT_MET
    mixed = run(cookies=[cook(1, ident="a|x.test|/"), cook(2, secure=False, ident="b|x.test|/")])["TS5"]
    assert mixed.state == PARTLY and "1 of 2" in mixed.note
    assert run(cookies=[cook(1, same_site="unspecified")])["TS5"].state == NOT_MET


def test_ts5_the_same_cookie_on_many_pages_is_counted_once():
    r = run(cookies=[cook(i) for i in range(10)])["TS5"]
    assert r.state == MET and "1 of 1" in r.note


def test_ts6_an_upgrade_to_https_is_fine_a_step_down_is_not():
    up = page(1, chain=[{"url": "http://x.test/a", "status": 301}])
    down = page(2, chain=[{"url": "https://x.test/a", "status": 302}])
    down[1]["page_url"] = "http://x.test/b"
    assert run(pages=[up])["TS6"].state == MET
    assert run(pages=[up, down])["TS6"].state == NOT_MET
    assert run(pages=[page(1)])["TS6"].state == NOT_APPLICABLE


def test_every_result_that_earns_or_loses_points_names_its_evidence():
    for a in run(cookies=[cook(1)], pages=[page(1, chain=[{"url": "http://x.test/a", "status": 301}])]).values():
        assert a.state in (NOT_APPLICABLE, NOT_ASSESSED) or a.evidence_refs


def test_the_certificate_is_described_without_a_clock():
    d = describe_cert({"notAfter": "Mar 25 12:00:00 2027 GMT", "notBefore": "Nov 25 12:00:00 2025 GMT", "issuer": ((("organizationName", "GoDaddy.com, Inc."),),),
                       "subject": ((("commonName", "x.test"),),), "subjectAltName": (("DNS", "x.test"), ("DNS", "www.x.test"), ("IP Address", "1.2.3.4"))})
    assert d["not_after"] == "2027-03-25T12:00:00+00:00" and d["issuer"] == "GoDaddy.com, Inc." and d["subject"] == "x.test" and d["alt_names"] == ["x.test", "www.x.test"]
