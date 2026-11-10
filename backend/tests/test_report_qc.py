"""Quality control of a finished report (services/report_qc.py): calculation QC on the scorecard result and content QC on the printed PDF."""
from __future__ import annotations

import io

import pytest
from pypdf import PdfWriter

from app.services import report_qc as qc


def _result(**over):
    rows = [{"id": f"P{i}", "marks": m, "outcome": o, "awarded": a, "evidence": [{"observed": "e"}], "found": "x", "blocked_by": None} for i, (m, o, a) in enumerate(
        [(8, "pass", 8), (8, "not_assessed", None), (12, "pass", 12), (12, "not_assessed", None), (8, "not_assessed", None), (6, "pass", 6), (10, "partial", 5), (10, "fail", 0), (10, "partial", 5), (8, "fail", 0), (4, "pass", 4), (4, "pass", 4)], start=1)]
    r = {"rows": rows, "points": {"awarded": 44, "in_play": 72}, "categories": [{"assessable": 20, "awarded": 20}, {"assessable": 36, "awarded": 16}, {"assessable": 16, "awarded": 8}], "score": 61, "band_notes": []}
    r.update(over)
    return r


def test_a_result_whose_figures_reconcile_passes():
    assert qc.check_scorecard(_result()) == []


def test_each_way_the_figures_can_disagree_is_named():
    assert any("score" in p for p in qc.check_scorecard(_result(score=70)))
    assert any("marks awarded" in p for p in qc.check_scorecard(_result(points={"awarded": 40, "in_play": 72})))
    assert any("categories" in p for p in qc.check_scorecard(_result(categories=[{"assessable": 20, "awarded": 20}])))


def test_a_score_held_at_99_is_not_a_mismatch():
    r = _result(score=99, band_notes=["held at 99"])
    r["points"] = {"awarded": 72, "in_play": 72}
    for x in r["rows"]:
        if x["outcome"] in ("partial", "fail"):
            x["outcome"], x["awarded"] = "pass", x["marks"]
    r["categories"] = [{"assessable": 20, "awarded": 20}, {"assessable": 36, "awarded": 36}, {"assessable": 16, "awarded": 16}]
    assert qc.check_scorecard(r) == []


def _pdf(pages, text="", links=(), size=(612, 792)):
    """A minimal real PDF: one line of text on every page (links annotated on the first)."""
    nl = chr(10)
    first_annot = 4 + 2 * pages
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", "<< /Type /Pages /Kids [" + " ".join(f"{4 + 2 * i} 0 R" for i in range(pages)) + f"] /Count {pages} >>", "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    for i in range(pages):
        body = f"BT /F1 12 Tf 72 720 Td ({text.replace('(', '[').replace(')', ']')}) Tj ET"
        annots = " /Annots [" + " ".join(f"{first_annot + k} 0 R" for k in range(len(links))) + "]" if i == 0 and links else ""
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {size[0]} {size[1]}] /Resources << /Font << /F1 3 0 R >> >> /Contents {5 + 2 * i} 0 R{annots} >>")
        objs.append(f"<< /Length {len(body)} >>" + nl + "stream" + nl + body + nl + "endstream")
    for url in links:
        objs.append(f"<< /Type /Annot /Subtype /Link /Rect [72 700 200 730] /Border [0 0 0] /A << /S /URI /URI ({url}) >> >>")
    out, offsets = b"%PDF-1.4" + nl.encode(), []
    for n, o in enumerate(objs, start=1):
        offsets.append(len(out))
        out += (f"{n} 0 obj" + nl + o + nl + "endobj" + nl).encode()
    xref = len(out)
    out += (f"xref{nl}0 {len(objs) + 1}{nl}0000000000 65535 f {nl}").encode() + b"".join(f"{o:010d} 00000 n {nl}".encode() for o in offsets)
    return out + (f"trailer{nl}<< /Size {len(objs) + 1} /Root 1 0 R >>{nl}startxref{nl}{xref}{nl}%%EOF").encode()


A4 = (595, 842)
GOOD = "poolscapes.in 10 Nov 2026, 11:30 IST FR-20261110-B841 Privacy Readiness Score 64/100 " + "padding text " * 20


def test_a_summary_must_have_two_pages():
    assert qc.check_pdf(_pdf(2, GOOD, size=A4), "summary") == []
    assert any("pages" in p for p in qc.check_pdf(_pdf(3, "53/100", size=A4), "summary"))


def test_internal_codes_never_reach_a_customer():
    bad = qc.check_pdf(_pdf(2, "The privacy notice this depends on was not found. (depends on P6)"), "summary")
    assert any("P6" in p for p in bad)
    assert qc.check_pdf(_pdf(2, GOOD + " Page 2 of 2, PDF, Check 6, P.S.", size=A4), "summary") == []


def test_two_different_overall_scores_are_refused():
    assert any("more than one" in p for p in qc.check_pdf(_pdf(2, "53/100 and 54/100"), "summary"))


def test_enforce_raises_with_every_problem():
    with pytest.raises(qc.ReportQcError) as e:
        qc.enforce(["a", "b"])
    assert e.value.problems == ["a", "b"]


def test_a_blocking_check_is_named_for_the_reader_never_by_its_internal_code():
    from app.services.assessment_service import _check_name
    assert _check_name("P6") == "Check 6" and _check_name("P11") == "Check 11"
    assert _check_name("A1") == "an earlier check"


async def test_a_printed_report_that_fails_the_gate_is_neither_stored_nor_served(monkeypatch):
    import uuid
    from types import SimpleNamespace

    from app.core.exceptions import ReportGenerationError
    from app.services import report_cache_service as rc
    from app.services.report_pdf_service import RenderedReport

    kept = []

    async def fp(db, sc, settings=None):
        return "A"

    async def stored(db, sid, variant, current):
        return None

    async def render(*a, **k):
        return RenderedReport(pdf=_pdf(2, "The notice this depends on was not found. (depends on P6) 53/100"), filename="r.pdf")

    async def store(*a, **k):
        kept.append(1)

    class NoDb:
        async def rollback(self):
            return None

    rc._inflight.clear()
    monkeypatch.setattr(rc, "fingerprint", fp)
    monkeypatch.setattr(rc, "get_stored", stored)
    monkeypatch.setattr(rc, "_store", store)
    monkeypatch.setattr(rc.report_pdf_service, "render_report_pdf", render)
    scan = SimpleNamespace(id=uuid.uuid4(), org_id=uuid.uuid4(), status="completed", completed_at=None, authorized_by_user_id=None)
    with pytest.raises(ReportGenerationError):
        await rc.get_or_render(NoDb(), scan, "summary", token="t", user_id="u", org_id="o", role="member")
    assert kept == []
    rc._inflight.clear()


def test_a_complete_first_page_passes_the_functional_checks():
    assert qc.check_pdf(_pdf(2, GOOD, links=("https://app.consiva.ai/register", "https://poolscapes.in/privacy-policy.php"), size=A4), "summary", domain="poolscapes.in") == []


def test_a_link_to_an_unexpected_address_is_named():
    bad = qc.check_pdf(_pdf(2, GOOD, links=("https://evil.example/x", "javascript:alert(1)"), size=A4), "summary", domain="poolscapes.in")
    assert any("evil.example" in p for p in bad) and any("javascript" in p for p in bad)


def test_the_first_page_must_name_the_site_the_scan_date_in_ist_and_a_report_id():
    wrong_site = qc.check_pdf(_pdf(2, GOOD, size=A4), "summary", domain="groupsnp.com")
    assert any("does not name the site" in p for p in wrong_site)
    no_date = qc.check_pdf(_pdf(2, GOOD.replace("10 Nov 2026, 11:30 IST", "10 Nov 2026"), size=A4), "summary", domain="poolscapes.in")
    assert any("IST" in p for p in no_date)
    no_id = qc.check_pdf(_pdf(2, GOOD.replace("FR-20261110-B841", ""), size=A4), "summary", domain="poolscapes.in")
    assert any("report ID" in p for p in no_id)


def test_a_page_that_is_not_a_4_and_a_blank_page_are_refused():
    assert any("not A4" in p for p in qc.check_pdf(_pdf(2, GOOD, size=(612, 792)), "summary", domain="poolscapes.in"))
    assert any("almost no text" in p for p in qc.check_pdf(_pdf(2, "x", size=A4), "summary"))


def _row(id, outcome, marks, awarded, evidence=True, found="x", blocked_by=None):
    return {"id": id, "outcome": outcome, "marks": marks, "awarded": awarded, "evidence": [{"observed": "e"}] if evidence else [], "found": found, "blocked_by": blocked_by}


def test_every_row_must_agree_with_the_marking_rules():
    ok = {"rows": [_row("P1", "pass", 8, 8), _row("P2", "partial", 8, 4), _row("P3", "fail", 12, 0), _row("P4", "not_assessed", 12, None, evidence=False, found="")], "score": 60, "band": "Partially addressed"}
    assert qc.check_rows(ok) == []
    bad = {"rows": [_row("P1", "pass", 8, 7), _row("P2", "partial", 8, 8), _row("P3", "fail", 12, 0, evidence=False), _row("P4", "not_assessed", 12, 3), _row("P5", "pass", 8, 8, blocked_by="P4"), _row("P6", "partial", 6, 3, found="")]}
    text = " | ".join(qc.check_rows(bad))
    for needle in ("P1: pass should carry 8", "P2: partial should carry 4", "P3: fail without any evidence", "P4: not_assessed must carry no marks", "P5: blocked by P4", "P6: partial with no finding"):
        assert needle in text, needle


def test_the_band_must_match_the_score_or_be_held_one_level_lower():
    rows = [_row("P1", "pass", 8, 8)]
    assert qc.check_rows({"rows": rows, "score": 64, "band": "Partially addressed"}) == []
    assert qc.check_rows({"rows": rows, "score": 64, "band": "Needs attention"}) == []          # held one level lower for low confidence
    assert any("band" in p for p in qc.check_rows({"rows": rows, "score": 64, "band": "Substantially addressed"}))


class _Obj(dict):
    def get_object(self):
        return self


def test_soft_masks_are_counted_once_each_and_none_is_ignored():
    def gs(i):
        return _Obj({"/SMask": _Obj({"/G": type("R", (), {"idnum": i})()})})

    page = {"/Resources": _Obj({"/ExtGState": _Obj({"/a": gs(1), "/b": gs(2), "/c": gs(1), "/d": _Obj({"/SMask": "/None"})})})}
    reader = type("Reader", (), {"pages": [page, page]})()
    assert qc._count_soft_masks(reader) == 2
