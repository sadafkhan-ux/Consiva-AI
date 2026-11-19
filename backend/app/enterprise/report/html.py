"""Renders the Enterprise report as one self-contained, print-ready HTML document: inline CSS, A4, no script, no external request.

Layout (owner's decision, 19 Nov 2026):
  * Pages 1 and 2 are the two-page summary, laid out as the 5-page agent's two-page "Privacy Readiness Report" (header and title bar, score with its group totals, website privacy
    elements, consent banner, contacts, scope, key figures, consent states compared, pages scanned; then cookie consent analysis, forms and consent, key compliance checks with their
    marks, and the disclaimer). Nothing of the 5-page agent is imported or changed: the layout is rebuilt here from the Enterprise data.
  * From page 3, the detailed assessment: every parameter with its status (Pass, Partially Pass or Fail), marks, legal basis, what was found, the evidence with its page URL and document
    page number, and what to do; then the findings, the scope and coverage, consent behaviour, cookies and third parties, forms, scripts, security, documents, URLs, the legal framework,
    the evidence index and the limitations.
  * At most 40 pages. Long lists are capped by `level` (report/service.py raises it until the PDF fits); a capped list always says how many rows exist and which API returns all of them,
    so nothing is silently lost.

Every value comes from the `ReportData` object and is HTML-escaped (it originates in scanned websites and is untrusted). A value that was not recorded is printed as "not recorded", never as an
empty cell, and nothing is computed here that the data object does not already hold, except sums and counts of its own rows.
"""

from __future__ import annotations

from datetime import datetime
from html import escape
from urllib.parse import urlparse

from app.enterprise.findings import RECOMMENDATIONS
from app.enterprise.report.data import EvidenceRef, ParameterRow, ReportData

NOT_RECORDED = "not recorded"
MAX_PAGES = 40
SECTIONS = (
    "Parameter-by-parameter assessment", "Findings and priorities", "Scan scope, method and coverage", "Consent behaviour", "Cookies and third parties", "Forms and data collection",
    "Scripts and embedded frames", "Technical security", "Policies and documents read", "URL inventory", "Applicable DPDP legal framework", "Evidence index", "Limitations",
)
STATE_LABEL = {"met": "Pass", "partly_met": "Partially Pass", "not_met": "Fail"}
SEV_LABEL = {"high": "High", "medium": "Medium", "low": "Low", "info": "Information"}
CONSENT_LABEL = {
    "accept": "After Accept", "reject": "After Reject", "reject_persistence": "Reject carried to further pages", "withdrawal": "Withdrawal of consent",
    "purposes": "Purpose-by-purpose choices", "button_comparison": "Accept and Reject controls compared",
}
REASON_LABEL = {
    "homepage_not_loaded": "the entry page did not load", "page_not_loaded": "the entry page did not load", "accept_not_clicked": "Accept could not be given",
    "reject_not_clicked": "Reject could not be given", "control_not_found": "no banner control was found", "click_failed": "the control was found but could not be clicked",
    "no_reopen_control": "no control to reopen the choice was found", "no_withdraw_control": "the reopened dialog offered no control that withdraws consent",
    "banner_or_controls_not_found": "no banner, or one of its controls, was found", "error": "the test could not be completed", "no_preferences_control": "no preferences control was found",
    "no_purpose_choices": "no purpose-by-purpose choices were found", "not_recorded": "no result was recorded for this test",
}
#: Parts of the Act and Rules that are not duties a website owner can show; listed in the report so nobody assumes they were overlooked.
OUTSIDE_WEBSITE = (
    ("ss.3 and 17(2), (4), (5)", "Scope of the Act and exemptions for the State and notified bodies"),
    ("s.7(b) to (i); Rule 5; Second Schedule", "Legitimate uses by the State and its instrumentalities, and the standards for them"),
    ("s.15", "Duties of the Data Principal"),
    ("ss.18 to 37; Rules 17 to 23; Seventh Schedule", "The Data Protection Board, appeals, penalties procedure and calls for information by the Central Government"),
)
#: Rows shown per list at each level; the service raises the level until the PDF fits MAX_PAGES.
CAPS = (
    {"urls": 120, "cookies": 80, "hosts": 60, "forms": 40, "evidence": 150, "security": 30, "iframes": 30, "documents": 60, "consent_facts": 12, "param_refs": 4},
    {"urls": 60, "cookies": 50, "hosts": 40, "forms": 25, "evidence": 80, "security": 20, "iframes": 20, "documents": 40, "consent_facts": 8, "param_refs": 3},
    {"urls": 30, "cookies": 30, "hosts": 25, "forms": 15, "evidence": 40, "security": 12, "iframes": 12, "documents": 25, "consent_facts": 6, "param_refs": 3},
    {"urls": 15, "cookies": 15, "hosts": 15, "forms": 10, "evidence": 20, "security": 8, "iframes": 8, "documents": 15, "consent_facts": 4, "param_refs": 2},
    {"urls": 8, "cookies": 8, "hosts": 8, "forms": 6, "evidence": 10, "security": 5, "iframes": 5, "documents": 8, "consent_facts": 3, "param_refs": 2},
)
MAX_LEVEL = len(CAPS) - 1

CSS = """
:root { --ink:#1b2333; --muted:#566074; --rule:#d3d9e3; --soft:#f3f5f9; --accent:#1f4e79; --accent-soft:#e6eef6; --pass:#2b6a4d; --part:#a35f00; --fail:#9b2226; --info:#4a5568;
        --high:#9b2226; --medium:#a35f00; --low:#2b6a4d; }
* { box-sizing: border-box; }
@page { size: A4; margin: 22mm 16mm 20mm 16mm; }
html { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
body { margin: 0; background: #fff; color: var(--ink); font-family: "Segoe UI", "Helvetica Neue", Helvetica, Arial, sans-serif; font-size: 9.5pt; line-height: 1.45; }
h1 { font-size: 20pt; line-height: 1.15; margin: 0; color: var(--accent); letter-spacing: -0.2pt; }
h2 { font-size: 13.5pt; color: var(--accent); margin: 18pt 0 6pt; padding-bottom: 3pt; border-bottom: 1.5pt solid var(--accent); break-after: avoid; }
h3 { font-size: 10pt; margin: 10pt 0 4pt; color: var(--ink); break-after: avoid; }
h4 { font-size: 9pt; margin: 8pt 0 3pt; color: var(--muted); text-transform: uppercase; letter-spacing: 0.4pt; break-after: avoid; }
p { margin: 0 0 5pt; orphans: 3; widows: 3; }
.notice { border: 1pt solid var(--accent); background: var(--accent-soft); padding: 6pt 9pt; margin: 6pt 0 9pt; break-inside: avoid; }
.notice.warn { border-color: var(--medium); background: #fbf3e4; }
table { width: 100%; border-collapse: collapse; table-layout: fixed; margin: 5pt 0 11pt; font-size: 8pt; line-height: 1.35; }
th, td { border: 0.5pt solid var(--rule); padding: 3pt 4.5pt; text-align: left; vertical-align: top; overflow-wrap: anywhere; word-break: break-word; }
thead { display: table-header-group; }
tr { break-inside: avoid; page-break-inside: avoid; }
th { background: var(--accent); color: #fff; font-weight: 600; font-size: 7.2pt; text-transform: uppercase; letter-spacing: 0.3pt; border-color: var(--accent); }
tbody tr:nth-child(even) td { background: #f8f9fc; }
table.compact th, table.compact td { padding: 1.5pt 4pt; font-size: 7pt; line-height: 1.25; }
td.num, th.num { text-align: right; }
table.kv th { background: var(--soft); color: var(--ink); text-transform: none; letter-spacing: 0; font-size: 7.8pt; width: 34%; border-color: var(--rule); }
.mono { font-family: Consolas, "Courier New", monospace; font-size: 7pt; }
.muted { color: var(--muted); } .small { font-size: 7.8pt; }
.chip { display: inline-block; padding: 0 4pt; border-radius: 2pt; font-size: 7.3pt; font-weight: 700; border: 0.6pt solid currentColor; white-space: nowrap; }
.s-met { color: var(--pass); } .s-partly_met { color: var(--part); } .s-not_met { color: var(--fail); }
.chip.sev-high { color: var(--high); } .chip.sev-medium { color: var(--medium); } .chip.sev-low { color: var(--low); } .chip.sev-info { color: var(--info); }
.cap { color: var(--muted); font-size: 7.6pt; margin: -6pt 0 8pt; }
ul { margin: 2pt 0 5pt; padding-left: 13pt; } li { margin-bottom: 1.5pt; }
.detail { break-before: page; }
/* parameter cards */
.pcard { border: 0.5pt solid var(--rule); border-left: 3pt solid var(--info); padding: 4pt 7pt 3pt; margin: 0 0 5pt; break-inside: avoid; font-size: 8pt; }
.pcard.st-met { border-left-color: var(--pass); } .pcard.st-partly_met { border-left-color: var(--part); } .pcard.st-not_met { border-left-color: var(--fail); }
.pcard .ph { display: flex; justify-content: space-between; gap: 8pt; align-items: baseline; }
.pcard .ph b { font-size: 8.6pt; } .pcard .marks { white-space: nowrap; font-weight: 700; }
.pcard p { margin: 1.5pt 0; } .pcard .lab { color: var(--muted); font-weight: 600; }
.grp { margin: 8pt 0 4pt; font-size: 9.5pt; font-weight: 700; color: var(--accent); break-after: avoid; }
/* the two summary sheets */
.sheet { height: 252mm; break-after: page; display: flex; flex-direction: column; gap: 4.2pt; font-size: 7.8pt; line-height: 1.3; }
.sheet table { margin: 0; font-size: 7.2pt; line-height: 1.25; }
.sheet th { font-size: 6.6pt; padding: 2pt 4pt; } .sheet td { padding: 1.8pt 4pt; }
.sn-head { display: flex; justify-content: space-between; align-items: center; border-bottom: 1.5pt solid var(--accent); padding-bottom: 4pt; }
.sn-logo { font-size: 15pt; font-weight: 800; color: var(--accent); letter-spacing: -0.4pt; } .sn-logo span { color: #2f8f6b; }
.sn-head-r { text-align: right; } .sn-pill { display: inline-block; background: var(--accent); color: #fff; border-radius: 9pt; padding: 1pt 8pt; font-weight: 700; font-size: 7.6pt; }
.sn-head-r div { color: var(--muted); font-size: 7pt; margin-top: 1pt; }
.sn-titlebar { display: flex; justify-content: space-between; align-items: flex-end; gap: 10pt; }
.sn-domain { font-size: 10pt; font-weight: 600; color: var(--muted); }
.sn-meta { display: flex; gap: 10pt; } .sn-meta span { display: flex; flex-direction: column; } .sn-meta small { color: var(--muted); font-size: 6.6pt; text-transform: uppercase; } .sn-meta b { font-size: 8pt; }
.sn-hero { display: flex; gap: 6pt; }
.sn-card { border: 0.6pt solid var(--rule); border-radius: 4pt; padding: 5pt 7pt; background: #fff; }
.sn-score { flex: 1.15; display: flex; gap: 8pt; }
.sn-ring { width: 27mm; height: 27mm; border-radius: 50%; flex: none; display: flex; align-items: center; justify-content: center; }
.sn-ring div { width: 20mm; height: 20mm; border-radius: 50%; background: #fff; display: flex; flex-direction: column; align-items: center; justify-content: center; }
.sn-ring b { font-size: 17pt; line-height: 1; } .sn-ring small { color: var(--muted); font-size: 6.5pt; }
.sn-cats { flex: 1; display: flex; flex-direction: column; gap: 2.2pt; }
.sn-cat-top { display: flex; justify-content: space-between; font-size: 6.9pt; } .sn-cat-top b { font-weight: 600; }
.sn-meter { height: 3pt; background: var(--soft); border-radius: 2pt; overflow: hidden; } .sn-meter i { display: block; height: 100%; background: var(--accent); }
.sn-side { flex: 1; display: flex; flex-direction: column; gap: 5pt; }
.sn-card-head { display: flex; justify-content: space-between; margin-bottom: 3pt; } .sn-card-head b { color: var(--accent); }
.sn-els { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 3pt; }
.sn-el { border: 0.5pt solid var(--rule); border-radius: 3pt; padding: 2.5pt 4pt; font-size: 7pt; } .sn-el b { display: block; } .sn-el.found span { color: var(--pass); font-weight: 700; } .sn-el.missing span { color: var(--fail); font-weight: 700; }
.sn-el.notneeded span { color: var(--info); font-weight: 700; }
.sn-opts { display: flex; gap: 8pt; flex-wrap: wrap; font-size: 7pt; }
.sn-row { display: flex; gap: 6pt; } .sn-row > * { flex: 1; min-width: 0; }
.sn-contact b { display: block; color: var(--accent); } .sn-none { color: var(--fail); }
.sn-chips { display: flex; flex-wrap: wrap; gap: 4pt; align-items: center; } .sn-chips b { color: var(--accent); margin-right: 3pt; }
.sn-chip { border: 0.5pt solid var(--rule); background: var(--soft); border-radius: 8pt; padding: 0.5pt 6pt; font-size: 6.9pt; }
.sn-tiles { display: flex; gap: 5pt; } .sn-tile { flex: 1; border: 0.6pt solid var(--rule); border-radius: 4pt; padding: 3pt 6pt; background: var(--soft); }
.sn-tile b { display: block; font-size: 13pt; color: var(--accent); line-height: 1.1; } .sn-tile span { font-size: 6.8pt; color: var(--muted); }
.sn-title { font-weight: 700; color: var(--accent); font-size: 8.6pt; display: flex; justify-content: space-between; } .sn-title em { font-weight: 400; color: var(--muted); font-size: 6.9pt; font-style: normal; }
.sn-grp td { background: var(--accent-soft) !important; font-weight: 700; }
.sn-total td { font-weight: 800; background: var(--soft) !important; }
.sn-next { border: 0.8pt solid var(--accent); background: var(--accent-soft); border-radius: 4pt; padding: 4pt 8pt; }
.sn-disc { border-top: 0.6pt solid var(--rule); padding-top: 3pt; color: var(--muted); font-size: 6.5pt; line-height: 1.3; margin-top: auto; }
"""

# ---- small helpers -----------------------------------------------------------------------------------------------------------------------------------------------------


def esc(v) -> str:
    return escape(str(v), quote=True)


def fmt(v) -> str:
    """A value for display; anything that was not recorded reads "not recorded"."""
    if v is None or (isinstance(v, str) and not v.strip()):
        return NOT_RECORDED
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d %H:%M UTC")
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


def c(v) -> str:
    return esc(fmt(v))


def k(key: str, v) -> str:
    """A figure the reconciliation tests read back: its key and its value."""
    return f'<span data-k="{esc(key)}">{c(v)}</span>'


def chip(state: str) -> str:
    return f'<span class="chip s-{esc(state)}">{esc(STATE_LABEL.get(state, "Fail"))}</span>'


def short_ref(ref: str) -> str:
    return ref[:11] if ref.startswith("ev:") else ref[:40]


def clip(text: str | None, n: int) -> str:
    t = (text or "").strip()
    return t if len(t) <= n else t[: n - 1] + "…"


def table(headers: list[str], rows: list[list[str]], *, widths: list[int] | None = None, num: tuple[int, ...] = (), cls: str = "") -> str:
    """`headers` are plain text, `rows` already-escaped cells. The header row repeats on every page the table spans."""
    if not rows:
        return ""
    cols = "".join(f'<col style="width:{w}%">' for w in widths) if widths else ""
    head = "".join(f'<th class="{"num" if i in num else ""}">{esc(h)}</th>' for i, h in enumerate(headers))
    body = "".join("<tr>" + "".join(f'<td class="{"num" if i in num else ""}">{cell}</td>' for i, cell in enumerate(r)) + "</tr>" for r in rows)
    return f'<table class="{cls}"><colgroup>{cols}</colgroup><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>'


def kv(pairs: list[tuple[str, str]]) -> str:
    return '<table class="kv"><tbody>' + "".join(f"<tr><th>{esc(a)}</th><td>{b}</td></tr>" for a, b in pairs) + "</tbody></table>"


def short(url: str, n: int = 90) -> str:
    return esc(url if len(url) <= n else url[: n - 1] + "…")


def url_cell(url: str | None) -> str:
    return f'<span class="mono">{esc(url)}</span>' if url else esc(NOT_RECORDED)


def pages_cell(pages: list[str], limit: int = 4) -> str:
    if not pages:
        return esc(NOT_RECORDED)
    shown = "<br>".join(f'<span class="mono">{short(p, 70)}</span>' for p in pages[:limit])
    more = f'<br><span class="muted">and {len(pages) - limit} more</span>' if len(pages) > limit else ""
    return f"{len(pages)} page(s):<br>{shown}{more}"


def capped(what: str, shown: int, total: int | None, endpoint: str, scan_id: str) -> str:
    """The line under a capped list: how many rows exist, how many are shown, and where all of them are."""
    if total is None or total <= shown:
        return ""
    return (f'<p class="cap">Showing {shown} of {total} {esc(what)} (most important first). The full list is in the evidence appendix: '
            f'<span class="mono">GET /api/v1/enterprise/scans/{esc(scan_id)}/{esc(endpoint)}</span>.</p>')


def _counts(params: list[ParameterRow]) -> dict[str, int]:
    out = {"met": 0, "partly_met": 0, "not_met": 0}
    for p in params:
        out[p.state if p.state in out else "not_met"] += 1
    return out


def _host(url: str) -> str:
    return urlparse(url).netloc or url


def _evidence_index(d: ReportData) -> dict[str, EvidenceRef]:
    return {e.ref: e for e in d.evidence}


def _where(ref: str, idx: dict[str, EvidenceRef]) -> str:
    """One evidence reference as a reviewer can open it: the page URL, and the document page where there is one."""
    e = idx.get(ref)
    if e is None or not e.source_url:
        return f'<span class="mono">{esc(short_ref(ref))}</span>'
    page = f", page {e.page_number}" if e.page_number else ""
    return f'<span class="mono">{short(e.source_url, 95)}</span><span class="muted">{esc(page)} ({esc(e.kind or "evidence")}, {esc(short_ref(ref))})</span>'


def _section(n: int, body: str, *, first: bool = False) -> str:
    cls = ' class="detail"' if first else ""
    return f'<section id="s{n}"{cls}><h2>{n}. {esc(SECTIONS[n - 1])}</h2>{body}</section>'


def _score_label(d: ReportData) -> str:
    return "Readiness score" if d.score is None or d.score.basis == "readiness" else "Assessment score"


def _param(d: ReportData, pid: str) -> ParameterRow | None:
    return next((p for p in d.parameters if p.id == pid), None)


# ---- the two-page summary ------------------------------------------------------------------------------------------------------------------------------------------------

DISCLAIMER = (
    "Consiva AI's Enterprise DPDP Assessment is an automated, browser-based review of a publicly accessible website and the policy documents it links to, intended for awareness and "
    "readiness benchmarking against the Digital Personal Data Protection Act, 2023 and the DPDP Rules, 2025. The requester represents that they own the submitted domain or have obtained "
    "the necessary authorisation. Duties a website cannot show are judged from the published policy only. Findings and scores are indicative, time-specific and subject to technical "
    "limitations. They do not constitute legal advice, a legal or regulatory determination, compliance certification, comprehensive privacy audit or security assessment."
)

ELEMENTS = (("Privacy Policy", "N1"), ("Terms of Use", None), ("Cookie Policy", None), ("Consent Banner", "C10"), ("Forms", None), ("Per-purpose Choices", "C11"))


def _not_needed(p: ParameterRow | None) -> bool:
    """A Pass that was given because the duty does not apply (for example no banner is needed where nothing needs consent)."""
    return bool(p and p.adjustment and p.adjustment.startswith("does not apply"))


def _elements(d: ReportData) -> list[tuple[str, str]]:
    """(label, "found" | "missing" | "notneeded") for the six website privacy elements, each read from the evidence or from its parameter's result."""
    kinds = {k_ for p in d.policies for k_ in p.kinds}
    out = []
    for label, _pid in ELEMENTS:
        if label == "Privacy Policy":
            p = _param(d, "N1")
            found = "Privacy policy" in kinds or bool(p and p.state == "met")
        elif label == "Terms of Use":
            found = "Terms and conditions" in kinds
        elif label == "Cookie Policy":
            found = "Cookie policy" in kinds
        elif label == "Consent Banner":
            found = bool(d.choice_states.get("accept", {}).get("tested") or d.choice_states.get("reject", {}).get("tested"))
            if not found and _not_needed(_param(d, "C10")):
                out.append((label, "notneeded"))
                continue
        elif label == "Forms":
            found = bool(d.forms)
        else:
            p = _param(d, "C11")
            if _not_needed(p):
                out.append((label, "notneeded"))
                continue
            found = bool(p and p.state == "met")
        out.append((label, "found" if found else "missing"))
    return out


def _ring(score: int | None) -> str:
    pct = max(0, min(100, score or 0))
    colour = "#2b6a4d" if pct >= 75 else "#a35f00" if pct >= 50 else "#9b2226"
    num = "–" if score is None else str(score)
    return (f'<div class="sn-ring" style="background: conic-gradient({colour} 0 {pct}%, #e3e8ef {pct}% 100%)"><div><b data-k="summary_score">{esc(num)}</b><small>of 100</small></div></div>')


def _page1(d: ReportData) -> str:
    s, decl = d.scan, d.declaration
    u = decl.get("urls") or {}
    host = _host(s.website_url) or s.domain
    sc = d.score
    counts = _counts(d.parameters)
    cats = []
    for g in (sc.by_group.values() if sc else []):
        avail = float(g.get("points_available") or 0)
        earned = float(g.get("points_earned") or 0)
        if avail <= 0:
            continue
        pct = round(100 * earned / avail)
        cats.append(f'<div><div class="sn-cat-top"><b>{esc(g.get("label"))}</b><span><strong>{earned:g}</strong> of {avail:g} marks</span></div>'
                    f'<div class="sn-meter"><i style="width:{pct}%"></i></div></div>')
    score_card = (f'<div class="sn-card sn-score">{_ring(sc.score if sc else None)}<div class="sn-cats"><b style="color:var(--accent)">{esc(_score_label(d))}</b>'
                  f'<div class="small"><span class="s-met"><b>{k("sum.pass", counts["met"])}</b> Pass</span> · <span class="s-partly_met"><b>{k("sum.partial", counts["partly_met"])}</b> Partially Pass</span> · '
                  f'<span class="s-not_met"><b>{k("sum.fail", counts["not_met"])}</b> Fail</span></div>{"".join(cats)}</div></div>')
    els = _elements(d)
    words = {"found": "Found", "missing": "Not found", "notneeded": "Not needed"}
    el_html = "".join(f'<div class="sn-el {st}"><b>{esc(lab)}</b><span>{words[st]}</span></div>' for lab, st in els)
    acc, rej = d.choice_states.get("accept", {}), d.choice_states.get("reject", {})
    banner = bool(acc.get("tested") or rej.get("tested"))
    c11 = _param(d, "C11")
    banner_card = (f'<div class="sn-card"><div class="sn-card-head"><b>Consent Banner</b><span class="chip {"s-met" if banner else "s-not_met"}">{"Detected" if banner else "Not detected"}</span></div>'
                   f'<div class="sn-opts"><span><b>Accept option</b> {"Found" if acc.get("tested") else "Not found"}</span><span><b>Reject option</b> {"Found" if rej.get("tested") else "Not found"}</span>'
                   f'<span><b>Per-purpose choice</b> {esc(STATE_LABEL.get(c11.state, "Fail")) if c11 else "not recorded"}</span></div></div>')
    elements_card = (f'<div class="sn-card"><div class="sn-card-head"><b>Website Privacy Elements</b><em class="small muted">{sum(1 for _, st in els if st == "found")} of {len(els)} found</em></div>'
                     f'<div class="sn-els">{el_html}</div></div>')

    def contact(title: str, pid: str) -> str:
        p = _param(d, pid)
        if p and p.state in ("met", "partly_met") and p.note:
            return f'<div class="sn-card sn-contact"><b>{esc(title)}</b>{esc(clip(p.note, 150))}</div>'
        return f'<div class="sn-card sn-contact"><b>{esc(title)}</b><span class="sn-none">Not found in the website\'s public text</span></div>'

    contacts = f'<div class="sn-row">{contact("Data Protection Officer / contact person", "G1")}{contact("Grievance redressal", "G2")}{contact("Complaint to the Data Protection Board", "N6")}</div>'
    on_ev = sum(1 for p in d.parameters if p.adjustment is None or not p.adjustment.startswith(("could not be verified", "not shown in the published", "awaits a person")))
    chips = (f'<div class="sn-chips"><b>India DPDP Act, 2023 and DPDP Rules, 2025</b><span class="sn-chip">{k("sum.crawled", u.get("crawled"))} of {c(u.get("discovered"))} discovered pages crawled</span>'
             f'<span class="sn-chip">{c(u.get("analysed"))} pages analysed</span><span class="sn-chip">Consent tested on the entry page and up to 3 more pages</span>'
             f'<span class="sn-chip">{len(d.parameters)} parameters assessed; {on_ev} decided on collected evidence</span>'
             + ('<span class="sn-chip s-not_met"><b>Blocked, incomplete:</b> the website blocked the scanner; not a website-wide assessment</span>' if decl.get("blocked") else
                f'<span class="sn-chip s-partly_met"><b>Partial:</b> {c(u.get("pending"))} pages not inspected</span>' if d.completion == "partial" else "")
             + '</div>')
    tp = [h for h in d.hosts if h.third_party]
    tiles = [(sum(h.requests for h in tp), "Third-party requests"), (len(tp), "Third-party domains"), (len(d.pre_consent.non_essential), "Analytics and advertising services before any choice"),
             (len(d.cookies), "Cookies set")]
    tile_html = '<div class="sn-tiles">' + "".join(f'<div class="sn-tile"><b>{esc(v)}</b><span>{esc(lab)}</span></div>' for v, lab in tiles) + "</div>"

    def state_cell(st: dict, key: str) -> str:
        if not st.get("tested"):
            return "Not tested"
        v = st.get(key)
        return esc(len(v) if isinstance(v, list) else fmt(v))

    pre_requests = sum(h.requests for h in d.hosts)
    consent_rows = [["Cookies in the browser", esc(len(d.cookies)), state_cell(acc, "cookies"), state_cell(rej, "cookies")],
                    ["Analytics and advertising services", esc(len(d.pre_consent.non_essential)), state_cell(acc, "services"), state_cell(rej, "services")],
                    ["Requests made", esc(pre_requests) + '<span class="muted"> (all crawled pages)</span>', state_cell(acc, "requests"), state_cell(rej, "requests")]]
    consent_tbl = table(["Metric", "Before consent", "After Accept", "After Reject"], consent_rows, widths=[34, 26, 20, 20])
    pol_urls = {p.url for p in d.policies}
    # Only pages of the submitted host belong in "Page URLs (Scanned)": links to subdomains and other websites are recorded as excluded and counted in the coverage section, never listed here.
    from app.enterprise.crawl.host import is_same_host
    own = [x for x in d.urls.shown if is_same_host(x.url, s.website_url)]
    ranked = sorted(own, key=lambda x: (x.url not in pol_urls, x.status != "done", x.depth if x.depth is not None else 99, x.url))[:8]
    page_rows = [[esc(i), f'<span class="mono">{short(x.url, 80)}</span>', esc("policy or legal page" if x.url in pol_urls else (", ".join(x.discovery_sources) or "link")), c(x.status)]
                 for i, x in enumerate(ranked, 1)]
    pages_tbl = table(["#", "Page URL", "Why it was read early", "Result"], page_rows, widths=[5, 60, 23, 12]) or '<p class="muted">No page was recorded.</p>'
    total_urls = d.urls.total or 0
    return (
        '<div class="sheet" id="summary-1">'
        '<div class="sn-head"><div class="sn-logo">Consiva<span>.ai</span></div><div class="sn-head-r"><span class="sn-pill">Enterprise DPDP Report</span>'
        '<div>Assessment based on the Digital Personal Data Protection Act, 2023 and the DPDP Rules, 2025</div></div></div>'
        f'<div class="sn-titlebar"><div><h1>Enterprise DPDP Assessment</h1><div class="sn-domain">{esc(host)}</div></div>'
        f'<div class="sn-meta"><span><small>Scan date</small><b>{esc((s.completed_at or s.created_at or d.generated_at).strftime("%d %b %Y") if (s.completed_at or s.created_at or d.generated_at) else NOT_RECORDED)}</b></span>'
        f'<span><small>Pages crawled</small><b>{c(u.get("crawled"))}</b></span><span><small>Report ID</small><b class="mono">{esc(s.scan_id[:8])}</b></span></div></div>'
        f'<div class="sn-hero">{score_card}<div class="sn-side">{elements_card}{banner_card}</div></div>'
        f'{contacts}{chips}{tile_html}'
        '<div class="sn-title">Consent States Compared<em>entry page, fresh browser sessions</em></div>' + consent_tbl
        + f'<div class="sn-title">Page URLs (Scanned)<em>policy and legal pages first; {esc(total_urls)} URLs recorded in all</em></div>' + pages_tbl
        + "</div>")


def _page2(d: ReportData) -> str:
    acc, rej = d.choice_states.get("accept", {}), d.choice_states.get("reject", {})
    acc_c, rej_c = set(acc.get("cookies") or []), set(rej.get("cookies") or [])
    ordered = sorted(d.cookies, key=lambda x: (x.first_party is not False, x.name.lower()))
    top = ordered[:5]

    def after(name: str, st: dict, names: set[str]) -> str:
        return "Not tested" if not st.get("tested") else ("Set" if name in names else "Not set")

    ck_rows = [[esc(i), f"<b>{esc(clip(x.name, 34))}</b>", f'<span class="mono">{esc(clip(x.domain, 30))}</span>', esc("first party" if x.first_party else "third party" if x.first_party is False else NOT_RECORDED),
                esc("Set" if "pre_consent" in x.consent_states else "Not seen"), esc(after(x.name, acc, acc_c)), esc(after(x.name, rej, rej_c))] for i, x in enumerate(top, 1)]
    ck_tbl = (table(["#", "Cookie", "Domain", "Party", "Before", "After Accept", "After Reject"], ck_rows, widths=[5, 19, 21, 12, 13, 15, 15])
              or '<p class="muted">No cookie was recorded on the pages crawled.</p>')
    third = sum(1 for x in d.cookies if x.first_party is False)
    collecting = [f for f in d.forms if f.collects]
    with_box = [f for f in d.forms if f.consent_controls]
    n8, c11 = _param(d, "N8"), _param(d, "C11")
    form_rows = [["Contact or enquiry form collecting personal data", esc(f"Found on {len({f.page_url for f in collecting})} page(s)" if collecting else "Not found")],
                 ["Explicit consent (a checkbox)", esc(f"Found on {len({f.page_url for f in with_box})} page(s)" if with_box else "Not found")],
                 ["Consent purpose (choice per purpose)", esc(STATE_LABEL.get(c11.state, "Fail") if c11 else NOT_RECORDED)],
                 ["Privacy notice linked beside forms", esc(STATE_LABEL.get(n8.state, "Fail") if n8 else NOT_RECORDED)]]
    forms_tbl = table(["Forms and consent", "Result"], form_rows, widths=[66, 34])
    sc = d.score
    rows: list[str] = []
    for key, g in (sc.by_group.items() if sc else []):
        members = [p for p in d.parameters if p.group == key]
        cnt = _counts(members)
        rows.append(f'<tr class="sn-grp"><td colspan="2">{esc(g.get("label"))}</td><td>{cnt["met"]} Pass · {cnt["partly_met"]} Partial · {cnt["not_met"]} Fail</td>'
                    f'<td class="num">{float(g.get("points_earned") or 0):g} / {float(g.get("points_available") or 0):g}</td></tr>')
    sev = {"not_met": 0, "partly_met": 1}
    gaps = sorted((p for p in d.parameters if p.state in sev and p.points > 0), key=lambda p: (sev[p.state], -p.points, p.id))
    shown_gaps = gaps[:10]
    for p in shown_gaps:
        rows.append(f'<tr><td class="mono">{esc(p.id)}</td><td>{esc(clip(p.name, 88))}</td><td>{chip(p.state)}</td><td class="num">{float(p.earned or 0):g} / {p.points}</td></tr>')
    if len(gaps) > len(shown_gaps):
        rows.append(f'<tr><td>–</td><td colspan="3" class="muted">{len(gaps) - len(shown_gaps)} more parameter(s) Fail or Partially Pass; every parameter, with its evidence, is in section 1 from page 3.</td></tr>')
    total = f'{sc.score}/100' if sc and sc.score is not None else "No score"
    checks = ('<table><colgroup><col style="width:8%"><col style="width:52%"><col style="width:25%"><col style="width:15%"></colgroup>'
              '<thead><tr><th>#</th><th>Group, parameter and result</th><th>Result</th><th class="num">Marks (awarded / max)</th></tr></thead>'
              f'<tbody>{"".join(rows)}</tbody><tfoot><tr class="sn-total"><td colspan="3">Total score</td><td class="num" data-k="summary_total">{esc(total)}</td></tr></tfoot></table>')
    gaps_n = len(gaps)
    return (
        '<div class="sheet" id="summary-2">'
        f'<div class="sn-row"><div><div class="sn-title">Cookie Consent Analysis<em>{"top 5 of " + str(len(d.cookies)) if len(d.cookies) > 5 else str(len(d.cookies))} cookies</em></div>{ck_tbl}'
        f'<div class="small muted">Total cookies: <b>{len(d.cookies)}</b> · Third-party: <b>{third}</b></div></div>'
        f'<div style="flex:0.75"><div class="sn-title">Forms &amp; Consent</div>{forms_tbl}</div></div>'
        '<div class="sn-title">Key Compliance Checks<em>India DPDP Act, 2023 and Rules, 2025</em></div>' + checks
        + f'<div class="sn-next"><b>{esc(str(gaps_n) + " parameter(s) need action" if gaps_n else "No gap found in this scan")}.</b> The detailed assessment from page 3 sets out every one of the '
          f'{len(d.parameters)} parameters with its status, the evidence and the page URL it was found on, and what to do.</div>'
        + f'<div class="sn-disc"><b>Disclaimer:</b> {esc(DISCLAIMER)}</div></div>')


# ---- the detailed assessment ---------------------------------------------------------------------------------------------------------------------------------------------


def _s1(d: ReportData, caps: dict) -> str:
    idx = _evidence_index(d)
    finding_by = {f.parameter_id: f for f in d.findings}
    counts = _counts(d.parameters)
    out = [f'<p>Every parameter is reported as <b>Pass</b> (full marks), <b>Partially Pass</b> (half) or <b>Fail</b> (none). '
           f'Pass: {k("state.met", counts["met"])}; Partially Pass: {k("state.partly_met", counts["partly_met"])}; Fail: {k("state.not_met", counts["not_met"])}; of {k("params_listed", len(d.parameters))} parameters '
           f'worth {k("points_total", sum(p.points for p in d.parameters))} marks. A duty that does not apply to the site is a Pass with the reason; one that could not be verified is a Fail with '
           'what could not be verified; a duty a website cannot show is judged from the published policy, and says so.</p>']
    group = None
    for p in d.parameters:
        if p.group_label != group:
            group = p.group_label
            out.append(f'<div class="grp">{esc(group)}</div>')
        refs = p.evidence_refs
        n = caps["param_refs"]
        ev = "".join(f"<br>{_where(r, idx)}" for r in refs[:n]) + (f'<br><span class="muted">and {len(refs) - n} more evidence record(s)</span>' if len(refs) > n else "")
        f = finding_by.get(p.id)
        pages = ""
        if f and f.affected_count:
            pages = f'<p><span class="lab">Pages concerned:</span> {esc(f.affected_count)}' + (f' (first: <span class="mono">{short(f.affected_urls[0], 90)}</span>)' if f.affected_urls else "") + "</p>"
        why = f'<p><span class="lab">Why this status:</span> {esc(clip(p.adjustment, 400))}</p>' if p.adjustment else ""
        rec = f'<p><span class="lab">What to do:</span> {esc(RECOMMENDATIONS.get(p.id, ""))}</p>' if p.state != "met" and RECOMMENDATIONS.get(p.id) else ""
        review = f'<p><span class="lab">Reviewed by</span> {esc(p.reviewed_by)}: {c(p.review_reason)}</p>' if p.reviewed_by else ""
        basis = ", ".join(p.legal_refs) if p.legal_refs else "supporting technical signal (not a duty named in the Act or Rules)"
        kind = " · judged from the published policy" if p.kind == "policy" else (" · shown for information, no marks" if p.points == 0 else "")
        out.append(
            f'<div class="pcard st-{esc(p.state)}"><div class="ph"><span><b>{esc(p.id)}</b> {esc(p.name)}</span><span>{chip(p.state)} <span class="marks">{float(p.earned or 0):g} / {p.points}</span></span></div>'
            f'<p class="muted">{esc(basis)}{esc(kind)}{" · in force " + esc(p.in_force) if p.in_force else ""}</p>'
            f'<p><span class="lab">What was found:</span> {esc(clip(p.note, 600)) if p.note else esc("no observation was recorded")}</p>{why}'
            f'<p><span class="lab">Evidence:</span>{ev if refs else " " + esc("no evidence record (see why this status)")}</p>{pages}{rec}{review}</div>')
    return "".join(out)


def _s2(d: ReportData, caps: dict) -> str:
    if not d.findings:
        return "<p>No finding was raised: every parameter passed.</p>"
    rows = [[f'<span class="chip sev-{esc(f.severity)}">{esc(SEV_LABEL.get(f.severity, f.severity))}</span>', c(f.parameter_id), chip(f.state), c(f.title),
             esc(f.affected_count) if f.affected_count else "–", ("yes" if f.human_review else "no")] for f in d.findings]
    out = ("<p>One finding per parameter that did not pass, most serious first. Details, evidence and what to do are in section 1. A finding marked for review rests on something that could "
           "not be verified, or on a reading a person should confirm.</p>"
           + table(["Severity", "Parameter", "Status", "Finding", "Pages", "Review"], rows, widths=[11, 10, 13, 50, 7, 9], num=(4,)))
    if d.exposure:
        out += ("<h3>Schedule ceilings for duties not fully met</h3>" + table(["Parameter", "Status", "Schedule item", "Ceiling (Rs crore)", "Provision"],
                [[c(e.parameter_id), chip(e.state), c(e.schedule_item), k(f"exposure.{e.parameter_id}", e.maximum_penalty_crore), c(", ".join(e.legal_refs))] for e in d.exposure],
                widths=[14, 16, 16, 18, 36], num=(2, 3))
                + '<p class="small muted">Each figure is the highest amount the Schedule to the Act allows for that kind of breach: a ceiling set by law, not a prediction. Duties not yet in force cannot be breached.</p>')
    return out


def _s3(d: ReportData, caps: dict) -> str:
    s, decl = d.scan, d.declaration
    u, doc, ev, t, p = (decl.get(x) or {} for x in ("urls", "documents", "evidence", "analysis_tasks", "parameters"))
    sc = d.score
    excl = u.get("excluded_by_reason") or {}
    return (
        '<div class="notice"><b>What this report is.</b> A website-observable readiness assessment against the Digital Personal Data Protection Act, 2023 and the DPDP Rules, 2025. It is not a '
        'certification of compliance and not legal advice. A website cannot show how personal data is handled inside an organisation: those duties are judged from the published policy.</div>'
        + kv([("Website scanned", f'<span class="mono">{esc(s.website_url)}</span>'), ("Scan identifier", f'<span class="mono">{esc(s.scan_id)}</span>'),
              ("Crawl", f"{c(s.crawl_status)}; started {c(s.started_at)}, finished {c(s.completed_at)}" + (f"; stopped by {c(s.stop_reason)}" if s.stop_reason else "")),
              ("Analysis", f"{c(s.analysis_status if hasattr(s, 'analysis_status') else None)}; started {c(s.analysis_started_at)}, finished {c(s.analysis_completed_at)}"),
              ("Overall completion", (f'<b>Blocked, incomplete</b>: the website blocked the scanner, so only the pages inspected before that are assessed; this is not a website-wide assessment'
                                      if decl.get("blocked") else c(d.completion))), ("Report generated", c(d.generated_at)),
              ("Score", (f'{k("score", sc.score)} of 100 ({esc(_score_label(d).lower())}); method {c(sc.version)}; marks decided on collected evidence: {k("coverage", sc.coverage)}%' if sc and sc.score is not None
                         else esc(sc.reason_no_score if sc else "no score has been calculated yet"))),
              ("Parameters", f'{k("catalogue_parameters", len(d.parameters))} parameters, {k("catalogue_points", sum(x.points for x in d.parameters))} marks in all'),
              ("Legal review status", c(d.legal_review_status))])
        + "<h3>How the scan works</h3><ul>"
        "<li><b>Scope.</b> Only the exact host that was submitted is scanned. Links to its subdomains and to other websites are recorded as excluded and never opened, and a page that "
        "redirects to another host is not followed.</li>"
        "<li><b>Discovery.</b> Every page of the site that can be discovered (links, sitemap, documents) is found and recorded first, before any page is assessed.</li>"
        "<li><b>Crawl.</b> Pages are then inspected one at a time: the home page first, then the privacy policies (read against the DPDP parameters before anything else), then the other legal "
        "pages, then every other page. Each is loaded in a real browser in a fresh session that has given no consent, scrolled to the end, and its collapsed sections, shadow content and "
        "same-site frames read. Linked PDF and DOCX documents are read page by page, with OCR for scanned pages. Nothing is typed into a field and no form is submitted.</li>"
        "<li><b>Consent tests.</b> Separate fresh sessions test what the site does after Accept, after Reject, when a choice is withdrawn, when single purposes are switched on, and how Accept and Reject compare.</li>"
        "<li><b>Assessment.</b> Each parameter is decided from the stored evidence by written rules and, for policy text, by a language model whose every fact must quote the page word for word. "
        "A person's review replaces the automated result and is recorded with who and why.</li>"
        "<li><b>Score.</b> Pass earns a parameter's marks, Partially Pass half, Fail none. Score = marks earned out of 100, rounded half up.</li></ul>"
        "<h3>Coverage declaration</h3>"
        + table(["What", "Count"], [[esc(label), k(f"urls.{key}", u.get(key))] for key, label in (
            ("discovered", "URLs discovered"), ("eligible", "Eligible to crawl (submitted host)"), ("crawled", "Inspected on the submitted host"), ("analysed", "Analysed"),
            ("pending", "Pending, not inspected"), ("excluded", "Excluded"), ("excluded_subdomain", "Excluded: subdomain or other host of the site"),
            ("excluded_external_domain", "Excluded: other websites"), ("failed", "Failed"), ("blocked", "Blocked by the website"), ("redirected", "Redirected"),
            ("inaccessible", "Inaccessible (HTTP 401 or 403)"), ("awaiting_analysis", "Crawled, awaiting analysis"), ("analysis_failed", "Analysis failed"))]
            + [[esc(label), k(f"documents.{key}", doc.get(key))] for key, label in (("discovered", "Documents discovered"), ("extracted", "Documents extracted"), ("unreadable", "Documents unreadable"))]
            + [[esc("Evidence records"), k("evidence.records", ev.get("records"))], [esc("Analysis tasks done / failed / pending"), f'{c(t.get("done"))} / {c(t.get("failed"))} / {c(t.get("pending"))}'],
               [esc("Parameters Pass / Partially Pass / Fail"), f'{c(p.get("pass"))} / {c(p.get("partially_pass"))} / {c(p.get("fail"))}'],
               [esc("Fails that rest on something not verifiable"), k("parameters.fail_not_verified", p.get("fail_not_verified"))]],
            widths=[70, 30], num=(1,), cls="compact")
        + ("<h4>Excluded, by reason</h4>" + table(["Reason", "URLs"], [[esc(a), k(f"excluded.{a}", b)] for a, b in sorted(excl.items())], widths=[70, 30], num=(1,), cls="compact") if excl else ""))


def _s4(d: ReportData, caps: dict) -> str:
    pc = d.pre_consent
    blocks = []
    for t in d.consent:
        status = "carried out" if t.tested is True else "not carried out: " + REASON_LABEL.get(t.reason or "", t.reason or "no reason recorded")
        facts = t.facts[: caps["consent_facts"]]
        more = f'<p class="cap">{len(t.facts) - len(facts)} more line(s) in the evidence of this test.</p>' if len(t.facts) > len(facts) else ""
        blocks.append(f'<h3>{esc(CONSENT_LABEL.get(t.name, t.name))}</h3><p>Test {esc(status)}.</p>' + (kv([(a, c(b)) for a, b in facts]) if facts else "") + more)
    return ("<h3>Before any choice</h3>"
            f'<p>Every crawled page was loaded in a session that had given no consent. Of {k("pre_pages", pc.pages_observed)} pages whose requests were recorded, {k("pre_with", pc.pages_with_non_essential)} '
            "contacted analytics or advertising services before any choice." + (f' Services named: {c(", ".join(pc.non_essential))}.' if pc.non_essential else "") + "</p>"
            + "".join(blocks))


def _s5(d: ReportData, caps: dict) -> str:
    n = caps["cookies"]
    ordered = sorted(d.cookies, key=lambda x: (x.first_party is not False, x.name.lower()))
    ck = [[c(x.name), f'<span class="mono">{esc(x.domain)}</span>', c("first party" if x.first_party else ("third party" if x.first_party is False else None)),
           esc(", ".join(f for f, on in (("Secure", x.secure), ("HttpOnly", x.http_only)) if on) or "no Secure or HttpOnly") + f"<br>SameSite: {c(x.same_site)}",
           ("session" if x.session else c(x.expires)) if x.session is not None else esc(NOT_RECORDED), pages_cell(x.pages, 2)] for x in ordered[:n]]
    hosts = [h for h in d.hosts if h.third_party]
    m = caps["hosts"]
    tp = [[f'<span class="mono">{esc(h.host)}</span>', c(h.requests), pages_cell(h.pages, 2), c("; ".join(h.classified)) if h.classified else "not classified as analytics or advertising"] for h in hosts[:m]]
    return (f'<h3>Cookies</h3><p>{k("cookies_inventory", len(d.cookies))} distinct cookie(s), identified by name, domain and path; third-party cookies first. Cookie values are never kept.</p>'
            + (table(["Cookie", "Domain", "Party", "Flags", "Lifetime", "Seen on"], ck, widths=[17, 20, 10, 17, 10, 26], cls="compact") if ck else "<p>No cookie was observed.</p>")
            + capped("cookies", len(ck), len(d.cookies), "evidence?kind=cookie", d.scan.scan_id)
            + f'<h3>Third-party hosts</h3><p>{k("third_party_inventory", len(hosts))} third-party host(s) were contacted by the browser, most requests first.</p>'
            + (table(["Host", "Requests", "Seen on", "Classification"], tp, widths=[26, 9, 35, 30], num=(1,), cls="compact") if tp else "<p>No third-party host was contacted.</p>")
            + capped("third-party hosts", len(tp), len(hosts), "evidence?kind=network", d.scan.scan_id))


def _s6(d: ReportData, caps: dict) -> str:
    groups: dict[tuple, list] = {}
    for f in d.forms:
        sig = ("" if f.action == f.page_url else f.action, f.method, tuple((x.get("name") or x.get("id") or "", x.get("type") or "", bool(x.get("required"))) for x in f.fields),
               tuple((x.get("label") or x.get("name") or "") for x in f.consent_controls), f.privacy_link)
        groups.setdefault(sig, []).append(f)
    rows = []
    for members in sorted(groups.values(), key=lambda m: (-len(m), m[0].page_url)):
        f = members[0]
        pages = sorted({m.page_url for m in members})
        fields = ", ".join(f'{esc(x.get("name") or x.get("id") or x.get("type") or "unnamed")}{" (required)" if x.get("required") else ""}' for x in f.fields[:12]) or esc(NOT_RECORDED)
        cc = "; ".join((x.get("label") or x.get("name") or "checkbox") for x in f.consent_controls)
        rows.append([pages_cell(pages, 2), fields, c(", ".join(f.collects)) if f.collects else "no recognised personal data type",
                     (f"{len(f.consent_controls)} checkbox(es): {esc(clip(cc, 120))}" if f.consent_controls else "no consent checkbox"), c(f.privacy_link)])
    n = caps["forms"]
    return (f'<p>{k("forms_inventory", len(d.forms))} form(s) found, in {len(rows)} distinct kind(s). Identical forms are shown once with the pages that carry them. No form was submitted.</p>'
            + (table(["Pages", "Fields", "Collects", "Consent control", "Privacy link"], rows[:n], widths=[24, 28, 16, 22, 10], cls="compact") if rows else "<p>No form was found on the pages crawled.</p>")
            + capped("kinds of form", min(n, len(rows)), len(rows), "evidence?kind=form", d.scan.scan_id))


def _s7(d: ReportData, caps: dict) -> str:
    sc = [h for h in d.hosts if h.script_requests]
    n = caps["hosts"]
    rows = [[f'<span class="mono">{esc(h.host)}</span>', c("third party" if h.third_party else "first party"), c(h.script_requests), c("; ".join(h.classified)) if h.classified else esc("not classified")]
            for h in sc[:n]]
    m = caps["iframes"]
    fr = [[url_cell(i.src), c("third party" if i.third_party else "first party"), url_cell(i.page_url)] for i in d.iframes[:m]]
    return ((table(["Script host", "Party", "Script requests", "Classification"], rows, widths=[34, 14, 14, 38], num=(2,), cls="compact") if rows else "<p>No script request was recorded.</p>")
            + capped("script hosts", len(rows), len(sc), "evidence?kind=network", d.scan.scan_id)
            + "<h3>Embedded frames</h3>" + (table(["Frame source", "Party", "On page"], fr, widths=[45, 12, 43], cls="compact") if fr else "<p>No embedded frame was recorded.</p>")
            + capped("frames", len(fr), len(d.iframes), "evidence?kind=page_html", d.scan.scan_id))


def _s8(d: ReportData, caps: dict) -> str:
    s = d.security
    tls = s.tls
    tls_html = (kv([("Certificate verified", c(tls.get("verified"))), ("Negotiated protocol", c(tls.get("negotiated"))), ("TLS 1.3 offered", c(tls.get("tls13_supported"))),
                    ("Issuer", c(tls.get("issuer"))), ("Valid until", c(tls.get("not_after"))), ("Error", c(tls.get("verify_error") or tls.get("error")) if (tls.get("verify_error") or tls.get("error")) else "none recorded")])
                if tls else "<p>No TLS probe was recorded for this scan.</p>")
    hdr = [[f'<span class="mono">{esc(h)}</span>', k(f"hdr.{h}", s.header_counts.get(h)), esc(f"of {s.pages_total} pages")] for h in s.headers_checked]
    n = caps["security"]
    pg = [[url_cell(p.page_url), c(p.https), c(", ".join(p.missing)) if p.missing else "none missing"] for p in s.pages[:n]]
    return (f"<h3>TLS and certificate</h3>{tls_html}"
            f'<h3>Transport and headers</h3><p>{k("sec_pages_total", s.pages_total)} page(s) had a response record; {k("sec_pages_https", s.pages_https)} were served over HTTPS. Clear-text requests: {k("sec_insecure", s.insecure_requests)}.</p>'
            + (table(["Protective header", "Pages carrying it", "Of"], hdr, widths=[50, 25, 25], num=(1,), cls="compact") if s.pages_total else "")
            + (table(["Page", "HTTPS", "Headers missing"], pg, widths=[50, 10, 40], cls="compact") if pg else "")
            + capped("pages", len(pg), s.pages_total, "evidence?kind=header", d.scan.scan_id))


def _s9(d: ReportData, caps: dict) -> str:
    pol = [[url_cell(p.url), esc(", ".join(p.kinds)), c(p.title), c(f"{p.pages} page(s)" if p.pages is not None else (f"{p.size} bytes" if p.size is not None else None))] for p in d.policies]
    doc = d.declaration.get("documents") or {}
    n = caps["documents"]
    rows = [[url_cell(x.url), c(x.extraction_status), c(x.extraction_method), c(x.pages_read), c(x.status_reason)] for x in d.documents[:n]]
    return ("<h3>Policy pages and documents identified</h3>"
            + (table(["Address", "Kind", "Title", "Read"], pol, widths=[40, 18, 30, 12], cls="compact") if pol else "<p>No page or document whose address names a policy was found.</p>")
            + f'<h3>Linked documents</h3><p>Discovered: {k("docs_discovered", doc.get("discovered"))}; extracted: {k("docs_extracted", doc.get("extracted"))}; partly extracted: {k("docs_partly", doc.get("partly_extracted"))}; '
              f'unreadable: {k("docs_unreadable", doc.get("unreadable"))}. Scanned pages are read with OCR.</p>'
            + (table(["Document", "Extraction", "Method", "Pages read", "Note"], rows, widths=[38, 14, 14, 10, 24], num=(3,), cls="compact") if rows else "<p>No linked document was found.</p>")
            + capped("documents", len(rows), len(d.documents), "documents", d.scan.scan_id))


def _s10(d: ReportData, caps: dict) -> str:
    n = caps["urls"]
    rows = [[url_cell(x.url), c(x.depth), c(x.status), c(x.http_status), c(x.status_reason) if x.status_reason else "–"] for x in d.urls.shown[:n]]
    return (f'<p>{k("urls_discovered_inv", d.urls.total)} URLs were recorded, ordered by depth.</p>'
            + (table(["URL", "Depth", "Status", "HTTP", "Reason"], rows, widths=[56, 7, 12, 7, 18], num=(1, 3), cls="compact") if rows else "<p>No URL was recorded.</p>")
            + capped("URLs", len(rows), d.urls.total, "urls", d.scan.scan_id))


def _s11(d: ReportData, caps: dict) -> str:
    lg = d.legal
    com = [[c(a), c(b)] for a, b in lg.commencement]
    return (f'<div class="notice warn"><b>When the duties apply.</b> Most duties under the Act and the Rules come into force on <b>{esc(lg.main_date)}</b> (G.S.R. 843(E); Rule 1(4)). Until then this report '
            f'measures readiness for those duties, not a breach of them. Section 6(9) and Rule 4 (Consent Managers) come into force on {esc(lg.consent_manager_date)}.</div>'
            + table(["Date", "Provisions"], com, widths=[16, 84], cls="compact")
            + "<h3>Parts of the Act and Rules outside a website assessment</h3><p>These are not duties a website owner can show; they are listed so nobody assumes they were overlooked.</p>"
            + table(["Provision", "What it covers"], [[esc(a), esc(b)] for a, b in OUTSIDE_WEBSITE], widths=[34, 66], cls="compact")
            + f'<p class="small"><b>Legal review status:</b> {c(lg.legal_review_status)}. Sources: ' + "; ".join(f"{esc(x.title)} ({c(x.gazette_identifier)})" for x in lg.sources) + ".</p>")


def _s12(d: ReportData, caps: dict) -> str:
    n = caps["evidence"]
    rows = [[f'<span class="mono">{esc(e.ref)}</span>', c(e.kind), url_cell(e.source_url), c(e.page_number) if e.page_number else "–", c(", ".join(e.cited_by[:3]))] for e in d.evidence[:n]]
    return (f'<p>{k("evidence_total", d.evidence_total)} evidence record(s) are cited by the parameters and findings. Each can be opened by its identifier, which returns the stored content and its fingerprint.</p>'
            + (table(["Reference", "Kind", "Page URL", "Doc. page", "Cited by"], rows, widths=[22, 9, 41, 8, 20], cls="compact") if rows else "<p>No evidence reference is cited.</p>")
            + capped("evidence references", len(rows), d.evidence_total, "evidence", d.scan.scan_id))


def _s13(d: ReportData, caps: dict) -> str:
    items = [
        f"<b>Legal review status: {esc(d.legal_review_status)}.</b> The legal readings behind the parameters have not been signed off by a named legal reviewer.",
        "Website evidence cannot certify compliance. Duties a website cannot show (security controls, breach handling, retention, rights fulfilment) are judged from what the published policy says.",
        "Consent tests ran on the entry page and up to three further pages. Behaviour on other pages, devices, locations or after logging in was not tested.",
        "Observations come from requests and cookies recorded by an automated browser; a service that sends no request is not seen.",
        "Cookie values are never kept and no form is submitted, so what a form does with data after submission cannot be observed.",
        "Penalty ceilings are the limits the Schedule sets and are not predictions of any penalty.",
    ]
    decl = d.declaration
    u = decl.get("urls") or {}
    dyn = []
    if decl.get("blocked"):
        dyn.append(f"This scan is <b>blocked and incomplete</b>: the website blocked the scanner ({c(decl.get('stop_reason'))}), so only the pages inspected before that were assessed. "
                   "It is not a website-wide assessment; resuming the scan inspects the rest.")
    elif d.completion != "complete":
        dyn.append(f"This scan is <b>{c(d.completion)}</b>: crawl {c(decl.get('crawl_status'))}, analysis {c(decl.get('analysis_status'))}.")
    for key, label in (("pending", "URLs still pending"), ("failed", "URLs failed"), ("blocked", "URLs blocked by the website"), ("inaccessible", "URLs inaccessible (HTTP 401 or 403)"),
                       ("analysis_failed", "pages whose analysis failed")):
        if u.get(key):
            dyn.append(f"{k('lim.' + key, u[key])} {esc(label)}.")
    if d.unreadable_observations:
        dyn.append(f"{k('lim.unreadable', d.unreadable_observations)} stored observation(s) could not be read.")
    for t in d.consent:
        if t.tested is not True:
            dyn.append(f"Consent test \"{esc(CONSENT_LABEL.get(t.name, t.name))}\" was not carried out: {esc(REASON_LABEL.get(t.reason or '', t.reason or 'no reason recorded'))}.")
    unver = [p.id for p in d.parameters if p.adjustment and p.adjustment.startswith(("could not be verified", "not shown in the published", "awaits a person"))]
    if unver:
        dyn.append(f"Reported as Fail because they could not be verified: {c(', '.join(unver))}. A person should check them on the site.")
    return ("<h3>What this report cannot say</h3><ul>" + "".join(f"<li>{i}</li>" for i in items) + "</ul>"
            + "<h3>Specific to this scan</h3>" + ("<ul>" + "".join(f"<li>{i}</li>" for i in dyn) + "</ul>" if dyn else "<p>The scan completed with nothing outstanding.</p>"))


_BUILDERS = (_s1, _s2, _s3, _s4, _s5, _s6, _s7, _s8, _s9, _s10, _s11, _s12, _s13)


def render_html(d: ReportData, *, level: int = 0) -> str:
    """The complete report as one HTML document: the two-page summary, then the detailed assessment with lists capped at `level` (0 = the most rows). No request, no script."""
    caps = CAPS[max(0, min(level, MAX_LEVEL))]
    s = d.scan
    host = urlparse(s.website_url).netloc or s.domain
    body = "".join(_section(i, b(d, caps), first=(i == 1)) for i, b in enumerate(_BUILDERS, 1))
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Enterprise DPDP report: {esc(host)}</title>'
            f'<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="consiva-report-scan" content="{esc(s.scan_id)}"><meta name="consiva-report-site" content="{esc(host)}">'
            f'<meta name="consiva-report-level" content="{esc(level)}">'
            f'<style>{CSS}</style></head><body>{_page1(d)}{_page2(d)}{body}</body></html>')
