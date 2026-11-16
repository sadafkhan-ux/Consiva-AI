"""Renders the Enterprise report as one self-contained, print-ready HTML document: inline CSS, A4, no script, no external request.

Every value comes from the `ReportData` object and is HTML-escaped (it originates in scanned websites and is untrusted). A value that was not recorded is printed as "not recorded", never as an
empty cell, and nothing is computed here that the data object does not already hold, except sums of its own rows that are shown beside the figure they must equal.
"""

from __future__ import annotations

from datetime import datetime
from html import escape
from urllib.parse import urlparse

from app.enterprise.report.data import ReportData, ParameterRow

NOT_RECORDED = "not recorded"
SECTIONS = (
    "Executive summary", "Scan scope and methodology", "Coverage declaration", "URL inventory", "Privacy policy analysis", "Cookie policy analysis", "Consent behaviour",
    "Cookie and tracker inventory", "Forms and data collection", "Scripts and third-party integrations", "Technical security findings", "Document extraction results",
    "Applicable DPDP legal framework", "Parameter-level assessments", "Independent Enterprise score", "Risk-prioritised findings", "Evidence references", "Recommendations",
    "Human-review requirements", "Limitations and unverified items",
)
STATE_LABEL = {"met": "Met", "partly_met": "Partly met", "not_met": "Not met", "not_assessed": "Not assessed", "not_applicable": "Not applicable", "needs_records": "Needs records",
               "human_review": "Human review"}
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

CSS = """
:root { --ink:#1b2333; --muted:#566074; --rule:#d3d9e3; --soft:#f3f5f9; --accent:#1f4e79; --accent-soft:#e6eef6; --high:#9b2226; --medium:#a35f00; --low:#2b6a4d; --info:#4a5568; }
* { box-sizing: border-box; }
@page { size: A4; margin: 22mm 16mm 20mm 16mm; }
html { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
body { margin: 0; background: #fff; color: var(--ink); font-family: "Segoe UI", "Helvetica Neue", Helvetica, Arial, sans-serif; font-size: 10pt; line-height: 1.5; }
h1 { font-size: 24pt; line-height: 1.15; margin: 0 0 4pt; color: var(--accent); letter-spacing: -0.2pt; }
h2 { font-size: 14pt; color: var(--accent); margin: 22pt 0 6pt; padding-bottom: 3pt; border-bottom: 1.5pt solid var(--accent); break-after: avoid; }
h3 { font-size: 10.5pt; margin: 12pt 0 4pt; color: var(--ink); break-after: avoid; }
h4 { font-size: 9.5pt; margin: 8pt 0 3pt; color: var(--muted); text-transform: uppercase; letter-spacing: 0.4pt; break-after: avoid; }
p { margin: 0 0 6pt; orphans: 3; widows: 3; }
.lead { font-size: 10.5pt; color: var(--muted); margin-bottom: 10pt; }
.cover { border-bottom: 3pt solid var(--accent); padding: 25mm 0 10pt; margin-bottom: 16pt; }
.toc { break-after: page; }
#s1 { break-after: page; }
#s3, #s7, #s8, #s13, #s14, #s15, #s16, #s19 { break-before: page; }
#s1 h2 { margin-top: 0; }
.cover .meta { color: var(--muted); font-size: 9pt; }
.cover .meta b { color: var(--ink); }
.notice { border: 1pt solid var(--accent); background: var(--accent-soft); padding: 7pt 10pt; margin: 8pt 0 10pt; break-inside: avoid; }
.notice.warn { border-color: var(--medium); background: #fbf3e4; }
.tile { display: flex; gap: 10pt; margin: 8pt 0 10pt; break-inside: avoid; }
.tile > div { flex: 1; border: 1pt solid var(--rule); background: var(--soft); padding: 7pt 9pt; }
.tile .big { font-size: 24pt; font-weight: 700; line-height: 1.1; color: var(--accent); }
.tile .lab { font-size: 8pt; text-transform: uppercase; letter-spacing: 0.4pt; color: var(--muted); }
.toc { columns: 2; column-gap: 16pt; font-size: 9pt; margin: 6pt 0 10pt; padding-left: 0; list-style: none; }
.toc li { break-inside: avoid; padding: 1pt 0; }
.toc a { color: var(--ink); text-decoration: none; }
table { width: 100%; border-collapse: collapse; table-layout: fixed; margin: 6pt 0 14pt; font-size: 8.3pt; line-height: 1.4; }
th, td { border: 0.5pt solid var(--rule); padding: 4pt 5pt; text-align: left; vertical-align: top; overflow-wrap: anywhere; word-break: break-word; }
thead { display: table-header-group; }
tfoot { display: table-footer-group; }
tr { break-inside: avoid; page-break-inside: avoid; }
th { background: var(--accent); color: #fff; font-weight: 600; font-size: 7.5pt; text-transform: uppercase; letter-spacing: 0.3pt; border-color: var(--accent); }
tbody tr:nth-child(even) td { background: #f8f9fc; }
table.compact th, table.compact td { padding: 1.5pt 4pt; font-size: 7pt; line-height: 1.25; }
td.num, th.num { text-align: right; }
table.kv th { background: var(--soft); color: var(--ink); text-transform: none; letter-spacing: 0; font-size: 8pt; width: 34%; border-color: var(--rule); }
.mono { font-family: Consolas, "Courier New", monospace; font-size: 7.2pt; }
.muted { color: var(--muted); }
.small { font-size: 8pt; }
.chip { display: inline-block; padding: 0 4pt; border-radius: 2pt; font-size: 7.5pt; font-weight: 600; border: 0.5pt solid currentColor; white-space: nowrap; }
.s-met { color: var(--low); } .s-partly_met { color: var(--medium); } .s-not_met { color: var(--high); }
.s-not_assessed, .s-not_applicable, .s-needs_records, .s-human_review { color: var(--info); }
.chip.sev-high { color: var(--high); } .chip.sev-medium { color: var(--medium); } .chip.sev-low { color: var(--low); } .chip.sev-info { color: var(--info); }
.finding { border: 0.5pt solid var(--rule); border-left: 3pt solid var(--info); padding: 6pt 9pt; margin: 6pt 0; break-inside: avoid; }
.finding.sev-high { border-left-color: var(--high); } .finding.sev-medium { border-left-color: var(--medium); } .finding.sev-low { border-left-color: var(--low); }
.finding h3 { margin: 0 0 3pt; } .finding p { margin-bottom: 3pt; } .finding .meta { color: var(--muted); font-size: 8pt; margin-bottom: 4pt; }
ul { margin: 2pt 0 6pt; padding-left: 14pt; } li { margin-bottom: 2pt; }
.urls { font-size: 7.5pt; } .urls li { overflow-wrap: anywhere; word-break: break-word; }
.cap { color: var(--muted); font-size: 8pt; margin: -4pt 0 8pt; }
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
    return f'<span class="chip s-{esc(state)}">{esc(STATE_LABEL.get(state, state))}</span>'


def short_ref(ref: str) -> str:
    return ref[:11] if ref.startswith("ev:") else ref[:40]


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


def _states(parameters: list[ParameterRow]) -> dict[str, int]:
    out: dict[str, int] = {}
    for p in parameters:
        out[p.state] = out.get(p.state, 0) + 1
    return out


def _section(n: int, body: str) -> str:
    return f'<section id="s{n}"><h2>{n}. {esc(SECTIONS[n - 1])}</h2>{body}</section>'


# ---- sections -----------------------------------------------------------------------------------------------------------------------------------------------------------

def _score_is_shown(d: ReportData) -> bool:
    return d.score is not None and d.score.score is not None


def _score_label(d: ReportData) -> str:
    return "Readiness score" if d.score is None or d.score.basis == "readiness" else "Assessment score"


def _s1(d: ReportData) -> str:
    s, params, decl = d.scan, d.parameters, d.declaration
    states = _states(params)
    if _score_is_shown(d):
        sc = d.score
        score_tile = (f'<div><div class="lab">{esc(_score_label(d))} (0 to 100)</div><div class="big">{k("score", sc.score)}</div>'
                      f'<div class="small muted">{esc("readiness for the Act as it will apply from " + d.legal.main_date if sc.basis == "readiness" else "assessment against duties in force")}</div></div>'
                      f'<div><div class="lab">Coverage</div><div class="big">{k("coverage", sc.coverage)}%</div><div class="small muted">of the 100 points could be assessed from the website</div></div>')
    elif d.score is not None:
        score_tile = (f'<div><div class="lab">{esc(_score_label(d))}</div><div class="big">No score</div><div class="small muted">{c(d.score.reason_no_score)}</div></div>'
                      f'<div><div class="lab">Coverage</div><div class="big">{k("coverage", d.score.coverage)}%</div><div class="small muted">of the 100 points could be assessed from the website</div></div>')
    else:
        score_tile = '<div><div class="lab">Score</div><div class="big">No score</div><div class="small muted">no score has been calculated for this scan yet</div></div>'
    sev: dict[str, int] = {}
    for f in d.findings:
        sev[f.severity] = sev.get(f.severity, 0) + 1
    u = decl.get("urls") or {}
    top = [f for f in d.findings if f.severity in ("high", "medium")][:5]
    hr_params = [p for p in params if p.state in ("human_review", "needs_records")]
    return (
        '<div class="notice"><b>What this report is.</b> This is a website-observable readiness assessment. It is not a certification of compliance with the Digital Personal Data Protection '
        'Act, 2023, and it is not legal advice. It reports what could be observed from the public website; a website cannot show how personal data is handled inside an organisation.</div>'
        f'<div class="tile">{score_tile}</div>'
        + f'<p class="small muted">Legal review status of the legal readings behind the parameters: {c(d.legal_review_status)}.</p>'
        + "<h3>Where the scan stands</h3>"
        + kv([("Crawl", f'{c(decl.get("crawl_status"))}'), ("Analysis", f'{c(decl.get("analysis_status"))}'),
              ("Assessment", f'{k("assessed", (decl.get("parameters") or {}).get("assessed"))} of {k("params_total", len(params))} parameters assessed as met, partly met or not met'),
              ("Overall completion", f'{c(d.completion)}')])
        + "<h3>Key figures</h3>"
        + kv([("Website", f'<span class="mono">{esc(s.website_url)}</span>'), ("URLs discovered", k("urls_discovered", u.get("discovered"))), ("Pages crawled", k("urls_crawled", u.get("crawled"))),
              ("Pages analysed", k("urls_analysed", u.get("analysed"))), ("Distinct cookies observed", k("cookies_total", len(d.cookies))),
              ("Third-party hosts contacted", k("third_party_hosts", sum(1 for h in d.hosts if h.third_party))), ("Forms found", k("forms_total", len(d.forms))),
              ("Findings", f'{k("findings_total", len(d.findings))} ({", ".join(f"{sev[x]} {SEV_LABEL[x].lower()}" for x in ("high", "medium", "low", "info") if x in sev) or "none raised"})'),
              ("Parameters for human review or records", k("human_review_total", len(hr_params))),
              ("Pages that contacted analytics or advertising before any choice",
               f'{k("pre_consent_pages", d.pre_consent.pages_with_non_essential)} of {k("pages_observed", d.pre_consent.pages_observed)} pages observed')])
        + ("<h3>Most important findings</h3>" + table(["Severity", "Parameter", "Finding"], [[f'<span class="chip sev-{esc(f.severity)}">{esc(SEV_LABEL.get(f.severity, f.severity))}</span>', c(f.parameter_id), c(f.title)] for f in top],
                                                         widths=[14, 12, 74]) if top else "")
    )


def _s2(d: ReportData) -> str:
    s = d.scan
    assessors: dict[str, int] = {}
    for p in d.parameters:
        if p.assessor:
            assessors[p.assessor] = assessors.get(p.assessor, 0) + 1
    opts = sorted((str(a), str(b)) for a, b in s.options.items())
    return (
        kv([("Website scanned", f'<span class="mono">{esc(s.website_url)}</span>'), ("Domain", c(s.domain)), ("Scan identifier", f'<span class="mono">{esc(s.scan_id)}</span>'),
            ("Scan requested", c(s.created_at)), ("Crawl started", c(s.started_at)), ("Crawl finished", c(s.completed_at)), ("Analysis started", c(s.analysis_started_at)),
            ("Analysis finished", c(s.analysis_completed_at)), ("Crawl status", c(s.crawl_status)), ("Stop reason", c(s.stop_reason) if s.stop_reason else "the crawl was not stopped by a limit"),
            ("Report generated", c(d.generated_at)), ("Scoring method version", c(d.score.version if d.score else None)),
            ("Parameters in the catalogue", f'{k("catalogue_parameters", len(d.parameters))} parameters; {k("catalogue_points", sum(p.points for p in d.parameters))} points in total')])
        + ("<h4>Scan options as requested</h4>" + table(["Option", "Value"], [[esc(a), esc(b)] for a, b in opts], widths=[35, 65]) if opts else "")
        + ("<p><b>Scan error recorded:</b> " + c(s.error) + "</p>" if s.error else "") + ("<p><b>Analysis error recorded:</b> " + c(s.analysis_error) + "</p>" if s.analysis_error else "")
        + "<h3>How the scan works</h3><ul>"
        "<li><b>Crawl.</b> Each page is loaded in a real browser in a fresh session that has given no consent, scrolled, and its collapsed sections opened. The page text, the cookies set, the requests made, the forms and the response headers are kept as evidence. Linked PDF and DOCX documents are fetched and read page by page. Nothing is typed into a field and no form is submitted.</li>"
        "<li><b>Consent tests.</b> After the crawl, separate fresh sessions test what the site does after Accept, after Reject, when a choice is withdrawn, and when single purposes are switched on.</li>"
        "<li><b>Assessment.</b> Each of the parameters is assessed from the stored evidence by written rules and, for the policy text, by a language model whose reading is recorded with its confidence. A low-confidence reading goes to a person and earns or loses no points. A person's review replaces the automated result and is recorded with who and why.</li>"
        "<li><b>Score.</b> Points are spread over the scored parameters. Met earns all of a parameter's points, partly met half (only where a partial rule exists), not met none. Parameters that were not assessed, do not apply, need records or await review are taken out of the calculation. Score = 100 x points earned / points in play, rounded half up. Coverage is the points in play out of 100; it is reported beside the score and never withholds it.</li></ul>"
        + ("<h4>Who assessed the parameters</h4>" + table(["Assessor", "Parameters"], [[esc(a), esc(n)] for a, n in sorted(assessors.items())], widths=[60, 40], num=(1,)) if assessors else "")
    )


def _s3(d: ReportData) -> str:
    decl = d.declaration
    u, doc, ev, t, p = (decl.get(x) or {} for x in ("urls", "documents", "evidence", "analysis_tasks", "parameters"))

    def pairs(prefix: str, src: dict, labels: list[tuple[str, str]]) -> str:
        return kv([(label, k(f"{prefix}.{key}", src.get(key))) for key, label in labels])

    excl = u.get("excluded_by_reason") or {}
    return (
        f'<p>Crawl status <b>{c(decl.get("crawl_status"))}</b>; analysis status <b>{c(decl.get("analysis_status"))}</b>; overall <b>{c(d.completion)}</b>. Every figure below is a count of stored rows. A figure that could not yet be known is shown as not recorded, never as zero.</p>'
        "<h3>URLs</h3>" + pairs("urls", u, [("discovered", "Discovered"), ("eligible", "Eligible to crawl"), ("crawled", "Crawled"), ("analysed", "Analysed"), ("pending", "Pending"), ("excluded", "Excluded"),
                                            ("failed", "Failed"), ("redirected", "Redirected"), ("inaccessible", "Inaccessible (HTTP 401 or 403)"), ("unsupported", "Unsupported"),
                                            ("duplicates_merged", "Duplicate discoveries merged"), ("awaiting_analysis", "Crawled, awaiting analysis"), ("analysis_failed", "Analysis failed")])
        + ("<h4>Excluded, by reason</h4>" + table(["Reason", "URLs"], [[esc(a), k(f"excluded.{a}", b)] for a, b in sorted(excl.items())], widths=[70, 30], num=(1,)) if excl else "")
        + "<h3>Documents</h3>" + pairs("documents", doc, [("discovered", "Discovered"), ("extracted", "Extracted"), ("partly_extracted", "Partly extracted"), ("unreadable", "Unreadable"),
                                                       ("extraction_not_recorded", "Extraction not recorded")])
        + "<h3>Evidence and analysis tasks</h3>"
        + kv([("Evidence records", k("evidence.records", ev.get("records"))), ("Distinct evidence sources", k("evidence.sources", ev.get("sources"))), ("Evidence analysed", k("evidence.analysed", ev.get("analysed"))),
              ("Analysis tasks done", k("tasks.done", t.get("done"))), ("Analysis tasks failed", k("tasks.failed", t.get("failed"))), ("Analysis tasks pending", k("tasks.pending", t.get("pending")))])
        + "<h3>Parameters</h3>" + pairs("parameters", p, [("total", "In the catalogue"), ("assessed", "Assessed (met, partly met or not met)"), ("not_assessed", "Not assessed"), ("not_applicable", "Not applicable"),
                                                         ("awaiting_human_review", "Awaiting human review"), ("needs_records", "Need records"), ("recorded", "With a recorded assessment")])
    )


def _s4(d: ReportData) -> str:
    u = d.declaration.get("urls") or {}
    rows = [[url_cell(x.url) + (f'<br><span class="muted small">final: </span><span class="mono">{short(x.final_url, 70)}</span>' if x.final_url and x.final_url != x.url else ""), c(x.depth), c(x.status),
             c(x.analysis_status), c(x.http_status), c(x.status_reason) if x.status_reason else esc(NOT_RECORDED), c(", ".join(x.discovery_sources))] for x in d.urls.shown]
    total = d.urls.total
    return (f'<p>{k("urls_discovered_inv", total)} URLs were discovered. Crawled: {k("urls_crawled_inv", u.get("crawled"))}; excluded: {k("urls_excluded_inv", u.get("excluded"))}; failed: {k("urls_failed_inv", u.get("failed"))}; pending: {k("urls_pending_inv", u.get("pending"))}.</p>'
            + (f'<p class="cap">Showing the first {k("urls_shown", len(d.urls.shown))} of {k("urls_total", total)} URLs, ordered by depth. The full list is available from the scan in the application.</p>' if total and total > len(d.urls.shown) else
               (f'<p class="cap">All {k("urls_shown", len(d.urls.shown))} URLs are listed.</p>' if d.urls.shown else ""))
            + (table(["URL", "Depth", "Status", "Analysis", "HTTP", "Reason", "Found by"], rows, widths=[36, 6, 9, 10, 6, 20, 13], num=(1, 4), cls="compact") if rows else "<p>No URL was recorded for this scan.</p>"))


def _param_table(params: list[ParameterRow]) -> str:
    return table(["Parameter", "State", "What was observed"], [[f'<b>{esc(p.id)}</b> {c(p.name)}', chip(p.state), c(p.note)] for p in params], widths=[38, 12, 50])


def _policy_table(rows) -> str:
    return table(["Address", "Title", "Content hash (SHA-256)", "Size"], [[url_cell(p.url), c(p.title) + (f'<br><span class="muted small">from the {esc(p.title_source)}</span>' if p.title_source else ""),
                                                                           f'<span class="mono">{c(p.content_hash)}</span>',
                                                                           c(f"{p.size} bytes" if p.size is not None else None) + (f"<br>{p.pages} page(s) read" if p.pages is not None else "")] for p in rows],
                 widths=[28, 24, 38, 10])


def _s5(d: ReportData) -> str:
    rows = [p for p in d.policies if "Privacy policy" in p.kinds]
    notice = [p for p in d.parameters if p.group in ("notice",) or p.id in ("G1", "G2", "G3", "G4", "G5", "G6", "G7", "G8")]
    return ("<p>Pages and documents whose address names a privacy policy are identified below by address pattern; the content hash fingerprints the exact text that was read, so a later scan can show whether the policy changed.</p>"
            + (f'<p>{k("privacy_policies", len(rows))} privacy policy page(s) or document(s) identified.</p>' + _policy_table(rows) if rows else
               '<p><b>No page or document whose address names a privacy policy was found among the stored evidence.</b> This does not show that the site has no notice: it may sit at an address that does not name it.</p>')
            + "<h3>Notice, contact, grievance and rights parameters</h3>" + _param_table(notice))


def _s6(d: ReportData) -> str:
    rows = [p for p in d.policies if "Cookie policy" in p.kinds]
    x1 = [p for p in d.parameters if p.id == "X1"]
    return ("<p>Pages and documents whose address names a cookie policy are identified below by address pattern.</p>"
            + (f'<p>{k("cookie_policies", len(rows))} cookie policy page(s) or document(s) identified.</p>' + _policy_table(rows) if rows else
               "<p><b>No page or document whose address names a cookie policy was found among the stored evidence.</b></p>")
            + f'<p>The scan observed {k("cookies_observed", len(d.cookies))} distinct cookie(s); the table in section 8 lists each one.</p>'
            + "<h3>Cookies observed against cookies declared</h3>" + _param_table(x1))


def _s7(d: ReportData) -> str:
    pc = d.pre_consent
    cparams = [p for p in d.parameters if p.group == "consent"]
    blocks = []
    for t in d.consent:
        if t.tested is True:
            status = "carried out"
        else:
            status = "not carried out: " + REASON_LABEL.get(t.reason or "", t.reason or "no reason recorded")
        body = kv([(a, c(b)) for a, b in t.facts]) if t.facts else ""
        blocks.append(f'<h3>{esc(CONSENT_LABEL.get(t.name, t.name))}</h3><p>Test {esc(status)}.</p>{body}')
    return (
        "<h3>Before any choice</h3>"
        f'<p>Every crawled page was loaded in a session that had given no consent. Of {k("pre_pages", pc.pages_observed)} pages whose requests were recorded, {k("pre_with", pc.pages_with_non_essential)} contacted analytics or advertising services before any choice.'
        + (f' Services named: {c(", ".join(pc.non_essential))}.' if pc.non_essential else "") + "</p>"
        + "<h3>Consent tests</h3><p>These tests ran in separate fresh sessions on the entry page and up to two further pages. A test that could not be carried out is reported as not carried out, never as a failure of the site.</p>"
        + "".join(blocks) + "<h3>Consent parameters</h3>" + _param_table(cparams))


def _s8(d: ReportData) -> str:
    ck = [[c(x.name), f'<span class="mono">{esc(x.domain)}</span><br><span class="mono">{esc(x.path)}</span>', c("first party" if x.first_party else ("third party" if x.first_party is False else None)),
           esc(", ".join(f for f, on in (("Secure", x.secure), ("HttpOnly", x.http_only)) if on) or "no Secure or HttpOnly") + f"<br>SameSite: {c(x.same_site)}",
           ("session" if x.session else c(x.expires)) if x.session is not None else esc(NOT_RECORDED), pages_cell(x.pages), esc(", ".join(s.replace("_", " ") for s in x.consent_states) or NOT_RECORDED)] for x in d.cookies]
    hosts = [h for h in d.hosts if h.third_party]
    tp = [[f'<span class="mono">{esc(h.host)}</span>', c(h.requests), pages_cell(h.pages, 3), esc(", ".join(s.replace("_", " ") for s in h.consent_states) or NOT_RECORDED),
           c("; ".join(h.classified)) if h.classified else "not classified as analytics or advertising"] for h in hosts]
    return (f'<h3>Cookies</h3><p>{k("cookies_inventory", len(d.cookies))} distinct cookie(s), identified by name, domain and path. A cookie seen on several pages is one row listing every page and every consent state it was seen in. Cookie values are never kept.</p>'
            + (table(["Cookie", "Domain and path", "Party", "Flags", "Lifetime", "Seen on", "Consent state"], ck, widths=[13, 19, 8, 14, 9, 25, 12]) if ck else "<p>No cookie was observed.</p>")
            + f'<h3>Third-party hosts and trackers</h3><p>{k("third_party_inventory", len(hosts))} third-party host(s) were contacted by the browser (by registered domain, not by the host alone).</p>'
            + (table(["Host", "Requests", "Seen on", "Consent state", "Catalogue classification"], tp, widths=[22, 8, 28, 14, 28], num=(1,)) if tp else "<p>No third-party host was contacted.</p>"))


def _s9(d: ReportData) -> str:
    """Identical forms (the same destination, method, fields and consent control) are one row that says how many pages carry it and lists the first few: a newsletter box in every page footer is one
    form on 25 pages, not 25 rows. Nothing is dropped: every page is counted, and a form that differs in any field is its own row."""
    groups: dict[tuple, list] = {}
    for f in d.forms:
        sig = ("" if f.action == f.page_url else f.action, f.method, tuple((x.get("name") or x.get("id") or "", x.get("type") or "", bool(x.get("required"))) for x in f.fields),
               tuple((x.get("label") or x.get("name") or "") for x in f.consent_controls), f.privacy_link)
        groups.setdefault(sig, []).append(f)
    rows = []
    for members in sorted(groups.values(), key=lambda m: (-len(m), m[0].page_url)):
        f = members[0]
        pages = sorted({m.page_url for m in members})
        fields = "<br>".join(f'<span class="mono">{esc(x.get("name") or x.get("id") or x.get("type") or "unnamed")}</span> ({esc(x.get("type") or NOT_RECORDED)}{", required" if x.get("required") else ""})' for x in f.fields) or esc(NOT_RECORDED)
        cc = "; ".join((x.get("label") or x.get("name") or "checkbox") for x in f.consent_controls)
        rows.append([pages_cell(pages, 3) + (f"<br>{len(members)} form(s) on {len(pages)} page(s)" if len(members) > 1 or len(pages) > 1 else ""),
                     (f'<span class="mono">{short(f.action, 60)}</span>' if f.action != f.page_url else "the page it is on") + f'<br>{c(f.method.upper())}', fields, c(", ".join(f.collects)) if f.collects else "no recognised personal data type",
                     (f"{len(f.consent_controls)} checkbox(es): {esc(cc)}" if f.consent_controls else "no consent checkbox"), c(f.privacy_link)])
    return (f'<p>{k("forms_inventory", len(d.forms))} form(s) found, in {len(rows)} distinct kind(s). The browser never typed into a field or submitted a form; what each form collects is read from its fields. '
            f'Hidden fields are counted but not listed. Identical forms are shown once with the pages that carry them.</p>'
            + (table(["Pages", "Sends to", "Fields", "Collects", "Consent control", "Privacy link in form"], rows, widths=[17, 17, 25, 14, 17, 10]) if rows else "<p>No form was found on the pages that were crawled.</p>"))


def _s10(d: ReportData) -> str:
    sc = [h for h in d.hosts if h.script_requests]
    rows = [[f'<span class="mono">{esc(h.host)}</span>', c("third party" if h.third_party else "first party"), c(h.script_requests), pages_cell(h.pages, 3), c("; ".join(h.classified)) if h.classified else esc("not classified as analytics or advertising")] for h in sc]
    fr = [[url_cell(i.src), c(i.title) if i.title else esc("no title"), c("third party" if i.third_party else "first party"), url_cell(i.page_url)] for i in d.iframes]
    return ("<p>Scripts are listed by the host they were requested from, as recorded in the browser's request log; the scripts' own content is not analysed here.</p>"
            + (table(["Script host", "Party", "Script requests", "Seen on", "Classification"], rows, widths=[24, 10, 10, 30, 26], num=(2,)) if rows else "<p>No script request was recorded.</p>")
            + "<h3>Embedded frames</h3>" + (table(["Frame source", "Title", "Party", "On page"], fr, widths=[38, 20, 10, 32]) if fr else "<p>No embedded frame was recorded.</p>"))


def _s11(d: ReportData) -> str:
    s = d.security
    tls = s.tls
    if tls:
        tls_html = kv([("Reachable", c(tls.get("reachable"))), ("Certificate verified", c(tls.get("verified"))), ("Verification error", c(tls.get("verify_error")) if tls.get("verify_error") else "none recorded"),
                       ("Negotiated protocol", c(tls.get("negotiated"))), ("TLS 1.3 offered", c(tls.get("tls13_supported"))), ("Issuer", c(tls.get("issuer"))), ("Subject", c(tls.get("subject"))),
                       ("Valid from", c(tls.get("not_before"))), ("Valid until", c(tls.get("not_after"))), ("Probe error", c(tls.get("error")) if tls.get("error") else "none recorded")])
    else:
        tls_html = "<p>No TLS probe was recorded for this scan (the entry address was not HTTPS, or the probe could not be kept).</p>"
    hdr = [[f'<span class="mono">{esc(h)}</span>', k(f"hdr.{h}", s.header_counts.get(h)), esc(f"of {s.pages_total} pages")] for h in s.headers_checked]
    pg = [[url_cell(p.page_url), c(p.https), c(len(p.present)) + f" of {len(s.headers_checked)}", c(", ".join(p.missing)) if p.missing else "none missing", c(p.redirects)] for p in s.pages]
    return (f"<h3>TLS and certificate</h3>{tls_html}<p class=\"small muted\">TLS 1.0 and 1.1 were not probed: a current client does not offer them. This report therefore cannot state whether the server still accepts them.</p>"
            f'<h3>Transport and headers across pages</h3><p>{k("sec_pages_total", s.pages_total)} page(s) had a response record; {k("sec_pages_https", s.pages_https)} were served over HTTPS. Clear-text (http://) requests made by pages: '
            f'{k("sec_insecure", s.insecure_requests)}.' + (f' Examples: {c(", ".join(s.insecure_examples))}.' if s.insecure_examples else "") + "</p>"
            + (table(["Protective header", "Pages carrying it", "Of"], hdr, widths=[50, 25, 25], num=(1,)) if s.pages_total else "")
            + ("<h3>Pages</h3>" + (f'<p class="cap">Showing {len(s.pages)} of {s.pages_total} pages.</p>' if s.pages_total > len(s.pages) else "")
               + table(["Page", "HTTPS", "Headers present", "Missing", "Redirects"], pg, widths=[38, 8, 14, 32, 8], num=(4,)) if pg else ""))


def _s12(d: ReportData) -> str:
    doc = d.declaration.get("documents") or {}
    rows = [[url_cell(x.url), c(x.content_type), c(x.status), c(x.extraction_status) if x.extraction_status else esc(NOT_RECORDED), c(x.extraction_method), c(x.pages_read), c(x.analysis_status), c(x.status_reason)] for x in d.documents]
    return (f'<p>Documents discovered: {k("docs_discovered", doc.get("discovered"))}; extracted: {k("docs_extracted", doc.get("extracted"))}; partly extracted: {k("docs_partly", doc.get("partly_extracted"))}; '
            f'unreadable: {k("docs_unreadable", doc.get("unreadable"))}; extraction not recorded: {k("docs_unrecorded", doc.get("extraction_not_recorded"))}.</p>'
            "<p>Pages of a PDF that held no extractable text (scanned images) were not read: OCR is not enabled, so such a page is reported here and is not assumed to be empty.</p>"
            + (table(["Document", "Type", "Crawl", "Extraction", "Method", "Pages read", "Analysis", "Note"], rows, widths=[26, 10, 8, 11, 10, 7, 9, 19], num=(5,)) if rows else "<p>No linked document was found.</p>"))


def _s13(d: ReportData) -> str:
    lg = d.legal
    src = [[f"<b>{esc(x.id)}</b> {c(x.title)}", c(x.published), f'<span class="mono">{c(x.gazette_identifier)}</span>', f'<span class="mono">{c(x.sha256)}</span>'] for x in lg.sources]
    com = [[c(a), c(b)] for a, b in lg.commencement]
    return (
        f'<div class="notice warn"><b>When the duties apply.</b> Most duties under the Act and the Rules come into force on <b>{esc(lg.main_date)}</b>, eighteen months after 13 November 2025 (G.S.R. 843(E); Rule 1(4)). '
        f'Until then this report measures readiness for those duties, not a breach of them. Section 6(9) and Rule 4 (Consent Managers) come into force on {esc(lg.consent_manager_date)}.</div>'
        "<h3>Dates on which the Act's provisions come into force</h3>" + table(["Date", "Provisions"], com, widths=[16, 84])
        + "<h3>Legal sources</h3>" + table(["Source", "Published", "Gazette identifier", "SHA-256 of the copy read"], src, widths=[34, 24, 20, 22])
        + f'<p class="small">Register verified on {c(lg.verified_on)}; register status: {c(lg.register_status)}. {c(lg.how_verified)}</p>'
        f'<p><b>Legal review status:</b> {c(lg.legal_review_status)}. The legal readings of which provision each parameter rests on are a draft until a named person signs the registry off.</p>')


def _s14(d: ReportData) -> str:
    total_pts = sum(p.points for p in d.parameters)
    rows = []
    for p in d.parameters:
        ev = ", ".join(short_ref(r) for r in p.evidence_refs[:3]) + (f" and {len(p.evidence_refs) - 3} more" if len(p.evidence_refs) > 3 else "")
        rows.append([f'<b>{esc(p.id)}</b>', c(p.name) + (f'<br><span class="muted">{esc(", ".join(p.legal_refs))}</span>' if p.legal_refs else ""), chip(p.state), c(p.points),
                     c(p.earned) if p.earned is not None else esc(NOT_RECORDED), c(p.in_play) if p.in_play is not None else esc(NOT_RECORDED),
                     c(p.note) + (f'<br><span class="muted">{esc(p.adjustment)}</span>' if p.adjustment else "") + (f'<br><span class="muted">Reviewed by {esc(p.reviewed_by)}: {c(p.review_reason)}</span>' if p.reviewed_by else ""),
                     f'<span class="mono">{esc(ev)}</span>' if p.evidence_refs else esc("none")])
    counts = _states(d.parameters)
    return (f'<p>All {k("params_listed", len(d.parameters))} parameters of the catalogue, with their points available (total {k("points_total", total_pts)}). '
            + " ".join(f"{STATE_LABEL[s]}: {k('state.' + s, counts.get(s, 0))}." for s in STATE_LABEL) + '</p>'
            + table(["ID", "Parameter and legal basis", "State", "Pts", "Earned", "In play", "Note", "Evidence"], rows, widths=[5, 27, 10, 5, 7, 7, 27, 12], num=(3, 4, 5)))


def _s15(d: ReportData) -> str:
    sc = d.score
    if sc is None:
        return "<p><b>No score has been calculated for this scan yet.</b> The score is calculated after the parameters have been assessed.</p>"
    head: str
    if sc.score is not None:
        head = (f'<div class="tile"><div><div class="lab">{esc(_score_label(d))} (0 to 100)</div><div class="big">{k("score_detail", sc.score)}</div></div>'
                f'<div><div class="lab">Coverage</div><div class="big">{k("coverage_detail", sc.coverage)}%</div></div>'
                f'<div><div class="lab">Points earned of in play</div><div class="big">{k("earned", sc.points_earned)} / {k("in_play", sc.points_in_play)}</div></div></div>')
    else:
        head = f'<div class="notice warn"><b>No score is shown.</b> {c(sc.reason_no_score)}.</div>'
    if sc.basis == "readiness":
        basis = (f'<p>Basis: <b>readiness</b>. The score is a measure of readiness for duties that are not yet in force, never a finding of breach and never a certification. '
                 + (f'Parameters in the calculation whose provision is not yet in force on {c(sc.as_of)}: {c(", ".join(sc.not_yet_in_force))}.' if sc.not_yet_in_force else "") + "</p>")
    else:
        basis = f'<p>Basis: <b>assessment</b>. Every parameter in the calculation was in force on {c(sc.as_of)}.</p>'
    groups = [[c(g.get("label") or key), c(g.get("points_available")), c(g.get("points_in_play")), c(g.get("points_earned"))] for key, g in sc.by_group.items()]
    na = sc.needs_attention
    return (head + basis
            + kv([("Score name and version", c(sc.version)), ("Calculated", c(sc.calculated_at)), ("Position of the law taken", f"as of {c(sc.as_of)}"), ("Legal review status", c(d.legal_review_status))])
            + "<h3>Formula</h3><p>Score = 100 x points earned / points in play, rounded half up. Coverage = points in play / 100. Met earns a parameter's points, partly met half, not met none; "
              "parameters not assessed, not applicable, needing records or awaiting review are outside the calculation. Supporting technical signals count for at most 10% of the points in play."
              + (f' Their points were scaled to {k("supporting_scale", sc.supporting_scale)} of face value.' if sc.supporting_scale is not None else "") + "</p>"
            + "<h3>Breakdown by group</h3>" + table(["Group", "Points available", "Points in play", "Points earned"], groups, widths=[46, 18, 18, 18], num=(1, 2, 3))
            + "<h3>What needs attention</h3>" + kv([(STATE_LABEL[s], c(", ".join(na.get(s, [])) or "no parameter")) for s in ("not_met", "partly_met", "not_assessed", "human_review", "needs_records")]))


def _s16(d: ReportData) -> str:
    if not d.findings:
        return "<p>No finding was raised for this scan.</p>"
    out = [("<p>Findings are ordered by severity. Where a finding rests on a statutory duty, the Schedule to the Act sets the most the Data Protection Board may impose for that kind of breach. That figure is "
            "<b>a ceiling set by law, not a prediction</b> of any penalty, and it is kept apart from the score. For website-observable readiness it states the legal stakes of the duty, nothing more.</p>")]
    for f in d.findings:
        ceiling = (f'<p class="small"><b>Penalty ceiling set by the Schedule:</b> Rs {k(f"ceiling.{f.finding_key}", f.penalty_ceiling_cr)} crore (Schedule item {c(f.schedule_item)}). A ceiling set by law, not a prediction.</p>'
                   if f.penalty_ceiling_cr is not None else '<p class="small muted">No Schedule ceiling applies: this is not a statutory duty.</p>')
        urls = ""
        if f.affected_urls:
            urls = (f'<p class="small"><b>Raised on {k(f"affected.{f.finding_key}", f.affected_count)} URL(s)</b>' + (f", first {len(f.affected_urls)} shown" if f.affected_count > len(f.affected_urls) else "") + ":</p>"
                    '<ul class="urls">' + "".join(f'<li class="mono">{esc(u)}</li>' for u in f.affected_urls) + "</ul>")
        elif f.affected_count:
            urls = f'<p class="small">Raised on {k(f"affected.{f.finding_key}", f.affected_count)} URL(s); the addresses were not recorded.</p>'
        out.append(
            f'<div class="finding sev-{esc(f.severity)}"><h3>{c(f.title)}</h3>'
            f'<div class="meta"><span class="chip sev-{esc(f.severity)}">{esc(SEV_LABEL.get(f.severity, f.severity))}</span> &nbsp; Parameter {c(f.parameter_id)} &nbsp; {chip(f.state)} &nbsp; '
            f'Legal basis: {c(", ".join(f.legal_refs)) if f.legal_refs else "no statutory provision (supporting signal)"} &nbsp; Confidence: {c(f.confidence)} &nbsp; Verification: {c(f.verification_status)}'
            + (' &nbsp; <b>Needs human review</b>' if f.human_review else "") + "</div>"
            + (f"<p>{c(f.description)}</p>" if f.description else "") + (f"<p><b>Expected:</b> {c(f.expected)}</p>" if f.expected else "") + (f"<p><b>Observed:</b> {c(f.observed)}</p>" if f.observed else "")
            + (f"<p><b>Why this result:</b> {c(f.reason)}</p>" if f.reason else "") + urls + ceiling
            + (f'<p class="small muted">Evidence: <span class="mono">{esc(", ".join(short_ref(r) for r in f.evidence_refs[:6]))}</span>' + (f" and {len(f.evidence_refs) - 6} more" if len(f.evidence_refs) > 6 else "") + "</p>" if f.evidence_refs else "")
            + "</div>")
    if d.exposure:
        out.append("<h3>Schedule ceilings for duties not fully met</h3>" + table(["Parameter", "State", "Schedule item", "Ceiling (Rs crore)", "Provision"],
                   [[c(e.parameter_id), chip(e.state), c(e.schedule_item), k(f"exposure.{e.parameter_id}", e.maximum_penalty_crore), c(", ".join(e.legal_refs))] for e in d.exposure], widths=[14, 16, 16, 18, 36], num=(2, 3))
                   + '<p class="small muted">Each figure is the highest amount the Schedule allows for that kind of breach. It is a ceiling set by law and is not a prediction. Duties not yet in force cannot be breached.</p>')
    return "".join(out)


def _s17(d: ReportData) -> str:
    rows = [[f'<span class="mono">{esc(e.ref)}</span>', c(e.kind), url_cell(e.source_url), c(e.page_number if e.page_number else None) if e.kind == "document_text" else esc("not applicable"),
             f'<span class="mono">{c(e.content_hash)}</span>', c(e.captured_at), c(", ".join(e.cited_by[:4])) + (f" and {len(e.cited_by) - 4} more" if len(e.cited_by) > 4 else "")] for e in d.evidence]
    return (f'<p>{k("evidence_total", d.evidence_total)} evidence reference(s) are cited by parameter results and findings. Each can be opened in the application by its identifier, which shows the stored content and its fingerprint.</p>'
            + (f'<p class="cap">Showing {len(d.evidence)} of {d.evidence_total} references.</p>' if d.evidence_total > len(d.evidence) else "")
            + (table(["Reference", "Kind", "Source", "Page", "Content hash (SHA-256)", "Captured", "Cited by"], rows, widths=[15, 7, 20, 6, 24, 10, 18], cls="compact") if rows else "<p>No evidence reference is cited.</p>"))


def _s18(d: ReportData) -> str:
    seen, rows = set(), []
    for f in d.findings:
        if not f.recommendation or (f.parameter_id, f.recommendation) in seen:
            continue
        seen.add((f.parameter_id, f.recommendation))
        rows.append([f'<span class="chip sev-{esc(f.severity)}">{esc(SEV_LABEL.get(f.severity, f.severity))}</span>', c(f.parameter_id), c(f.recommendation)])
    notassessed = [p for p in d.parameters if p.state == "not_assessed"]
    return ("<p>Recommendations are those recorded with each finding, ordered by severity. They are suggestions for the organisation to weigh; they are not legal advice.</p>"
            + (table(["Priority", "Parameter", "Recommendation"], rows, widths=[12, 12, 76]) if rows else "<p>No recommendation was recorded: no finding with a recommendation was raised.</p>")
            + (f'<p>{k("not_assessed_count", len(notassessed))} parameter(s) could not be assessed from the website: ' + c(", ".join(p.id for p in notassessed)) + ". Providing the missing records, or a person's review, would let them count.</p>" if notassessed else ""))


def _s19(d: ReportData) -> str:
    hr = [p for p in d.parameters if p.state in ("human_review", "needs_records")]
    flagged = [f for f in d.findings if f.human_review]
    confirm = [p for p in d.parameters if p.human_review_required and p.state in ("met", "partly_met", "not_met")]
    return (
        f'<p>{k("hr_parameters", len(hr))} parameter(s) await a person or records; {k("hr_findings", len(flagged))} finding(s) are flagged for human review. These are never shown as passed or failed.</p>'
        + ("<h3>Parameters awaiting a person or records</h3>" + table(["Parameter", "State", "Why"], [[f"<b>{esc(p.id)}</b> {c(p.name)}", chip(p.state), c(p.note) if p.note else esc("the parameter is outside what a website can show")] for p in hr], widths=[40, 14, 46]) if hr else "")
        + ("<h3>Findings flagged for human review</h3>" + table(["Finding", "Parameter", "Confidence"], [[c(f.title), c(f.parameter_id), c(f.confidence)] for f in flagged], widths=[70, 14, 16]) if flagged else "")
        + ("<h3>Readings a person should confirm</h3><p>These parameters have a result, but their reading calls for a person's confirmation before it is relied on.</p>"
           + table(["Parameter", "Current state"], [[f"<b>{esc(p.id)}</b> {c(p.name)}", chip(p.state)] for p in confirm], widths=[75, 25]) if confirm else ""))


def _s20(d: ReportData) -> str:
    items = [
        f"<b>Legal review status: {esc(d.legal_review_status)}.</b> The legal readings behind the parameters, and the registry of sources, have not been signed off by a named legal reviewer.",
        "Website evidence cannot certify compliance. A website shows notices, banners, cookies and requests; it does not show how data is stored, shared, secured, retained or erased inside the organisation.",
        "Consent tests ran on the entry page and up to two further pages only. Behaviour on other pages, other devices, other locations or after logging in was not tested.",
        "Observations are made from requests and cookies recorded by an automated browser. A service that does not send a request, or loads only after interaction the scanner does not perform, is not seen.",
        "TLS 1.0 and 1.1 were not probed, so the report does not state whether the server accepts them.",
        "Cookie values are never kept, and no form is submitted: what a form does with data after submission cannot be observed.",
        "Policy-based parameters rest on the text that could be read. Pages of documents with no extractable text (scanned images) were not read because OCR is not enabled.",
        "Cookie and tracker classification comes from public catalogues; a cookie or host that is not in them is reported as not classified, not as safe.",
        "Penalty ceilings are the limits the Schedule sets and are not predictions of any penalty.",
    ]
    decl = d.declaration
    u = decl.get("urls") or {}
    dyn = []
    if d.completion != "complete":
        dyn.append(f"This scan is <b>{c(d.completion)}</b>, not complete: crawl {c(decl.get('crawl_status'))}, analysis {c(decl.get('analysis_status'))}.")
    for key, label in (("pending", "URLs still pending"), ("failed", "URLs failed"), ("inaccessible", "URLs inaccessible (HTTP 401 or 403)"), ("excluded", "URLs excluded"), ("awaiting_analysis", "crawled pages awaiting analysis"),
                       ("analysis_failed", "pages whose analysis failed")):
        if u.get(key):
            dyn.append(f"{k('lim.' + key, u[key])} {esc(label)}.")
    if (decl.get("analysis_tasks") or {}).get("failed"):
        dyn.append(f"{k('lim.tasks_failed', decl['analysis_tasks']['failed'])} analysis task(s) failed.")
    if d.urls.total and d.urls.total > len(d.urls.shown):
        dyn.append(f"The URL list in this report is capped at {len(d.urls.shown)} of {d.urls.total} URLs.")
    if d.unreadable_observations:
        dyn.append(f"{k('lim.unreadable', d.unreadable_observations)} stored observation(s) could not be read and are not reflected in this report.")
    for t in d.consent:
        if t.tested is not True:
            dyn.append(f"Consent test \"{esc(CONSENT_LABEL.get(t.name, t.name))}\" was not carried out: {esc(REASON_LABEL.get(t.reason or '', t.reason or 'no reason recorded'))}.")
    if d.score is not None and d.score.score is None:
        dyn.append("No score is shown because coverage is below the minimum.")
    na = [p.id for p in d.parameters if p.state == "not_assessed"]
    if na:
        dyn.append(f"Parameters not assessed: {c(', '.join(na))}.")
    return ("<h3>What this report cannot say</h3><ul>" + "".join(f"<li>{i}</li>" for i in items) + "</ul>"
            + ("<h3>Specific to this scan</h3><ul>" + "".join(f"<li>{i}</li>" for i in dyn) + "</ul>" if dyn else "<h3>Specific to this scan</h3><p>The scan completed with nothing outstanding.</p>"))


_BUILDERS = (_s1, _s2, _s3, _s4, _s5, _s6, _s7, _s8, _s9, _s10, _s11, _s12, _s13, _s14, _s15, _s16, _s17, _s18, _s19, _s20)


def render_html(d: ReportData) -> str:
    """The complete report as one HTML document. It makes no request and needs no script."""
    s = d.scan
    host = urlparse(s.website_url).netloc or s.domain
    cover = (f'<div class="cover"><div class="small muted">CONSIVA ENTERPRISE</div><h1>DPDP readiness report</h1><p class="lead">{esc(host)}</p>'
             f'<div class="meta">Scan <span class="mono">{esc(s.scan_id)}</span> &nbsp;|&nbsp; Generated <b>{c(d.generated_at)}</b> &nbsp;|&nbsp; Scoring method <b>{c(d.score.version if d.score else None)}</b></div></div>')
    toc = ('<h3>Contents</h3><ol class="toc">' + "".join(f'<li><a href="#s{i}">{i}. {esc(t)}</a></li>' for i, t in enumerate(SECTIONS, 1)) + "</ol>")
    body = "".join(_section(i, b(d)) for i, b in enumerate(_BUILDERS, 1))
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>DPDP readiness report: {esc(host)}</title>'
            f'<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="consiva-report-scan" content="{esc(s.scan_id)}"><meta name="consiva-report-site" content="{esc(host)}">'
            f'<style>{CSS}</style></head><body>{cover}{toc}{body}</body></html>')
