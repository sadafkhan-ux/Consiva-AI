"""The Enterprise PDF report: data gathering, HTML reconciliation, escaping, attribution, score wording, and (where Chromium runs) a real PDF."""
from __future__ import annotations

import html as htmllib
import json
import re
import uuid
from dataclasses import replace
from datetime import date, datetime, timezone

import pytest

from app.enterprise.report import data as report_data
from app.enterprise.report.data import assemble, gather
from app.enterprise.report.html import NOT_RECORDED, SECTIONS, STATE_LABEL, render_html
from app.enterprise.scoring.catalogue import PARAMETERS
from app.enterprise.scoring.engine import Assessment, score

SCAN = uuid.UUID("11111111-2222-3333-4444-555555555555")
NOW = datetime(2026, 11, 13, 9, 30, tzinfo=timezone.utc)
A, B, C = "https://example.test/", "https://example.test/privacy", "https://example.test/about"
HOSTILE_TITLE = "<script>alert('x')</script> Privacy"
HOSTILE_QUOTE = "we collect ${x} and {{y}}"


def ev_id(name: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, name))


def decl(**over) -> dict:
    d = {
        "crawl_status": "completed", "analysis_status": "completed", "completion": "complete",
        "urls": {"discovered": 14, "eligible": 12, "crawled": 11, "analysed": 11, "pending": 0, "excluded": 2, "failed": 1, "redirected": 1, "inaccessible": 0, "unsupported": 0,
                 "duplicates_merged": 3, "awaiting_analysis": 0, "analysis_failed": 0, "excluded_by_reason": {"robots_txt": 2}},
        "documents": {"discovered": 2, "extracted": 1, "partly_extracted": 1, "unreadable": 0, "extraction_not_recorded": 0},
        "evidence": {"records": 60, "sources": 12, "analysed": 11}, "analysis_tasks": {"done": 20, "failed": 0, "pending": 0},
        "parameters": {"total": 39, "assessed": 30, "not_assessed": 2, "not_applicable": 2, "awaiting_human_review": 1, "needs_records": 2, "recorded": 39},
    }
    d.update(over)
    return d


def states_for_rich() -> dict[str, str]:
    base = {p.id: "met" for p in PARAMETERS}
    base.update({"C4": "not_met", "C8": "not_met", "N2": "partly_met", "X1": "partly_met", "S1": "needs_records", "T1": "needs_records", "E1": "human_review",
                 "K1": "not_applicable", "M1": "not_applicable", "TS4": "partly_met"})
    return base


def assessments_from(states: dict[str, str]) -> list[Assessment]:
    return [Assessment(pid, st, (f"ev:{ev_id(pid)}",) if st in ("met", "partly_met", "not_met") else (), None, f"note for {pid}") for pid, st in states.items()]


def raw_score(states: dict[str, str], as_of: date) -> tuple[dict, list[dict]]:
    res = score(assessments_from(states), as_of=as_of)
    rows = [{"parameter_id": a.parameter_id, "state": a.state, "evidence_refs": list(a.evidence_refs), "confidence": None, "note": a.note, "assessor": "rule:consent", "model": None,
             "reviewed_by": None, "review_reason": None, "created_at": NOW} for a in assessments_from(states)]
    return {"id": str(uuid.UUID(int=7)), "version": res.version, "as_of": res.as_of, "score": res.score, "coverage": res.coverage, "basis": res.basis, "result": res.to_dict(), "created_at": NOW}, rows


def cookie_row(page: str, state: str = "pre_consent", name: str = "_ga", domain: str = ".example.test") -> dict:
    return {"id": uuid.uuid4(), "kind": "cookie", "source_url": page, "detection_method": "browser_cookies", "captured_at": NOW, "content": json.dumps({
        "page_url": page, "consent_state": state, "cookies": [{"identity": f"{name}|{domain.lstrip('.')}|/", "name": name, "domain": domain.lstrip("."), "path": "/", "expires": 1893456000.0, "session": False,
                                                              "secure": True, "http_only": False, "same_site": "Lax", "first_party": True, "consent_state": state, "value_length": 20, "value_fingerprint": "ab"}]})}


def network_row(page: str, non_essential: list[dict] | None = None) -> dict:
    return {"id": uuid.uuid4(), "kind": "network", "source_url": page, "detection_method": "browser_requests", "captured_at": NOW, "content": json.dumps({
        "page_url": page, "consent_state": "pre_consent", "total_requests": 5, "insecure_requests": 1, "insecure_examples": ["http://cdn.example.test/a.js"], "third_party_hosts": 1,
        "third_party_requests": 3, "non_essential": non_essential or [],
        "hosts": [{"host": "example.test", "third_party": False, "requests": 2, "types": {"document": 1, "script": 1}},
                  {"host": "www.google-analytics.com", "third_party": True, "requests": 3, "types": {"script": 1, "xhr": 2}}]})}


def header_row(page: str, hostile: bool = False) -> dict:
    return {"id": uuid.uuid4(), "kind": "header", "source_url": page, "detection_method": "response_headers", "captured_at": NOW, "content": json.dumps({
        "page_url": page, "http_status": 200, "redirect_chain": [{"url": "http://example.test/", "status": 301}],
        "security": {"https": True, "present": {"strict-transport-security": "max-age=1", "x-content-type-options": "nosniff"}, "missing": ["content-security-policy", "referrer-policy", "permissions-policy", "x-frame-options"]},
        "inspection": {"iframes": [{"src": "https://player.vendor.test/embed", "title": HOSTILE_TITLE if hostile else "Video player"}]}})}


def form_row(page: str) -> dict:
    return {"id": uuid.uuid4(), "kind": "form", "source_url": page, "detection_method": "dom_forms", "captured_at": NOW, "content": json.dumps({"page_url": page, "forms": [{
        "index": 0, "page_url": page, "action": "https://example.test/contact", "action_host": "example.test", "method": "post",
        "fields": [{"name": "email", "type": "email", "required": True}, {"name": "msg", "type": "textarea", "required": False}], "hidden_field_count": 1, "collects": ["email", "message"],
        "consent_controls": [{"name": "agree", "type": "checkbox", "label": "I agree to the privacy policy"}], "privacy_link": True, "text": "Contact us"}]})}


def consent_rows() -> list[dict]:
    tests = {
        "accept": {"test": "accept", "state": "post_accept", "tested": True, "page_url": A, "cookies": [{"name": "_fbp", "domain": "example.test", "path": "/", "secure": True, "http_only": False, "same_site": "Lax",
                                                                                                        "session": True, "expires": None, "first_party": True}],
                   "non_essential": [{"vendor": "Meta", "category": "marketing", "host": "connect.facebook.net"}], "requests": {"total_requests": 9, "hosts": [
                       {"host": "connect.facebook.net", "third_party": True, "requests": 4, "types": {"script": 2}}]}},
        "reject": {"test": "reject", "state": "post_reject", "tested": False, "reason": "control_not_found"},
        "reject_persistence": {"test": "reject_persistence", "tested": True, "non_essential_after_reject": [], "further_pages": [{"url": B, "loaded": True, "non_essential": [{"vendor": "Google Analytics", "category": "analytics", "host": "www.google-analytics.com"}]}]},
        "withdrawal": {"test": "withdrawal", "tested": True, "reopen_control": "Cookie settings", "withdraw_control": "Withdraw", "accepted_non_essential": [{"vendor": "Meta", "category": "marketing", "host": "connect.facebook.net"}],
                       "after_withdrawal": [{"vendor": "Meta", "category": "marketing", "host": "connect.facebook.net"}], "further_pages": []},
        "purposes": {"test": "purposes", "tested": True, "purposes": [{"kind": "analytics", "label": "Analytics", "observed": [], "verdict": "respected"}]},
        "button_comparison": {"test": "button_comparison", "comparable": False, "why": "Accept is filled and larger", "accept": {"tag": "BUTTON", "w": 120, "h": 40, "font": 14, "filled": True}, "reject": {"tag": "A", "w": 60, "h": 20, "font": 11, "filled": False}},
    }
    return [{"id": uuid.uuid4(), "kind": "consent", "source_url": A, "detection_method": f"consent_test:{n}", "captured_at": NOW, "content": json.dumps(d)} for n, d in tests.items()]


def finding(key: str, pid: str, sev: str, urls: list[str], **over) -> dict:
    f = {"id": uuid.uuid4(), "finding_key": key, "parameter_id": pid, "title": f"Finding {key}", "description": "desc", "severity": sev, "state": "not_met", "legal_refs": ["s.6(1)"],
         "affected_urls": urls, "affected_count": len(urls), "evidence_refs": [f"ev:{ev_id(pid)}"], "expected": "no tracker before choice", "observed": "a tracker fired", "reason": "because", "confidence": "high",
         "recommendation": f"Fix {pid} now", "human_review": False, "verification_status": "unverified", "schedule_item": 7, "penalty_ceiling_cr": 50}
    f.update(over)
    return f


def make_raw(*, hostile: bool = False, states: dict | None = None, as_of: date = date(2026, 11, 13), declaration: dict | None = None) -> dict:
    sc, arows = raw_score(states or states_for_rich(), as_of)
    evidence = [{"id": uuid.UUID(ev_id(p)), "kind": "page_text", "source_url": B, "page_number": 0, "content_hash": "ab" * 32, "detection_method": "dom_render", "captured_at": NOW}
                for p in ("C4", "C8", "N2")]
    return {
        "scan": {"id": SCAN, "website_url": "https://example.test/", "domain": "example.test", "status": "completed", "analysis_status": "completed", "options": {"max_urls": 0, "concurrency": 4},
                 "stop_reason": None, "error": None, "created_at": NOW, "started_at": NOW, "completed_at": NOW, "analysis_started_at": NOW, "analysis_completed_at": NOW, "analysis_error": None, "previous_scan_id": None},
        "declaration": declaration or decl(),
        "urls": [{"original_url": u, "final_url": u, "depth": i, "status": "done", "analysis_status": "done", "http_status": 200, "status_reason": None, "discovery_sources": ["seed"]} for i, u in enumerate((A, B, C))],
        "policies": [{"source_url": B, "content_hash": "cd" * 32, "byte_size": 4000, "title": HOSTILE_TITLE if hostile else "Privacy Policy", "opening": "Privacy Policy\nwe care"}],
        "policy_docs": [{"source_url": "https://example.test/cookies.pdf", "pages": 3, "size": 9000, "content_hash": "ef" * 32}],
        "observations": [header_row(A, hostile), header_row(B), cookie_row(A), cookie_row(B), network_row(A, [{"vendor": "Google Analytics", "category": "analytics", "host": "www.google-analytics.com"}]),
                         network_row(B), form_row(A), {**header_row(A), "id": uuid.uuid4(), "detection_method": "tls_probe", "content": json.dumps({
                             "probe": "tls", "host": "example.test", "reachable": True, "verified": True, "negotiated": "TLSv1.3", "tls13_supported": True, "issuer": "Example CA", "subject": "example.test",
                             "not_before": "2026-01-01T00:00:00+00:00", "not_after": "2027-03-25T12:00:00+00:00"})}] + consent_rows()
                        + ([cookie_row(C, name="<img src=x onerror=alert(1)>")] if hostile else []),
        "documents": [{"original_url": "https://example.test/policy.pdf", "final_url": None, "content_type": "application/pdf", "status": "done", "extraction_status": "extracted", "extraction_method": "pdf_text",
                       "analysis_status": "done", "status_reason": "3 page(s) read", "pages_read": 3}],
        "assessments": arows, "score": sc,
        "findings": [finding("C4:pre", "C4", "high", [A, B], observed=HOSTILE_QUOTE if hostile else "a tracker fired"), finding("C8:w", "C8", "medium", [A]),
                     finding("N2:items", "N2", "low", [], penalty_ceiling_cr=None, schedule_item=None, human_review=True, state="partly_met")],
        "evidence": evidence,
    }


def build(**kw):
    return assemble(make_raw(**kw), generated_at=NOW)


def visible_text(doc: str) -> str:
    body = re.sub(r"(?is)<(style|script|head)\b.*?</\1>", " ", doc)
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", body)))


def section_html(doc: str, n: int) -> str:
    start = doc.index(f'<section id="s{n}">')
    end = doc.index(f'<section id="s{n + 1}">') if n < 20 else doc.index("</body>")
    return doc[start:end]


def keyed(doc: str) -> dict[str, str]:
    return {m.group(1): htmllib.unescape(m.group(2)) for m in re.finditer(r'data-k="([^"]+)">([^<]*)<', doc)}


# ---- (a) data gathering ------------------------------------------------------------------------------------------------------------------------------------------------

class _Result:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self

    def all(self):
        return self.rows


class FakeDB:
    """Answers each tagged query with canned rows and records what was asked."""

    def __init__(self, raw: dict, scan_missing: bool = False):
        self.raw, self.calls, self.scan_missing = raw, [], scan_missing

    async def execute(self, stmt, params=None):
        tag = re.search(r"/\* report:(\w+) \*/", str(stmt)).group(1)
        self.calls.append((tag, params))
        table = {"scan": [] if self.scan_missing else [self.raw["scan"]], "urls": self.raw["urls"], "policies": self.raw["policies"], "policy_docs": self.raw["policy_docs"],
                 "observations": self.raw["observations"], "documents": self.raw["documents"], "assessments": self.raw["assessments"],
                 "score": [self.raw["score"]] if self.raw["score"] else [], "findings": self.raw["findings"], "evidence": self.raw["evidence"]}
        return _Result(table[tag])


async def test_gather_reads_only_the_enterprise_tables_and_builds_the_data(monkeypatch):
    raw = make_raw()

    async def fake_declaration(db, scan_id):
        assert scan_id == SCAN
        return raw["declaration"]

    monkeypatch.setattr(report_data, "coverage_declaration", fake_declaration)
    db = FakeDB(raw)
    d = await gather(db, SCAN)
    assert {t for t, _ in db.calls} == {"scan", "urls", "policies", "policy_docs", "observations", "documents", "assessments", "score", "findings", "evidence"}
    assert all(p.get("scan_id") == SCAN for _, p in db.calls)
    asked = next(p for t, p in db.calls if t == "evidence")
    assert set(asked["ids"]) >= {ev_id("C4"), ev_id("C8")}
    assert d.scan.website_url == "https://example.test/" and d.declaration["urls"]["discovered"] == 14
    assert len(d.parameters) == 39 and d.score.score == raw["score"]["score"] and len(d.findings) == 3
    assert [p.url for p in d.policies] == [B, "https://example.test/cookies.pdf"] or {p.url for p in d.policies} == {B, "https://example.test/cookies.pdf"}
    assert d.legal.sources[0].gazette_identifier == "CG-DL-E-12082023-248045"
    assert d.consent[0].name == "accept" and len(d.consent) == 6
    assert d.security.tls["negotiated"] == "TLSv1.3"


async def test_gather_of_a_missing_scan_raises(monkeypatch):
    with pytest.raises(LookupError):
        await gather(FakeDB(make_raw(), scan_missing=True), SCAN)


def test_a_figure_that_cannot_be_known_is_not_recorded_never_zero_or_blank():
    raw = make_raw(declaration=decl(evidence={"records": 5, "sources": 2, "analysed": None}))
    raw["score"] = None
    raw["assessments"] = []
    d = assemble(raw, generated_at=NOW)
    doc = render_html(d)
    assert keyed(doc)["evidence.analysed"] == NOT_RECORDED
    assert d.score is None and all(p.state == "not_assessed" and p.earned is None for p in d.parameters)
    s14 = section_html(doc, 14)
    assert NOT_RECORDED in s14 and "No score has been calculated" in section_html(doc, 15)


# ---- (b) reconciliation ------------------------------------------------------------------------------------------------------------------------------------------------

def test_every_number_in_the_html_equals_the_data_object():
    d = build()
    doc = render_html(d)
    k = keyed(doc)
    assert k["score"] == k["score_detail"] == str(d.score.score)
    assert k["coverage"] == k["coverage_detail"] == str(d.score.coverage)
    assert k["params_total"] == k["params_listed"] == k["catalogue_parameters"] == "39"
    assert k["points_total"] == k["catalogue_points"] == "100"
    for prefix, src in (("urls", d.declaration["urls"]), ("documents", d.declaration["documents"]), ("parameters", d.declaration["parameters"])):
        for key, value in src.items():
            if not isinstance(value, dict):
                assert k[f"{prefix}.{key}"] == str(value), f"{prefix}.{key}"
    assert {k[f"excluded.{r}"] for r in d.declaration["urls"]["excluded_by_reason"]} == {"2"}
    assert k["evidence.records"] == "60" and k["tasks.done"] == "20"
    assert k["urls_discovered"] == k["urls_discovered_inv"] == k["urls_total"] == "14" and k["urls_shown"] == "3"
    assert k["cookies_total"] == k["cookies_inventory"] == str(len(d.cookies)) and k["forms_total"] == k["forms_inventory"] == "1"
    assert k["findings_total"] == str(len(d.findings)) and k["pre_consent_pages"] == "1" and k["pages_observed"] == "2"
    assert k["evidence_total"] == str(d.evidence_total)


def test_sums_are_internally_consistent():
    d = build()
    k = keyed(render_html(d))
    counts = {s: sum(1 for p in d.parameters if p.state == s) for s in STATE_LABEL}
    assert all(k[f"state.{s}"] == str(n) for s, n in counts.items()) and sum(counts.values()) == 39
    assert sum(p.points for p in d.parameters) == 100 and sum(g["points_available"] for g in d.score.by_group.values()) == 100
    assert round(sum(p.earned for p in d.parameters), 6) == round(d.score.points_earned, 6)
    assert round(sum(p.in_play for p in d.parameters), 6) == round(d.score.points_in_play, 6)
    assert d.score.coverage == round(d.score.points_in_play)
    assert d.score.score == int(d.score.points_earned + 0.5)          # the score is the points earned, out of 100


def test_every_parameter_and_finding_row_matches_the_data():
    d = build()
    doc = render_html(d)
    rows = re.findall(r"<tr>(.*?)</tr>", section_html(doc, 14), re.S)
    by_id = {re.search(r"<b>([A-Z]+\d+)</b>", r).group(1): r for r in rows if re.search(r"<td class=\"\"><b>[A-Z]+\d+</b>", r)}
    assert len(by_id) == 39
    for p in d.parameters:
        assert f"s-{p.state}" in by_id[p.id] and STATE_LABEL[p.state] in by_id[p.id], p.id
    s16 = section_html(doc, 16)
    k = keyed(doc)
    for f in d.findings:
        assert htmllib.escape(f.title) in s16
        assert k[f"affected.{f.finding_key}"] == str(f.affected_count) if f.affected_count else True
        if f.penalty_ceiling_cr is not None:
            assert k[f"ceiling.{f.finding_key}"] == str(f.penalty_ceiling_cr)
    assert [f.severity for f in d.findings] == ["high", "medium", "low"]


# ---- (c) no placeholders ----------------------------------------------------------------------------------------------------------------------------------------------

FORBIDDEN = ("${", "{{", "}}", "undefined", "NaN", "None", "null", "[object", "TODO")


@pytest.mark.parametrize("variant", ["rich", "no_score", "empty"])
def test_no_placeholder_or_leaked_artefact_in_the_visible_text(variant):
    if variant == "rich":
        d = build()
    elif variant == "no_score":
        d = build(states={"N1": "met", "N2": "not_met"})
    else:
        raw = make_raw()
        raw.update(score=None, assessments=[], findings=[], observations=[], policies=[], policy_docs=[], documents=[], urls=[], evidence=[])
        raw["declaration"] = decl(evidence={"records": 0, "sources": 0, "analysed": None}, completion="partial")
        d = assemble(raw, generated_at=NOW)
    text = visible_text(render_html(d))
    for token in FORBIDDEN:
        assert token not in text, f"{token!r} leaked in the {variant} report: ...{text[max(0, text.index(token) - 60): text.index(token) + 60]}..."


def test_the_document_is_self_contained():
    doc = render_html(build())
    assert "<script" not in doc.lower()
    assert not re.search(r"""(src|href)\s*=\s*["']https?://""", doc) and "@import" not in doc and "url(" not in doc
    assert 'size: A4' in doc


# ---- (d) escaping ------------------------------------------------------------------------------------------------------------------------------------------------------

def test_hostile_content_is_escaped_everywhere():
    d = build(hostile=True)
    doc = render_html(d)
    assert "<script" not in doc.lower() and "<img src=x" not in doc and "onerror=alert(1)>" not in doc.replace("&gt;", "")
    assert htmllib.escape(HOSTILE_TITLE) in doc and "&lt;script&gt;alert(&#x27;x&#x27;)&lt;/script&gt;" in doc
    assert HOSTILE_QUOTE in visible_text(doc)             # shown literally as text, not interpreted
    assert "&lt;img src=x onerror=alert(1)&gt;" in doc


# ---- (e) attribution ---------------------------------------------------------------------------------------------------------------------------------------------------

def test_a_cookie_seen_on_two_pages_is_one_row_listing_both():
    d = build()
    ga = [c for c in d.cookies if c.name == "_ga"]
    assert len(ga) == 1 and ga[0].pages == [A, B] and ga[0].consent_states == ["pre_consent"]
    s8 = section_html(render_html(d), 8)
    assert s8.count(">_ga<") == 1
    row = next(r for r in re.findall(r"<tr>(.*?)</tr>", s8, re.S) if ">_ga<" in r)
    assert "2 page(s)" in row and htmllib.escape(A) in row and htmllib.escape(B) in row


def test_cookie_identity_ignores_the_leading_dot_and_case_of_the_domain():
    raw = make_raw()
    raw["observations"] += [cookie_row(C, domain="EXAMPLE.test")]
    d = assemble(raw, generated_at=NOW)
    assert len([c for c in d.cookies if c.name == "_ga"]) == 1 and [c for c in d.cookies if c.name == "_ga"][0].pages == [A, B, C]


def test_cookies_from_the_consent_tests_carry_their_own_state():
    d = build()
    fbp = next(c for c in d.cookies if c.name == "_fbp")
    assert fbp.consent_states == ["post_accept"] and fbp.pages == [A]


def test_a_finding_names_only_the_urls_it_was_raised_on():
    d = build()
    s16 = section_html(render_html(d), 16)
    blocks = s16.split('<div class="finding ')[1:]
    c8 = next(b for b in blocks if "Finding C8:w" in b)
    assert htmllib.escape(A) in c8 and htmllib.escape(B) not in c8 and htmllib.escape(C) not in c8
    c4 = next(b for b in blocks if "Finding C4:pre" in b)
    assert htmllib.escape(A) in c4 and htmllib.escape(B) in c4 and htmllib.escape(C) not in c4


# ---- (f) the score is shown whatever the coverage; "No score" only when nothing was assessed ---------------------------------------------------------------------------------

def test_the_score_is_shown_even_when_coverage_is_low_and_no_minimum_is_mentioned():
    d = build(states={"N1": "met", "N2": "not_met", "C4": "met"})
    assert d.score.score is not None and d.score.coverage < 60
    doc = render_html(d)
    s15, s1 = section_html(doc, 15), section_html(doc, 1)
    assert "No score is shown" not in s15 and "No score" not in visible_text(s1)
    assert str(d.score.score) in visible_text(s1)
    assert "minimum coverage" not in visible_text(doc).lower() and "60%" not in visible_text(s15)


# ---- (g) readiness vs assessment wording ------------------------------------------------------------------------------------------------------------------------------

def test_readiness_basis_is_labelled_readiness_and_names_what_is_not_yet_in_force():
    d = build()
    assert d.score.basis == "readiness" and d.score.not_yet_in_force
    doc = render_html(d)
    s15 = visible_text(section_html(doc, 15))
    assert "Readiness score" in visible_text(section_html(doc, 1)) and "Basis: readiness" in s15 and "never a finding of breach" in s15
    assert all(pid in s15 for pid in d.score.not_yet_in_force)
    assert "13 May 2027" in visible_text(doc) or "2027-05-13" in visible_text(doc)


def test_assessment_basis_is_labelled_assessment_not_readiness():
    d = build(as_of=date(2027, 6, 1))
    assert d.score.basis == "assessment" and not d.score.not_yet_in_force
    doc = render_html(d)
    s15 = visible_text(section_html(doc, 15))
    assert "Basis: assessment" in s15 and "readiness" not in s15.lower()
    assert "Assessment score" in visible_text(section_html(doc, 1)) and "Readiness score" not in visible_text(doc)


# ---- (h) structure -----------------------------------------------------------------------------------------------------------------------------------------------------

def test_all_twenty_section_headings_are_present_in_order():
    doc = render_html(build())
    heads = re.findall(r"<h2>(.*?)</h2>", doc)
    assert heads == [f"{i}. {t}" for i, t in enumerate(SECTIONS, 1)] and len(heads) == 20
    assert [m.group(1) for m in re.finditer(r'<section id="s(\d+)">', doc)] == [str(i) for i in range(1, 21)]


def test_the_first_page_says_it_is_a_readiness_assessment_not_a_certification():
    doc = render_html(build())
    first = visible_text(doc[: doc.index('<section id="s2">')])
    assert "website-observable readiness assessment" in first and "not a certification" in first
    assert "13 May 2027" in visible_text(section_html(doc, 13)) or "2027-05-13" in visible_text(section_html(doc, 13))


def test_legal_framework_uses_the_registry_sources():
    d = build()
    s13 = section_html(render_html(d), 13)
    for src in d.legal.sources:
        assert htmllib.escape(src.title) in s13 and (src.sha256 is None or src.sha256 in s13)
    for gazette in ("CG-DL-E-12082023-248045", "CG-DL-E-14112025-267650", "CG-DL-E-14112025-267647", "CG-DL-E-12122025-268455"):
        assert gazette in s13
    assert "2027-05-13" in s13 and "2026-11-13" in s13 and "draft" in s13.lower()


def test_limitations_name_the_standing_caveats_and_the_scans_own_gaps():
    d = build()
    s20 = visible_text(section_html(render_html(d), 20))
    for phrase in ("draft", "entry page and up to two further pages", "TLS 1.0 and 1.1 were not probed", "cannot certify compliance", "requests and cookies recorded"):
        assert phrase in s20
    assert "Consent test \"Reject carried" not in s20 and "After Reject" in s20          # the Reject test could not run, and the report says so
    assert "URLs failed" in s20 and "URLs excluded" in s20


def test_the_url_listing_is_capped_with_a_stated_total():
    raw = make_raw()
    raw["urls"] = raw["urls"][:2]
    raw["declaration"] = decl()
    doc = render_html(assemble(raw, generated_at=NOW))
    assert "Showing the first" in section_html(doc, 4) and keyed(doc)["urls_total"] == "14" and keyed(doc)["urls_shown"] == "2"


def test_the_penalty_ceiling_is_shown_as_a_ceiling_not_a_prediction():
    s16 = visible_text(section_html(render_html(build()), 16))
    assert "ceiling set by law, not a prediction" in s16 and "Rs 50 crore" in s16 and "Schedule item 7" in s16


def test_human_review_section_lists_states_and_flagged_findings():
    d = build()
    s19 = section_html(render_html(d), 19)
    for pid in ("S1", "T1", "E1"):
        assert f"<b>{pid}</b>" in s19
    assert keyed(render_html(d))["hr_findings"] == "1" and "Finding N2:items" in s19


# ---- (i) a real PDF ---------------------------------------------------------------------------------------------------------------------------------------------------

async def _chromium_works() -> str | None:
    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as pw:
            b = await pw.chromium.launch(headless=True)
            await b.close()
        return None
    except Exception as exc:                                 # not installed, or cannot start here
        return f"{type(exc).__name__}: {str(exc)[:120]}"


async def test_a_real_pdf_is_generated_and_inspected():
    why = await _chromium_works()
    if why:
        pytest.skip(f"Chromium cannot run on this host ({why}); the PDF step needs the container")
    import io

    from pypdf import PdfReader

    from app.enterprise.report.pdf import render_pdf

    raw = make_raw()
    raw["urls"] = [{"original_url": f"https://example.test/page-{i}", "final_url": None, "depth": 1, "status": "done", "analysis_status": "done", "http_status": 200,
                    "status_reason": None, "discovery_sources": ["sitemap"]} for i in range(150)]
    raw["observations"] += [cookie_row(f"https://example.test/p{i}", name=f"cookie_{i}") for i in range(40)]
    d = assemble(raw, generated_at=NOW)
    doc = render_html(d)
    pdf, pages = await render_pdf(doc)
    reader = PdfReader(io.BytesIO(pdf))
    assert pages == len(reader.pages) > 3 and pdf.startswith(b"%PDF")
    texts = [p.extract_text() for p in reader.pages]
    full = "\n".join(texts)
    assert str(d.score.score) in full and "readiness score" in full.lower()
    # section order: the last occurrence of each heading is its place in the body (the contents list repeats them earlier)
    pos, end = [], len(full)
    for i in range(20, 0, -1):
        at = full.rfind(f"{i}. {SECTIONS[i - 1]}", 0, end)
        assert at >= 0, f"section {i} heading is missing from the PDF text"
        pos.append(at)
        end = at
    assert pos == sorted(pos, reverse=True)
    # running header and footer with page numbers, on every page
    for n, t in enumerate(texts, 1):
        assert f"Page {n} of {pages}" in t and str(SCAN) in t, f"page {n} lacks its footer or scan id"
    # page 1 is the cover and contents, the executive summary is one page of its own, and section 2 starts the next page
    assert "Contents" in texts[0] and "1. Executive summary" in texts[0] and "Key figures" not in texts[0]
    assert "1. Executive summary" in texts[1] and "Key figures" in texts[1] and "2. Scan scope and methodology" not in texts[1]
    assert "2. Scan scope and methodology" in texts[2]
    print("PDF PAGES:", pages, "bytes:", len(pdf))
    for token in FORBIDDEN:
        assert token not in full, token
    assert "not a certification" in texts[0] or "not a certification" in full


def test_identical_forms_are_one_row_that_counts_every_page_and_a_different_form_is_its_own_row():
    from types import SimpleNamespace

    from app.enterprise.report import html as rh
    from app.enterprise.report.data import FormRow

    def form(page, extra=()):
        fields = [{"name": "email", "type": "email", "required": True}, *extra]
        return FormRow(page_url=page, action="https://x.test/subscribe", action_host="x.test", method="post", fields=fields, collects=["email"], consent_controls=[], privacy_link=True, hidden_field_count=0)

    forms = [form(f"https://x.test/p{i}") for i in range(25)] + [form("https://x.test/contact", extra=[{"name": "phone", "type": "tel", "required": False}])]
    out = rh._s9(SimpleNamespace(forms=forms, known=lambda *a, **k: None))
    assert "26 form(s) found, in 2 distinct kind(s)" in out or "26" in out and "2 distinct kind(s)" in out
    assert "25 form(s) on 25 page(s)" in out
    assert out.count("<tr>") <= 3                       # a header row and two data rows, not twenty-seven
