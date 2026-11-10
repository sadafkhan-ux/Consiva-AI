"""
Quality checks on a finished report, run before a PDF is kept or handed to a customer.

Two kinds, both read-only (they never change a score or a word):

* Calculation QC, on the scorecard result: the marks awarded and assessed add up from the rows, the category figures add up to the same totals, and the score is
  the awarded share of the assessed marks, rounded half up (or is held back for a reason the result itself states).
* Content QC, on the printed PDF: the page count, no internal code (P6, "depends on P11") in text a customer reads, and every "N/100" on a page agrees.

A failure is a `ReportQcError` listing every problem; the report is then neither stored nor served, and the reason is logged for the person who owns report quality.
"""
from __future__ import annotations

import io
import re
from typing import Any

from pypdf import PdfReader

# An internal check code as it would read in customer text: P1 to P12 on their own. "Page 2" / "P.S." / "PDF" never match (a digit must follow the P directly).
_INTERNAL_CODE = re.compile(r"(?<![A-Za-z0-9])P(?:1[0-2]|[1-9])(?![A-Za-z0-9])")
_SCORE = re.compile(r"(?<![\d.])(\d{1,3})\s*/\s*100(?!\d)")
EXPECTED_PAGES = {"summary": 2, "free": 2}


class ReportQcError(Exception):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


def check_scorecard(r: dict[str, Any]) -> list[str]:
    """Calculation QC: every figure of the scorecard result reconciles with the rows it comes from."""
    problems: list[str] = []
    rows = r.get("rows") or []
    in_play_ids = {"pass", "partial", "fail"}
    in_play = sum(x["marks"] for x in rows if x["outcome"] in in_play_ids)
    awarded = sum(x["awarded"] or 0 for x in rows if x["outcome"] in in_play_ids)
    pts = r.get("points") or {}
    if pts.get("in_play") != in_play:
        problems.append(f"marks assessed: the rows give {in_play}, the result says {pts.get('in_play')}")
    if pts.get("awarded") != awarded:
        problems.append(f"marks awarded: the rows give {awarded}, the result says {pts.get('awarded')}")
    cats = r.get("categories") or []
    if sum(c["assessable"] for c in cats) != in_play:
        problems.append(f"the categories assess {sum(c['assessable'] for c in cats)} marks, the rows {in_play}")
    if sum(c["awarded"] for c in cats) != awarded:
        problems.append(f"the categories award {sum(c['awarded'] for c in cats)} marks, the rows {awarded}")
    if sum(x["marks"] for x in rows) != 100:
        problems.append("the weights do not add up to 100")
    problems.extend(check_rows(r))
    score = r.get("score")
    if score is not None and in_play:
        expected = int(100 * awarded / in_play + 0.5)
        held = r.get("band_notes") or []
        if score != expected and not (score == 99 and expected >= 100 and held):
            problems.append(f"score {score}: {awarded} of {in_play} marks gives {expected}")
    return problems


MAX_SOFT_MASKS = 20                              # per two-page report; the current design prints 5, the blurred-shadow design printed 70
ALLOWED_LINK_HOSTS = ("consiva.ai",)            # Consiva's own address (the sign-up and Full Scan links); the scanned site's own pages are allowed as well
_REPORT_ID = re.compile(r"(?<![A-Za-z0-9])[A-Z]{2}-\d{8}-[0-9A-F]{4}(?![A-Za-z0-9])")
_A4 = (595.28, 841.89)
FIRST_PAGE_VARIANTS = ("summary", "free")        # these open on the page that names the site, the scan date and the report ID


def _count_soft_masks(reader: PdfReader) -> int:
    """Distinct soft masks in the document. Chromium prints a blurred shadow or a translucent gradient as a soft mask (a raster picture used as a stencil), and a page that fills with them is the
    source of halos and rectangular patches in some viewers. A report printed with flat edges carries a handful; one printed with blurred shadows on every tile carries dozens."""
    ids: set = set()
    for page in reader.pages:
        res = page.get("/Resources")
        eg = res.get_object().get("/ExtGState") if res is not None else None
        if eg is None:
            continue
        for ref in eg.get_object().values():
            sm = ref.get_object().get("/SMask")
            sm = sm.get_object() if hasattr(sm, "get_object") else sm
            if not hasattr(sm, "get"):
                continue                                  # /None: no soft mask
            g = sm.get("/G")
            ids.add(getattr(g, "idnum", id(g)))
    return len(ids)


def check_rows(r: dict) -> list[str]:
    """Content QC on the scorecard result: every row's outcome, marks, evidence and dependency agree with the rules, and the band agrees with the score."""
    problems: list[str] = []
    for x in r.get("rows") or []:
        o, m, a = x["outcome"], x["marks"], x["awarded"]
        want = {"pass": m, "partial": m // 2, "fail": 0}.get(o)
        if o in ("pass", "partial", "fail") and a != want:
            problems.append(f"{x['id']}: {o} should carry {want} of {m} marks, it carries {a}")
        if o not in ("pass", "partial", "fail") and a is not None:
            problems.append(f"{x['id']}: {o} must carry no marks, it carries {a}")
        if o in ("pass", "partial", "fail") and not x.get("evidence"):
            problems.append(f"{x['id']}: {o} without any evidence")
        if x.get("blocked_by") and (o != "fail" or a != 0):
            problems.append(f"{x['id']}: blocked by {x['blocked_by']} but not scored 0")
        if o in ("partial", "fail") and not (x.get("found") or "").strip():
            problems.append(f"{x['id']}: {o} with no finding stated")
    score = r.get("score")
    if score is not None and r.get("band"):
        from app.scoring import scorecard

        names = [b[0] for b in scorecard.BANDS]
        expected = next(n for n, floor in scorecard.BANDS if score >= floor)
        lowered = names[min(len(names) - 1, names.index(expected) + 1)]
        if r["band"] not in (expected, lowered):
            problems.append(f"band '{r['band']}' does not match the score {score}")
    return problems


def _host_ok(host: str, domain: str | None) -> bool:
    host = host.lower().rstrip(".")
    if any(host == h or host.endswith("." + h) for h in ALLOWED_LINK_HOSTS):
        return True
    if domain:
        d = domain.lower().removeprefix("www.")
        return host == d or host.endswith("." + d)
    return False


def check_pdf(pdf: bytes, variant: str, domain: str | None = None) -> list[str]:
    """Content, visual-basics and functional QC on the printed report. `domain` is the scanned site, when known: it must be named on the page and the only outside host a link may reach."""
    problems: list[str] = []
    reader = PdfReader(io.BytesIO(pdf))
    pages = len(reader.pages)
    want = EXPECTED_PAGES.get(variant)
    if want is not None and pages != want:
        problems.append(f"the report has {pages} pages, it must have {want}")
    text = "\n".join((p.extract_text() or "") for p in reader.pages)
    found = sorted(set(_INTERNAL_CODE.findall(text)))
    if found:
        problems.append("internal check code in customer text: " + ", ".join(found))
    # visual basics: A4 pages, each carrying real text (a page that printed blank or as a picture is not a report)
    for n, page in enumerate(reader.pages, start=1):
        w, h = float(page.mediabox.width), float(page.mediabox.height)
        if abs(w - _A4[0]) > 3 or abs(h - _A4[1]) > 3:
            problems.append(f"page {n} is {w:.0f} x {h:.0f} pt, not A4")
        if variant in EXPECTED_PAGES and len((page.extract_text() or "").strip()) < 150:
            problems.append(f"page {n} carries almost no text")
    if variant in EXPECTED_PAGES and (masks := _count_soft_masks(reader)) > MAX_SOFT_MASKS:
        problems.append(f"the report holds {masks} soft masks (limit {MAX_SOFT_MASKS}): shadows or gradients are being printed as pictures, which shows as patches in some PDF viewers")
    # functional: every link goes to Consiva or to the scanned site, never to a script or an unknown host
    from urllib.parse import urlparse
    seen: set[str] = set()
    for page in reader.pages:
        for annot in page.get("/Annots") or []:
            a = annot.get_object()
            uri = (a.get("/A") or {}).get("/URI") if a.get("/A") else None
            if not uri or uri in seen:
                continue
            seen.add(uri)
            u = urlparse(str(uri))
            if u.scheme not in ("http", "https") or not u.hostname or not _host_ok(u.hostname, domain):
                problems.append(f"link to an unexpected address: {uri}")
    # metadata: the first page names the site, carries the scan date in IST and a report ID
    if variant in FIRST_PAGE_VARIANTS:
        first = reader.pages[0].extract_text() or ""
        flat = re.sub(r"\s+", "", first).lower()
        if domain and domain.lower().removeprefix("www.") not in flat:
            problems.append(f"the first page does not name the site {domain}")
        if not re.search(r"\d{1,2} [A-Z][a-z]{2} \d{4}, \d{2}:\d{2} IST", first):
            problems.append("the first page has no scan date and time in IST")
        if not _REPORT_ID.search(first):
            problems.append("the first page has no report ID")
    scores = set(_SCORE.findall(text))
    if len(scores) > 1:
        problems.append("more than one overall score is printed: " + ", ".join(sorted(f"{s}/100" for s in scores)))
    return problems


def enforce(problems: list[str]) -> None:
    if problems:
        raise ReportQcError(problems)
