"""The Enterprise report for method 3.0.0 (approved by the owner on 20 November 2026 from the swaransoft.com and consiva.ai demonstration reports).

    data = await gather(db, scan_id)          # the stored method 3.0.0 result plus the scan's own evidence, read once
    html = render_html(data)                  # one self-contained, print-ready HTML document (inline CSS, no script, no external request)

Layout: page 1 executive summary; how the score is calculated and the scan's transparency; the complete parameter-wise marking table (all 96 parameters, never shortened);
the URL-wise assessment of every inspected page and every URL found with what became of it; the privacy policy analysis; consent and withdrawal; cookies and trackers; forms;
third-party services with the request URLs behind them; technical security; conditional obligations; evidence limits and human review; prioritised corrective actions;
the final summary; Appendix A (every parameter's requirement, law, marks, evidence, method and criteria); Appendix B (where the result differs from the 51-parameter method).

Every value that comes from a website is HTML-escaped. Nothing of the 5-page agent's report is used or changed.
"""

from __future__ import annotations

import json
import re
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from html import escape as e
from urllib.parse import urlparse

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.enterprise.method3 import catalogue as cat

IST = timezone(timedelta(hours=5, minutes=30))
URLP = ["N02", "C01", "C12", "N13", "C02", "C03", "X03", "G02"]
STATUS_LABEL = {"pass": "Pass", "partial": "Partial", "fail": "Fail", "not_applicable": "Not applicable", "information": "Information"}
CHIP = {"pass": "ok", "partial": "part", "fail": "bad", "not_applicable": "na", "information": "na"}
CONSENT_STATES = {"pre_consent": "before a choice", "post_accept": "after Accept", "post_reject": "after Reject"}


@dataclass
class M3ReportData:
    scan_id: str
    org_id: str
    site: str
    scan: dict
    result: dict
    result_id: str
    frontier: list[dict] = field(default_factory=list)
    pages: list[dict] = field(default_factory=list)                 # per inspected page: url, category, forms count
    cookies: list[dict] = field(default_factory=list)               # one row per (name, domain): states seen, pages, flags
    hosts: list[dict] = field(default_factory=list)                 # one row per third-party host: pages, requests, vendor, category, example URLs
    forms: list[dict] = field(default_factory=list)                 # grouped by identical design
    consent: dict = field(default_factory=dict)
    documents: list[dict] = field(default_factory=list)
    tls: dict = field(default_factory=dict)
    sitemap: dict = field(default_factory=dict)                     # the crawl's `sitemap_read` audit: how many URLs the sitemap gave and whether the reader's cap was reached
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# ---- gathering -----------------------------------------------------------------------------------------------------------------------------------------------------------

async def gather(db: AsyncSession, scan_id: uuid.UUID) -> M3ReportData:
    from app.enterprise.method3 import service as method3
    from app.rules.cookie_purposes import describe_cookie         # the 5-page agent's cookie catalogue, used by import only

    row = await method3.latest(db, scan_id)
    if row is None:
        raise LookupError("method 3.0.0 has not been calculated for this scan")
    scan = (await db.execute(text("select id, org_id, website_url, status, stop_reason, started_at, completed_at, analysis_started_at, analysis_completed_at, options "
                                  "from ent_scans where id = :s"), {"s": scan_id})).mappings().one()
    frontier = [dict(r) for r in (await db.execute(text(
        "select original_url, final_url, status, depth, discovery_source, status_reason, http_status, page_category, page_title from ent_frontier where scan_id = :s "
        "order by case status when 'done' then 0 when 'failed' then 1 when 'excluded' then 2 else 3 end, depth, original_url"), {"s": scan_id})).mappings().all()]
    ev = (await db.execute(text("select id, kind, source_url, detection_method, content from ent_evidence where scan_id = :s and kind in ('cookie', 'network', 'form', 'consent', 'header') "
                                "order by captured_at, id"), {"s": scan_id})).mappings().all()
    docs = (await db.execute(text("select source_url, count(*) n from ent_evidence where scan_id = :s and kind = 'document_text' group by source_url order by source_url"),
                             {"s": scan_id})).mappings().all()
    d = M3ReportData(str(scan_id), str(scan["org_id"]), scan["website_url"], dict(scan), row.result, str(row.id), frontier, documents=[dict(x) for x in docs])
    sm = await db.scalar(text("select detail from ent_audit_events where scan_id = :s and event = 'sitemap_read' order by created_at desc limit 1"), {"s": scan_id})
    d.sitemap = sm if isinstance(sm, dict) else {}

    def js(c):
        try:
            v = json.loads(c)
            return v if isinstance(v, dict) else {}
        except (ValueError, TypeError):
            return {}

    cookies: dict[tuple, dict] = {}
    hosts: dict[str, dict] = {}
    vendor_of: dict[str, tuple[str, str]] = {}
    forms_by_page: dict[str, list] = {}
    for r in ev:
        c = js(r["content"])
        if r["kind"] == "cookie":
            for k in c.get("cookies") or []:
                x = cookies.setdefault((k.get("name"), k.get("domain")), {**k, "states": set(), "pages": set()})
                x["states"].add(k.get("consent_state") or "pre_consent")
                x["pages"].add(c.get("page_url"))
        elif r["kind"] == "network":
            for t in c.get("non_essential") or []:
                vendor_of.setdefault(t.get("host"), (t.get("vendor") or t.get("host"), t.get("category") or ""))
            examples = c.get("examples") or {}
            for h in c.get("hosts") or []:
                if not h.get("third_party") or "." not in (h.get("host") or ""):
                    continue                 # scans stored before page_inspect skipped non-web addresses: a chrome-extension id has no dot and is not a host
                x = hosts.setdefault(h["host"], {"host": h["host"], "pages": 0, "requests": 0, "types": Counter(), "examples": [], "states": {"before a choice"}})
                x["pages"] += 1
                x["requests"] += h.get("requests", 0)
                x["types"].update(h.get("types") or {})
                for u in (examples.get(h["host"]) or [])[:3]:
                    if u not in x["examples"] and len(x["examples"]) < 3:
                        x["examples"].append(u)
            for u in c.get("script_urls") or []:
                hh = (urlparse(u).hostname or "").lower()
                if hh in hosts and u not in hosts[hh]["examples"] and len(hosts[hh]["examples"]) < 3:
                    hosts[hh]["examples"].append(u)
        elif r["kind"] == "form":
            forms_by_page[c.get("page_url") or r["source_url"]] = c.get("forms") or []
        elif r["kind"] == "consent":
            d.consent[r["detection_method"].removeprefix("consent_test:")] = c
        elif r["kind"] == "header" and r["detection_method"] == "tls_probe":
            d.tls = c
    for state, test in (("post_accept", "accept"), ("post_reject", "reject")):
        for k in (d.consent.get(test) or {}).get("cookies") or []:
            x = cookies.setdefault((k.get("name"), k.get("domain")), {**k, "states": set(), "pages": set()})
            x["states"].add(state)
        for t in (d.consent.get(test) or {}).get("non_essential") or []:
            vendor_of.setdefault(t.get("host"), (t.get("vendor") or t.get("host"), t.get("category") or ""))
            x = hosts.setdefault(t["host"], {"host": t["host"], "pages": 0, "requests": 0, "types": Counter(), "examples": [], "states": set()})
            x["states"].add(CONSENT_STATES[state])
    for (name, domain), x in cookies.items():
        desc = describe_cookie(name or "")
        x.update(provider=desc.get("provider") or "", purpose=desc.get("purpose") or "")
    d.cookies = sorted(cookies.values(), key=lambda x: (min(("pre_consent" in x["states"]) and 0 or 1, 1), x.get("name") or ""))
    for h, x in hosts.items():
        v = vendor_of.get(h)
        x["vendor"], x["category"] = (v if v else _classify_host(h, d.site))
    d.hosts = sorted(hosts.values(), key=lambda x: (-x["pages"], x["host"]))
    groups: dict[tuple, dict] = {}
    for url, fs in forms_by_page.items():
        for f in fs:
            if not f.get("collects") or (set(f.get("collects") or []) <= {"address"} and re.search(r"website|url|domain", (f.get("label_text") or "") + (f.get("text") or ""), re.I)):
                continue
            sig = (tuple(sorted(f["collects"])), (f.get("text") or "").split("\n")[-1][:40].strip(), bool(f.get("consent_controls")), bool(f.get("privacy_link")),
                   (f.get("method") or "get").upper(), bool(f.get("preticked")))
            g = groups.setdefault(sig, {"collects": sig[0], "label": sig[1], "consent": sig[2], "notice": sig[3], "method": sig[4], "preticked": sig[5], "pages": []})
            g["pages"].append(url)
    d.forms = sorted(groups.values(), key=lambda g: -len(g["pages"]))
    cats = {r.get("final_url") or r["original_url"]: r.get("page_category") for r in frontier}
    d.pages = [{"url": u, "category": cats.get(u) or cats.get(u.rstrip("/")) or cats.get(u + "/") or "",
                "forms": sum(1 for f in forms_by_page.get(u, []) if f.get("collects"))} for u in sorted({l["url"] for line in row.result["lines"] for l in line["urls"]} or
                                                                                                     {r["original_url"] for r in frontier if r["status"] == "done"})]
    return d


_HOST_CLASSES = ((r"googletagmanager", "Google Tag Manager", "analytics"), (r"google-analytics|analytics\.google", "Google Analytics", "analytics"),
                 (r"doubleclick|googlesyndication|googleadservices", "Google Ads", "advertising"), (r"clarity\.ms", "Microsoft Clarity", "analytics"),
                 (r"bing\.com", "Microsoft Bing", "advertising"), (r"facebook|fbcdn", "Meta", "advertising"), (r"linkedin", "LinkedIn", "advertising"),
                 (r"gstatic|googleapis", "Google Fonts / static", "functional"), (r"loom\.com", "Loom video embed", "embedded content"),
                 (r"youtube|ytimg", "YouTube embed", "embedded content"), (r"cloudfront|cdn|static", "Content delivery (CDN)", "functional"))


def _classify_host(host: str, site: str) -> tuple[str, str]:
    for rx, name, kind in _HOST_CLASSES:
        if re.search(rx, host, re.I):
            return name, kind
    return host, "not classified"


# ---- rendering ------------------------------------------------------------------------------------------------------------------------------------------------------------

CSS = """
@page { size: A4; }
* { box-sizing: border-box; }
body { font-family: 'Segoe UI', 'Selawik', Arial, sans-serif; font-size: 8.6pt; color: #1b1d2e; margin: 0; }
h1 { font-size: 22pt; margin: 0 0 4px; } h2 { font-size: 13pt; margin: 14px 0 6px; color: #1b1d2e; display:flex; align-items:center; gap:8px; page-break-after: avoid; }
h2 .n { background:#6d4aff; color:#fff; border-radius:50%; width:22px; height:22px; display:inline-flex; align-items:center; justify-content:center; font-size:10pt; }
h3 { font-size: 10pt; margin: 10px 0 4px; color:#4b2fd6; page-break-after: avoid; }
p { margin: 3px 0 6px; line-height: 1.4; }
.note { border: 1px solid #c7d2fe; background: #eef2ff; color: #1e1b4b; padding: 7px 10px; border-radius: 6px; margin: 6px 0 10px; font-size: 8.4pt; }
.alert { border: 1.5px solid #991b1b; background: #fef2f2; color: #7f1d1d; padding: 7px 10px; border-radius: 6px; margin: 6px 0 10px; font-size: 8.4pt; }
.hero { background: linear-gradient(135deg,#14163a,#2b2a7a); color:#fff; border-radius: 12px; padding: 16px 20px; display:flex; justify-content:space-between; gap: 16px; }
.hero .meta { font-size: 8.6pt; opacity: .9; line-height:1.6 }
.score { background:#fff; color:#1b1d2e; border-radius: 12px; padding: 12px 18px; text-align:center; min-width: 170px; }
.score .big { font-size: 34pt; font-weight: 700; color:#4b2fd6; line-height:1 } .score .sub { font-size: 8pt; color:#555 }
.tiles { display:grid; grid-template-columns: repeat(6, 1fr); gap: 6px; margin: 10px 0; }
.tile { border:1px solid #e3e5f2; border-radius: 8px; padding: 7px 8px; background:#f7f8fe } .tile b { font-size: 14pt; display:block }
table { width: 100%; border-collapse: collapse; margin: 4px 0 8px; page-break-inside: auto; }
thead { display: table-header-group; } tr { page-break-inside: avoid; }
th { background: #eef0fb; text-align:left; font-size: 7.4pt; text-transform: uppercase; letter-spacing: .3px; color:#4a4f6a; padding: 5px 5px; border-bottom: 1px solid #d5d8ea; }
td { padding: 4px 5px; border-bottom: 1px solid #ebedf5; vertical-align: top; line-height: 1.35; }
td.num, th.num { text-align: right; white-space: nowrap; }
.chip { display:inline-block; padding: 1px 7px; border-radius: 9px; font-size: 7.4pt; font-weight: 600; white-space: nowrap; }
table.fixed td .chip { white-space: normal; max-width: 100%; text-align: center; line-height: 1.25; }   /* a narrow Status column: wrap, never overlap the Evidence column */
.ok { background:#dcfce7; color:#166534 } .part { background:#fef3c7; color:#92400e } .bad { background:#fee2e2; color:#991b1b } .na { background:#eceef5; color:#555 }
.ev { color:#3b3f58; font-size: 7.6pt; word-break: break-word; } .ev div { margin-bottom: 2px } .small { font-size: 7.4pt; color:#5b6078 }
.flag { font-size: 7pt; color:#7c2d12; background:#ffedd5; border-radius: 6px; padding: 0 5px; margin-left: 3px; white-space: nowrap }
.cathead td { background:#f3f0ff; font-weight: 700; color:#3b1fb8; border-bottom: 1px solid #d9d0ff }
.bar { height: 8px; background:#eceef5; border-radius: 4px; overflow:hidden; width: 120px; display:inline-block; vertical-align: middle }
.bar i { display:block; height:100%; background:#6d4aff }
.pb { page-break-before: always; } tr.cathead { page-break-after: avoid; break-after: avoid; }
.grid2 { display:grid; grid-template-columns: 1fr 1fr; gap: 10px; }
.box { border:1px solid #e3e5f2; border-radius: 8px; padding: 8px 10px; background:#fbfbff; page-break-inside: avoid; }
.inv { border:1px solid #e3e5f2; border-radius: 8px; padding: 6px 9px; margin-bottom: 6px; page-break-inside: avoid; }
.inv .h { display:flex; justify-content:space-between; font-weight:700 } .inv dl { display:grid; grid-template-columns: 105px 1fr; margin: 3px 0 0; gap: 1px 8px } .inv dt { color:#5b6078 } .inv dd { margin:0 }
.mono { font-family: Consolas, monospace; font-size: 7.2pt; word-break: break-all; }
td.url { word-break: break-all; }
table.fixed { table-layout: fixed; } table.fixed td { overflow-wrap: anywhere; }
.c { text-align:center }
"""


def _ist(v) -> str:
    if not v:
        return "not recorded"
    if isinstance(v, str):
        v = datetime.fromisoformat(v)
    if v.tzinfo is None:
        v = v.replace(tzinfo=timezone.utc)
    return v.astimezone(IST).strftime("%d %b %Y, %H:%M IST")


def fm(x) -> str:
    if x is None:
        return "—"
    x = float(x)
    return f"{x:g}" if abs(x - round(x, 2)) < 1e-9 else f"{x:.3f}".rstrip("0")


def chip(s: str) -> str:
    return f'<span class="chip {CHIP.get(s, "na")}">{e(STATUS_LABEL.get(s, s))}</span>'


APPL_LABEL = {"applicable": "<b>Applicable</b>", "not_applicable": "Not applicable", "human_review": "<span class='flag'>Human review</span>"}
SEV_LABEL = {"high": "<b style='color:#991b1b'>High</b>", "medium": "<b style='color:#92400e'>Medium</b>", "low": "<span style='color:#555'>Low</span>"}
_CAT_LABEL = {"general": "General business", "ecommerce": "E-commerce", "gaming": "Online gaming", "social_media": "Social media", "edtech": "EdTech / education",
              "children": "Children's service"}


def cat_label(c: str) -> str:
    return _CAT_LABEL.get(c, c)


def legal(p) -> str:
    return "<br>".join(e(x) for x in (p.act, p.rule) if x and x != "-")


def _path(u: str, site: str) -> str:
    root = f"{urlparse(site).scheme}://{urlparse(site).netloc}"
    return (u[len(root):] if u.startswith(root) else u) or "/"


#: What to do, for the results that most often fall short. Any other parameter's action is its requirement, stated as a task.
ACTIONS = {
    "C12": ("Load analytics and advertising tags only after the matching category is accepted: block them by default in the consent tool or start them from its accept callback; load embedded players (videos) only after consent.", "Engineering", "Medium"),
    "C13": ("After Reject, make sure no analytics or advertising tag runs and remove the cookies such tags set before the choice.", "Engineering", "Medium"),
    "C14": ("Read the stored refusal on every page load before any tag runs, so later pages do not start tracking again.", "Engineering", "Medium"),
    "C17": ("On withdrawal, stop every tag the consent had started, on the page and on every later page, and clear the cookies they set.", "Engineering", "Medium"),
    "C10": ("Give the contact of the DPO or the person who answers questions (or a link to it) in the banner's preferences layer and beside each form's consent wording.", "Website owner", "Low"),
    "C02": ("Add an unticked consent checkbox, and a link to the notice, to every form that collects personal data and has none.", "Website owner", "Low"),
    "C04": ("Offer a separate choice for each purpose in the banner's preferences layer, reachable by keyboard and script.", "Website owner", "Low"),
    "N02": ("Link the privacy notice from every page (the standard footer), including landing pages.", "Website owner", "Low"),
    "N13": ("Add a link to the privacy notice beside every form that collects personal data.", "Website owner", "Low"),
    "N05": ("Itemise every kind of personal data collected, including each field the forms ask for and the identifiers analytics and advertising tags collect.", "Privacy lead", "Low"),
    "N10": ("Say how to complain to the Data Protection Board: its portal or the official page that publishes its complaint channel.", "Privacy lead", "Low"),
    "N14": ("State how people whose data was collected before the notice duty applies will be notified.", "Privacy lead", "Medium"),
    "F10": ("Describe how each affected person is told of a personal data breach: what happened, likely consequences, measures taken, safety steps, a contact (Rule 7(1)).", "Legal / privacy lead", "Low"),
    "F11": ("Describe the intimation to the Board without delay and the detailed report within 72 hours (Rule 7(2)).", "Legal / privacy lead", "Low"),
    "F02": ("State the organisation's own duty to keep personal data accurate, complete and consistent (s.8(3)), separately from the correction right.", "Privacy lead", "Low"),
    "F09": ("State that processor contracts require reasonable security safeguards (Rule 6(1)(f)).", "Legal", "Low"),
    "RT2": ("State that processors are made to erase the data when it is erased.", "Legal", "Low"),
    "RT3": ("Give a retention period for every kind of data collected, including analytics and newsletter data.", "Privacy lead", "Low"),
    "RT5": ("State the one-year retention of personal data, traffic data and logs under Rule 8(3).", "Privacy lead", "Low"),
    "R02": ("Name the processors and third parties, or state the right to obtain their identities.", "Privacy lead", "Low"),
    "R07": ("State which identifiers a requester must give and how the request is verified.", "Privacy lead", "Low"),
    "X01": ("List every cookie the site sets (name, provider, purpose, lifetime) in the cookie policy.", "Privacy lead", "Low"),
    "X02": ("Name every analytics, advertising and embedded service the pages contact, or remove the ones not intended.", "Privacy lead", "Low"),
    "X03": ("Itemise every kind of data the forms ask for in the notice, or stop asking for it.", "Privacy lead", "Low"),
    "L02": ("Tie each purpose that does not rest on consent to its s.7 legitimate use.", "Legal", "Low"),
    "TS05": ("Send Strict-Transport-Security (max-age of a year or more).", "Engineering", "Low"),
    "TS06": ("Send a Content-Security-Policy.", "Engineering", "Medium"),
    "TS11": ("Mark every cookie Secure (for analytics tags, through their cookie flags setting).", "Engineering", "Low"),
    "TS15": ("Use POST for every form that collects personal data, or confirm the script submission path.", "Engineering", "Low"),
}


#: Page-level DPDP checks shown URL by URL (the demonstration report's columns).
URL_COLS = ["N02", "C01", "C12", "N13", "C02", "C03", "G02"]
#: What each reason a URL was not inspected means, in plain words.
EXCLUDED_MEANING = {
    "excluded_external_domain": "Another website (social networks, partners, tools): outside the exact-host scope.",
    "excluded_subdomain": "Another host of the same organisation (for example www. or app.): outside the exact-host scope.",
    "query_variant": "The same page with tracking or filter parameters.",
    "soft_404": "Answered 200 but the page says it does not exist.",
    "duplicate": "The same page under another address.",
    "robots_txt": "The site's robots.txt asks crawlers not to open it.",
    "non_html": "Not a web page (an image, an archive or another file).",
    "limit": "Beyond the scan's page limit.",
}
_NA_DUTY = {"K": "children and persons with disability", "S": "Significant Data Fiduciary", "M": "Consent Manager", "RT": "Third Schedule", "E": "exemptions"}


def _font_faces() -> str:
    """Selawik (Microsoft's open-licence stand-in for Segoe UI, SIL OFL; LICENSE-Selawik.txt beside the files), embedded so the PDF looks the same on any server."""
    import base64
    from pathlib import Path
    out = []
    for f, weight in (("selawk.ttf", 400), ("selawksb.ttf", 600), ("selawkb.ttf", 700)):
        p = Path(__file__).with_name("fonts") / f
        if p.exists():
            out.append(f"@font-face {{ font-family: 'Selawik'; font-weight: {weight}; src: url(data:font/ttf;base64,{base64.b64encode(p.read_bytes()).decode()}) format('truetype'); }}")
    return "\n".join(out)


_FONTS = _font_faces()


def header_footer(site_host: str, version: str, scan_id: str) -> tuple[str, str]:
    """The running header and footer of the demonstration report (Chromium templates; their text is escaped)."""
    font = "font-family:'Segoe UI',Selawik,Arial,sans-serif;"
    header = (f'<div style="width:100%;{font}font-size:7pt;padding:0 12mm;display:flex;justify-content:space-between;color:#444">'
              f'<span><b style="color:#4b2fd6">Consiva.ai</b> · Enterprise DPDP Compliance Assessment · {e(site_host)}</span>'
              f'<span style="color:#4b2fd6;font-weight:700">Method {e(version)} · Scan {e(scan_id[:8])}</span></div>')
    footer = (f'<div style="width:100%;{font}font-size:6.5pt;padding:0 12mm;display:flex;justify-content:space-between;color:#777">'
              '<span>Automated readiness assessment against the DPDP Act, 2023 and DPDP Rules, 2025. Informational, not legal advice.</span>'
              '<span>Page <span class="pageNumber"></span> of <span class="totalPages"></span></span></div>')
    return header, footer


#: The demonstration report's page margins.
MARGIN = {"top": "18mm", "bottom": "14mm", "left": "12mm", "right": "12mm"}


def _named_list(expl: str, marker: str) -> set[str]:
    """The names a cross-check lists after "not declared:" / "not named:" in its explanation."""
    m = re.search(re.escape(marker) + r"\s*(.+?)\.?$", expl or "")
    if not m:
        return set()
    return {re.sub(r"\s*\(.*?\)$", "", x).strip().lower() for x in re.split(r",\s*(?![^()]*\))", m.group(1)) if x.strip()}


def render_html(d: M3ReportData) -> str:
    r = d.result
    lines = {x["parameter_id"]: x for x in r["lines"]}
    site_host = urlparse(d.site).netloc or d.site
    H: list[str] = []
    w = H.append
    c = r["counts"]
    scored = [x for x in r["lines"] if x["status"] in ("pass", "partial", "fail")]
    pages_inspected = len(d.pages)
    fr_status = Counter(x["status"] for x in d.frontier)
    as_of = datetime.fromisoformat(r["as_of"]).strftime("%d %b %Y") if r.get("as_of") else ""

    # ---- page 1: executive summary
    w(f"""<div class="hero"><div><div style="font-size:9pt;opacity:.8">ENTERPRISE DPDP COMPLIANCE ASSESSMENT · METHOD {e(r['catalogue_version'])}</div>
<h1>{e(site_host)}</h1><div class="meta">Whole-site deep scan, exact host only ({e(d.site)})<br>
Crawl {_ist(d.scan.get('started_at'))} → {_ist(d.scan.get('completed_at'))} · analysis completed {_ist(d.scan.get('analysis_completed_at'))}<br>
Scan run {e(d.scan_id[:8])} · Assessment date {e(as_of)} · {'Readiness view: most duties apply from 13 May 2027' if r['basis'] == 'readiness' else 'Assessment of duties in force'}</div></div>
<div class="score"><div class="big">{fm(r['score'])}</div><div class="sub">/ 100 DPDP compliance score</div>
<div style="margin-top:6px;font-size:8.4pt"><b>{fm(r['marks_earned'])}</b> of <b>{fm(r['applicable_max'])}</b> applicable marks<br><span class="small">{fm(r['not_applicable_max'])} marks not applicable, removed from the
maximum{f" (of them {fm(r.get('human_review_max'))} held back for human review)" if r.get('human_review_max') else ""}</span></div></div></div>
<div class="note"><b>Enterprise DPDP compliance report.</b> Automated assessment of the website's own evidence (pages, cookies, requests, forms, consent tests and published policies) under
method {e(r['catalogue_version'])}: {r['parameters']} parameters, one per independently assessable requirement of the DPDP Act, 2023 and the DPDP Rules, 2025. Legal readings:
{e(r['legal_review_status'])}. A readiness assessment, not a certification of compliance and not legal advice.</div>
<div class="tiles">
<div class="tile"><b>{r['parameters']}</b>parameters in the catalogue</div>
<div class="tile"><b>{len(scored)}</b>assessed and scored</div>
<div class="tile"><b style="color:#166534">{c.get('pass', 0)}</b>Pass</div>
<div class="tile"><b style="color:#92400e">{c.get('partial', 0)}</b>Partial</div>
<div class="tile"><b style="color:#991b1b">{c.get('fail', 0)}</b>Fail</div>
<div class="tile"><b style="color:#555">{c.get('not_applicable', 0)}</b>not applicable (+{c.get('information', 0)} information)</div></div>
<div class="tiles">
<div class="tile"><b>{len(d.frontier)}</b>URLs discovered</div>
<div class="tile"><b>{pages_inspected}</b>web pages inspected</div>
<div class="tile"><b>{len(d.documents)}</b>PDF documents read</div>
<div class="tile"><b>{sum(len(g['pages']) for g in d.forms)}</b>forms read (read-only)</div>
<div class="tile"><b>{sum(1 for k in ('accept', 'reject', 'withdrawal') if (d.consent.get(k) or {}).get('tested'))}</b>consent states tested</div>
<div class="tile"><b>{len(r['review'])}</b>results flagged for human review</div></div>
<h2>Category-wise compliance</h2><table><thead><tr><th>Category</th><th class="num">Parameters</th><th class="num">Max marks</th><th class="num">Earned</th><th>Compliance</th><th>Pass / Partial / Fail / N.A.</th></tr></thead><tbody>""")
    for k, cc in ((k, r["categories"][k]) for k in cat.CATS if k in r["categories"]):
        pct = cc["pct"]
        bar = f'<span class="bar"><i style="width:{pct or 0}%"></i></span> {fm(pct)}%' if pct is not None else '<span class="small">not applicable to this organisation</span>'
        cn = cc["counts"]
        w(f"<tr><td>{e(cc['label'])}</td><td class='num'>{cc['parameters']}</td><td class='num'>{fm(cc['max'])}<span class='small'> / {fm(cc['max_all'])}</span></td><td class='num'>{fm(cc['earned'])}</td>"
          f"<td>{bar}</td><td>{cn.get('pass', 0)} / {cn.get('partial', 0)} / {cn.get('fail', 0)} / {cn.get('not_applicable', 0) + cn.get('information', 0)}</td></tr>")
    w(f"<tr><td><b>Total</b></td><td class='num'><b>{r['parameters']}</b></td><td class='num'><b>{fm(r['applicable_max'])}</b><span class='small'> / 100</span></td><td class='num'><b>{fm(r['marks_earned'])}</b></td>"
      f"<td><b>{fm(r['score'])}%</b></td><td></td></tr></tbody></table>")
    gaps = sorted([x for x in scored if x["status"] != "pass"], key=lambda x: (-(x["max"] - x["earned"]), x["parameter_id"]))[:8]
    w("<h2>Major compliance gaps (by marks lost)</h2><table><thead><tr><th>ID</th><th>Parameter</th><th>Act / Rule</th><th class='num'>Lost</th><th>Status</th></tr></thead><tbody>")
    for x in gaps:
        p = cat.BY_ID[x["parameter_id"]]
        w(f"<tr><td>{p.id}</td><td>{e(p.name)}<div class='small'>{e(x['explanation'])}</div></td><td>{legal(p)}</td><td class='num'>{fm(x['max'] - x['earned'])}</td><td>{chip(x['status'])}</td></tr>")
    w("</tbody></table>")

    # ---- 1. how the score is calculated
    acts = sorted({int(m) for p in cat.PARAMS for m in re.findall(r"s\.(\d+)", p.act)})
    rules = sorted({int(m) for p in cat.PARAMS for m in re.findall(r"R(\d+)", p.rule)})
    na_kinds = [lbl for k, lbl in _NA_DUTY.items() if (r["categories"].get(k) or {}).get("counts", {}).get("not_applicable")]
    w(f"""<h2 class="pb"><span class="n">1</span>How the score is calculated</h2>
<div class="grid2"><div class="box"><h3>Marks</h3><p>Each of the {r['parameters']} parameters is one independently assessable requirement. Its weight reflects its importance (directly observed core consent and notice duties weigh most; supporting technical signals least).
Weights total 400; <b>one weight point = 0.25 marks</b>, so the maximum marks across all parameters total exactly <b>100</b> and no parameter loses its relative weight.</p>
<table><thead><tr><th>Status</th><th>Marks awarded</th></tr></thead><tbody><tr><td>{chip('pass')}</td><td>100% of the parameter's marks</td></tr><tr><td>{chip('partial')}</td><td>50%</td></tr><tr><td>{chip('fail')}</td><td>0</td></tr>
<tr><td>{chip('not_applicable')}</td><td>Removed from the maximum, with the reason documented (a conditional duty that does not apply, a check with nothing to test on this site, or a result held for human review)</td></tr></tbody></table></div>
<div class="box"><h3>Score</h3><p><b>Score = marks earned ÷ applicable maximum × 100</b><br>= {fm(r['marks_earned'])} ÷ {fm(r['applicable_max'])} × 100 = <b>{fm(r['score'])}</b></p>
<p>The applicable maximum is 100 minus the marks of parameters that do not apply ({fm(r['not_applicable_max'])} here{': ' + ', '.join(na_kinds) + ' duties' if na_kinds else ''}). Category percentages use the same formula within the category.</p>
<h3>URL-wise parameters</h3><p>Where a requirement applies to every page or form, every applicable URL is assessed and the result is: <b>Pass</b> if every URL meets it, <b>Partial</b> if at least half do, <b>Fail</b> if fewer than half do. One good page never earns the marks alone.</p></div></div>
<div class="grid2" style="margin-top:8px"><div class="box"><h3>No “unable to verify”</h3><p>Every applicable parameter receives Pass, Partial or Fail. Where a duty is internal to the organisation (security measures, breach procedure, contracts, records), it is judged from what the published policy states and the row says so: <i>“Published policy; internal records not inspected”</i>. A statement in the policy is not proof the practice exists, and its absence is reported as a missing statement.</p></div>
<div class="box"><h3>Coverage of the law</h3><p><b>{len(acts)} Act sections:</b> {', '.join('s.' + str(a) for a in acts)}<br><b>{len(rules)} Rules:</b> {', '.join('Rule ' + str(x) for x in rules)}<br><b>Schedules:</b> First (Consent Managers), Third (erasure periods)<br>
<b>{r['parameters']} parameters</b>, {r['scored_parameters']} scored, 1 information only. Applicability: Act ss.3-17 (except 6(9)) and Rules 3, 5-16 apply from 13 May 2027; s.6(9) and Rule 4 from 13 Nov 2026 — results are readiness findings, not violations.</p></div></div>""")

    # ---- 2. complete parameter-wise marking
    w('<h2 class="pb"><span class="n">2</span>Complete parameter-wise marking</h2><p>Every parameter in the catalogue, in category order. Evidence is quoted exactly or is a stored observation; each item names its URL.</p>')
    w("<table class='fixed'><colgroup><col style='width:6%'><col style='width:17%'><col style='width:9%'><col style='width:5%'><col style='width:6%'><col style='width:7%'><col style='width:28%'><col style='width:22%'></colgroup>"
      "<thead><tr><th>ID</th><th>Compliance parameter</th><th>DPDP Act / Rule</th><th class='num'>Max</th><th class='num'>Earned</th><th>Status</th><th>Evidence</th><th>Explanation</th></tr></thead><tbody>")
    for k, label in cat.CATS.items():
        cc = r["categories"][k]
        w(f"<tr class='cathead'><td colspan='3'>{e(label)}</td><td class='num'>{fm(cc['max_all'])}</td><td class='num'>{fm(cc['earned'])}</td><td colspan='3'>"
          f"{'' if cc['pct'] is None else fm(cc['pct']) + '% of applicable marks'}</td></tr>")
        for p in [p for p in cat.PARAMS if p.cat == k]:
            x = lines[p.id]
            ev = "".join(f"<div>{e(str(i)[:420])}</div>" for i in x["evidence"][:4]) or ("<div class='small'>Per-URL results in section 3.</div>" if x["urls"] else "")
            flags = ("<span class='flag'>human review</span>" if x["review"] else "") + ("<span class='flag'>records not inspected</span>" if p.kind == cat.RECORDS else "") + \
                    ("<span class='flag'>differs from v2</span>" if x.get("differs") else "")
            expl = e(x["explanation"]) + (f"<div class='small'>{e(x['limitation'])}</div>" if x.get("limitation") else "")
            w(f"<tr><td><b>{p.id}</b></td><td>{e(p.name)}<div class='small'>{e(p.kind)}</div>{flags}</td><td>{legal(p)}</td><td class='num'>{fm(p.max_marks)}</td><td class='num'>{fm(x['earned'])}</td>"
              f"<td>{chip(x['status'])}</td><td class='ev'>{ev}</td><td>{expl}</td></tr>")
    w(f"<tr class='cathead'><td colspan='3'>Total</td><td class='num'>100</td><td class='num'>{fm(r['marks_earned'])}</td><td colspan='3'>of {fm(r['applicable_max'])} applicable → score {fm(r['score'])}</td></tr></tbody></table>")

    # ---- 3. URL-wise
    checks: dict[str, dict] = defaultdict(dict)
    for pid in URL_COLS:
        for u in lines[pid]["urls"]:
            checks[u["url"]][pid] = (u["status"], u["note"])
    all_header_fail = [pid for pid in ("TS05", "TS06") if lines[pid]["urls"] and all(u["status"] != "pass" for u in lines[pid]["urls"])]
    w(f'<h2 class="pb"><span class="n">3</span>Complete URL-wise assessment</h2><p>All {pages_inspected} web pages inspected one at a time. ✓ meets, ✗ does not, · not applicable (no form on the page). '
      f'<b>DPDP result</b> covers the DPDP checks ({", ".join(URL_COLS)}); technical headers are shown separately'
      + (f". {' and '.join('HSTS' if p == 'TS05' else 'CSP' for p in all_header_fail)} {'are' if len(all_header_fail) > 1 else 'is'} missing on every page, so "
         f"{'they are' if len(all_header_fail) > 1 else 'it is'} reported once in section 9 rather than marking every URL" if all_header_fail else "") + ".</p>")
    w(f"<table><thead><tr><th>#</th><th>Page ({e(site_host)})</th><th>Type</th>" + "".join(f"<th class='c'>{x}</th>" for x in URL_COLS) +
      "<th class='c'>Forms</th><th>DPDP result</th><th>Note</th></tr></thead><tbody>")
    url_status = Counter()
    for i, pgx in enumerate(d.pages, 1):
        row = checks.get(pgx["url"], {})
        cells, okc, n = [], 0, 0
        for pid in URL_COLS:
            if pid in row:
                st, _ = row[pid]
                n += 1
                okc += st == "pass"
                cells.append("<td class='c' style='color:#166534'>✓</td>" if st == "pass" else "<td class='c' style='color:#991b1b'><b>✗</b></td>")
            else:
                cells.append("<td class='c small'>·</td>")
        st = "pass" if okc == n else ("partial" if okc * 2 >= n else "fail")
        url_status[st] += 1
        notes = "; ".join(v[1] for v in row.values() if v[0] != "pass")
        w(f"<tr><td>{i}</td><td class='url'>{e(_path(pgx['url'], d.site))}</td><td class='small'>{e(pgx['category'] or '')}</td>{''.join(cells)}<td class='c'>{pgx['forms'] or '·'}</td>"
          f"<td>{chip(st)}</td><td class='small'>{e(notes[:200])}</td></tr>")
    w("</tbody></table>")
    w(f"<p><b>URL results:</b> {url_status['pass']} pages pass every DPDP check, {url_status['partial']} partly, {url_status['fail']} fail.</p>")
    excluded = Counter((x.get("status_reason") or "other").split(":")[0] for x in d.frontier if x["status"] == "excluded")
    failed = [x for x in d.frontier if x["status"] in ("failed", "pending")]
    w("<h3>URLs not inspected, and why</h3><table><thead><tr><th>Reason</th><th class='num'>URLs</th><th>Meaning</th></tr></thead><tbody>")
    for k2, v in excluded.most_common():
        w(f"<tr><td>{e(k2)}</td><td class='num'>{v}</td><td>{e(EXCLUDED_MEANING.get(k2, ''))}</td></tr>")
    for x in failed:
        w(f"<tr><td>{e(x['status'])}</td><td class='num'>1</td><td class='url'>{e(x['original_url'])}: {e((x.get('status_reason') or '')[:160])}"
          f"{(' (HTTP ' + str(x['http_status']) + ')') if x.get('http_status') else ''}</td></tr>")
    if not excluded and not failed:
        w("<tr><td colspan='3' class='small'>Every URL found on the host was inspected.</td></tr>")
    w("</tbody></table><p class='small'>Every URL found, with how it was found and what became of it, is listed in Appendix C.</p>")
    if d.documents:
        w("<h3>PDF documents read</h3><table><thead><tr><th>Document</th><th class='num'>Pages read</th></tr></thead><tbody>" +
          "".join(f"<tr><td class='url'>{e(_path(x['source_url'], d.site))}</td><td class='num'>{x['n']}</td></tr>" for x in d.documents) + "</tbody></table>")

    # ---- 4. privacy policy analysis
    policy_pages = [x for x in d.frontier if x["status"] == "done" and (x.get("page_category") or "") in ("privacy_policy", "cookie_policy", "grievance")]
    read = ", ".join(f"<b>{e(x.get('final_url') or x['original_url'])}</b>" for x in policy_pages[:4]) or "the policy pages found on the site"
    w(f'<h2 class="pb"><span class="n">4</span>Privacy policy analysis</h2><p>Notice read in full at {read}. Each requirement the notice must state, with its status and the passage relied on.</p>')
    w("<table><thead><tr><th>ID</th><th>What the notice must say</th><th>Act / Rule</th><th>Status</th><th>What it says (verbatim)</th></tr></thead><tbody>")
    for p in cat.PARAMS:
        if p.kind in (cat.POLICY, cat.RECORDS):
            x = lines[p.id]
            w(f"<tr><td>{p.id}</td><td>{e(p.name)}</td><td>{legal(p)}</td><td>{chip(x['status'])}</td><td class='ev'>{e((x['evidence'][0] if x['evidence'] else x['explanation'])[:380])}</td></tr>")
    w("</tbody></table>")

    # ---- 5. consent and withdrawal
    acc = d.consent.get("accept") or {}
    recheck = d.consent.get("pre_consent_recheck") or {}
    banner = next((v.get("cmp_vendor") or v.get("vendor") for v in d.consent.values() if isinstance(v, dict) and (v.get("cmp_vendor") or v.get("vendor"))), None)
    w(f'<h2><span class="n">5</span>Consent and withdrawal assessment</h2><p>{("Consent tool: " + e(str(banner)) + ". ") if banner else ""}'
      f'Accept, Reject and withdrawal were tested on the homepage in fresh browsers; before-consent behaviour on every page.</p>')
    w("<table><thead><tr><th>State</th><th>Result</th><th>What was observed</th></tr></thead><tbody>")
    w(f"<tr><td>Before a choice (every page)</td><td>{chip(lines['C12']['status'])}</td><td>{e(lines['C12']['explanation'])}</td></tr>")
    if acc.get("tested"):
        w(f"<tr><td>After Accept</td><td><span class='chip na'>observed</span></td><td>{len(acc.get('cookies') or [])} cookies set"
          f"{(' (' + e(', '.join(str(k.get('name')) for k in (acc.get('cookies') or [])[:8])) + ')') if acc.get('cookies') else ''}; services started: "
          f"{e(', '.join(sorted({x.get('vendor') or x.get('host') for x in acc.get('non_essential') or []})) or 'none')}</td></tr>")
    w(f"<tr><td>After Reject</td><td>{chip(lines['C13']['status'])}</td><td>{e(lines['C13']['explanation'])} {e(lines['C14']['explanation'])}</td></tr>")
    w(f"<tr><td>Withdrawal</td><td>{chip(lines['C16']['status'])}</td><td>{e(lines['C16']['explanation'])}</td></tr>")
    w(f"<tr><td>After withdrawal</td><td>{chip(lines['C17']['status'])}</td><td>{e(lines['C17']['explanation'])}</td></tr>")
    w(f"<tr><td>Per-purpose choice</td><td>{chip(lines['C04']['status'])}</td><td>{e(lines['C04']['explanation'])}</td></tr></tbody></table>")
    if recheck.get("pages"):
        w("<h3>Clean reloads of pages that tracked before a choice</h3><table><thead><tr><th>Page</th><th class='num'>Loads</th><th class='num'>Fired</th><th>Result</th></tr></thead><tbody>" +
          "".join(f"<tr><td class='url'>{e(x.get('url', ''))}</td><td class='num'>{x.get('loads', 0)}</td><td class='num'>{x.get('fired_loads', 0)}</td><td>{'repeated' if x.get('fired') else 'not repeated'}</td></tr>"
                  for x in recheck["pages"]) + "</tbody></table>")

    # ---- 6. cookies and trackers
    undeclared = _named_list(lines["X01"]["explanation"], "not declared:")
    w('<h2><span class="n">6</span>Cookies and tracker findings</h2><table><thead><tr><th>Cookie</th><th>Domain</th><th>Service</th><th>Seen</th><th>Lifetime</th><th>Secure</th><th>SameSite</th><th>In the notice</th></tr></thead><tbody>')
    ref = d.scan.get("completed_at") or d.generated_at
    ref_ts = (ref if isinstance(ref, datetime) else datetime.fromisoformat(str(ref))).timestamp()
    for k in d.cookies:
        exp = k.get("expires")
        days = (exp - ref_ts) / 86400 if exp else None
        life = "session" if k.get("session") or days is None or days < 0 else (f"{round(days * 24)} hours" if days < 1 else f"{round(days)} days" if days < 45 else f"{round(days / 30.4)} months")
        seen = ", ".join(CONSENT_STATES.get(s, s).replace("before a choice", "pre consent").replace("after Accept", "post accept").replace("after Reject", "post reject") for s in sorted(k["states"]))
        named = "n/a" if lines["X01"]["status"] == "not_applicable" else ("not named" if (k.get("name") or "").lower() in undeclared else (f"{k['provider']} named" if k.get("provider") else "named"))
        w(f"<tr><td class='mono'>{e(k.get('name') or '')}</td><td>{e(k.get('domain') or '')}</td><td>{e(k.get('provider') or 'unidentified')}</td><td>{e(seen)}</td><td>{life}</td>"
          f"<td>{'yes' if k.get('secure') else 'no'}</td><td>{e(str(k.get('same_site') or ''))}</td><td>{e(named)}</td></tr>")
    if not d.cookies:
        w("<tr><td colspan='8' class='small'>No cookie was observed.</td></tr>")
    w("</tbody></table>")
    hits = [u for u in lines["C12"]["urls"] if u["status"] != "pass"]
    if hits:
        w("<h3>Pages where an analytics cookie appeared before a choice</h3><table><thead><tr><th>URL</th><th>Observed</th></tr></thead><tbody>" +
          "".join(f"<tr><td class='url'>{e(u['url'])}</td><td>{e(u['note'][:220])}</td></tr>" for u in hits[:60]) + "</tbody></table>")
        if len(hits) > 60:
            w(f"<p class='small'>{len(hits) - 60} further pages show the same; every page is listed in section 3.</p>")

    # ---- 7. forms
    w(f'<h2 class="pb"><span class="n">7</span>Form-wise assessment</h2><p>{sum(len(g["pages"]) for g in d.forms)} data-collecting forms on {len({u for g in d.forms for u in g["pages"]})} pages, '
      'grouped by identical design. Forms are read, never filled or submitted.</p>')
    w("<table><thead><tr><th>Form (submit text)</th><th>Collects</th><th class='num'>Instances</th><th class='c'>Consent box</th><th class='c'>Pre-ticked</th><th class='c'>Notice link</th><th>Method</th><th>Example page</th></tr></thead><tbody>")
    for g in d.forms:
        w(f"<tr><td>{e(g['label'] or '(no text)')}</td><td>{e(', '.join(g['collects']))}</td><td class='num'>{len(g['pages'])}</td><td class='c'>{'✓' if g['consent'] else '<b style=color:#991b1b>✗</b>'}</td>"
          f"<td class='c'>{'<b style=color:#991b1b>yes</b>' if g['preticked'] else 'no'}</td><td class='c'>{'✓' if g['notice'] else '<b style=color:#991b1b>✗</b>'}</td>"
          f"<td>{e(g['method'])}{' (same page)' if g['method'] == 'GET' else ''}</td><td class='url small'>{e(_path(g['pages'][0], d.site))}</td></tr>")
    if not d.forms:
        w("<tr><td colspan='8' class='small'>No form that collects personal data was found.</td></tr>")
    w("</tbody></table>")

    # ---- 8. third-party services
    not_named = _named_list(lines["X02"]["explanation"], "not named:")
    w('<h2><span class="n">8</span>Third-party service analysis</h2><table><thead><tr><th>Host</th><th>Service</th><th>Category</th><th class="num">Pages</th><th class="num">Requests</th><th>Named in notice</th></tr></thead><tbody>')
    for h in d.hosts:
        functional = h["category"] in ("functional", "not classified") or "functional" in h["category"]
        named = "n/a" if functional else ("no" if (h["vendor"] or "").lower() in not_named else "yes")
        after = "" if "before a choice" in h["states"] else " (after Accept)" if "after Accept" in h["states"] else ""
        ex = "".join(f"<div class='mono small'>{e(u[:150])}</div>" for u in h["examples"][:2])
        w(f"<tr><td class='mono'>{e(h['host'])}</td><td>{e(h['vendor'])}{ex}</td><td>{e(h['category'])}{after}</td><td class='num'>{h['pages'] or 'homepage'}</td>"
          f"<td class='num'>{h['requests'] or '—'}</td><td>{named}</td></tr>")
    if not d.hosts:
        w("<tr><td colspan='6' class='small'>No third-party host was contacted.</td></tr>")
    w("</tbody></table>")

    # ---- 9. technical
    ts = [p for p in cat.PARAMS if p.cat == "TS"]
    w(f'<h2><span class="n">9</span>Technical security assessment</h2><p class="small">Supporting signals for s.8(5) and Rule 6. Rule 6 names no website control, so these carry '
      f'{sum(p.weight for p in ts)} of 400 weight points ({fm(sum(p.max_marks for p in ts))} marks) and are never described as legal duties.</p>')
    w("<table><thead><tr><th>ID</th><th>Signal</th><th class='num'>Max</th><th class='num'>Earned</th><th>Status</th><th>Result</th></tr></thead><tbody>")
    for p in ts:
        x = lines[p.id]
        w(f"<tr><td>{p.id}</td><td>{e(p.name)}</td><td class='num'>{fm(p.max_marks)}</td><td class='num'>{fm(x['earned'])}</td><td>{chip(x['status'])}</td><td>{e(x['explanation'])}</td></tr>")
    w("</tbody></table>")
    if d.tls:
        w(f"<p class='small'>Certificate: {e(str(d.tls.get('subject')))}, issuer {e(str(d.tls.get('issuer')))}, valid {e(str(d.tls.get('not_before'))[:10])} to {e(str(d.tls.get('not_after'))[:10])}, "
          f"{'verified' if d.tls.get('verified') else 'not verified'}. {e(str(d.tls.get('negotiated') or ''))} negotiated; TLS 1.3 {'supported' if d.tls.get('tls13_supported') else 'not supported'}.</p>")

    # ---- 10. conditional obligations
    w('<h2 class="pb"><span class="n">10</span>Conditional obligations</h2><p>These duties apply only to some organisations. Where one does not apply, it is removed from the maximum with the reason below; it is never counted as passed or failed.</p>')
    site_cat = r.get("site_category")
    if site_cat:
        conf = site_cat.get("confidence") or {}
        others = ", ".join(f"{cat_label(c2)} ({conf.get(c2, '')})" for c2 in site_cat.get("categories") or [])
        weak = ", ".join(cat_label(c2) for c2 in site_cat.get("possible") or [])
        users = "; ".join(f'"{e(u["text"])}"' for u in (site_cat.get("users_stated") or [])[:2])
        w(f"<div class='note'><b>Website category: {e(site_cat.get('label', ''))}</b>"
          + (f" · with enough evidence: {others}" if others else "") + (f" · possible (one signal only, never applied alone): {e(weak)}" if weak else "")
          + (f" · user numbers stated on the site: {users}" if users else "")
          + "<br><span class='small'>Read from the site's own pages, forms, third-party services and policy text. It decides which conditional duties apply; it never changes a finding's severity.</span></div>")
    hr = r.get("human_review_pending") or []
    if hr:
        w(f"<p><b>{fm(r.get('human_review_max'))} marks held back for human review</b> ({', '.join(hr)}): the website does not settle these results (for a conditional duty, whether it "
          "applies; for N01, whether a notice is published on a page the crawl did not reach). They are kept out of the maximum, like a duty that does not apply, but they are not a "
          "confirmed Not applicable. The score in this report is calculated without them; a person should confirm each one (section 11) before the score is relied on.</p>")
    w("<table><thead><tr><th>ID</th><th>Obligation</th><th>Act / Rule</th><th class='num'>Marks</th><th>Applies?</th><th>Reason</th></tr></thead><tbody>")
    for p in cat.PARAMS:
        if p.kind in (cat.CONDITIONAL, cat.INFO):
            x = lines[p.id]
            appl = x.get("applicability")
            tag = ("<span class='flag'>human review</span>" if x["review"] else "") + (f"<span class='flag'>severity {e(x['severity'])}</span>" if x.get("severity") else "")
            label = chip(x["status"]) if appl != "human_review" else "<span class='chip part'>Human review</span>"
            w(f"<tr><td>{p.id}</td><td>{e(p.name)}</td><td>{legal(p)}</td><td class='num'>{fm(p.max_marks)}</td><td>{label}{tag}</td><td>{e(x['explanation'])}</td></tr>")
    w("</tbody></table>")
    w("<h3>Applicability, severity and status</h3><table class='fixed'><colgroup><col style='width:12%'><col style='width:10%'><col style='width:12%'><col style='width:8%'>"
      "<col style='width:10%'><col style='width:18%'><col style='width:20%'><col style='width:10%'></colgroup><thead><tr><th>ID</th><th>Category</th><th>Applicability</th>"
      "<th>Severity</th><th>Status</th><th>Evidence</th><th>Explanation</th><th>Act / Rule</th></tr></thead><tbody>")
    for pid in ("K01", "K02", "K03", "K04", "K05", "RT4", "G02"):
        p, x = cat.BY_ID[pid], lines[pid]
        appl = x.get("applicability") or ("not_applicable" if x["status"] == "not_applicable" else "applicable")
        dcat = x.get("duty_category") or ""
        if pid == "RT4" and site_cat and any(c2 in (site_cat.get("categories") or []) for c2 in ("ecommerce", "gaming", "social_media")):
            dcat = ", ".join(cat_label(c2) for c2 in site_cat["categories"] if c2 in ("ecommerce", "gaming", "social_media")) + " (Third Schedule)"
        status = "Not applicable" if x["status"] == "not_applicable" else x["status_label"]
        ev = "<br>".join(e(v) for v in (x.get("evidence") or [])[:3]) or "<span class='small'>none</span>"
        w(f"<tr><td><b>{pid}</b><br><span class='small'>{e(p.name)}</span></td><td>{e(dcat)}</td><td>{APPL_LABEL.get(appl, e(appl))}</td>"
          f"<td>{SEV_LABEL.get(x.get('severity'), '<span class=small>None</span>')}</td><td>{chip(x['status']) if x['status'] != 'not_applicable' else e(status)}</td>"
          f"<td class='small'>{ev}</td><td>{e(x['explanation'])}</td><td>{legal(p)}</td></tr>")
    w("</tbody></table><p class='small'>Severity is the importance of the duty itself (High: significant statutory risk; Medium: a meaningful gap to correct; Low: a minor "
      "presentation or disclosure issue). A Fail shows the duty's severity, a Partial one level lower; a Pass, a Not applicable or the information-only entry (E01) has none.</p>")

    # ---- 11. evidence limits and human review
    w('<h2><span class="n">11</span>Evidence limits and human-review requirements</h2>')
    w("<h3>Results that need a person to confirm</h3><table><thead><tr><th>ID</th><th>Parameter</th><th>Status</th><th>What to confirm</th></tr></thead><tbody>")
    for p in cat.PARAMS:
        x = lines[p.id]
        if x["review"]:
            w(f"<tr><td>{p.id}</td><td>{e(p.name)}</td><td>{chip(x['status'])}</td><td>{e(x.get('limitation') or x['explanation'])}</td></tr>")
    w("</tbody></table><h3>Duties judged from the published policy only (internal records not inspected)</h3><p>")
    w(", ".join(f"<b>{p.id}</b> {e(p.name)}" for p in cat.PARAMS if p.kind == cat.RECORDS))
    w(". For these, Pass means the policy states the practice; the organisation's records (contracts, logs, breach procedure, security controls) would confirm it.</p>")
    w("<h3>Other limits</h3><p>Only the submitted host was scanned: other hosts and subdomains (for example a separate application or login site) were not opened. Pages behind a login were not reached. "
      "Forms were read, not submitted. Consent tests ran on the homepage; before-consent behaviour was observed on every page. Pages were opened one at a time.</p>")
    if d.sitemap.get("sitemap_capped"):
        w(f"<p>The sitemap reader stops at {d.sitemap.get('sitemap_cap', 500)} URLs and this site's sitemap reached that limit: sitemap entries past it were found only where "
          "a page links to them, so a page listed only in the sitemap may not have been inspected.</p>")

    # ---- 12. prioritised corrective actions
    todo = sorted([x for x in scored if x["status"] != "pass"], key=lambda x: (-(x["max"] - x["earned"]), x["parameter_id"]))
    w('<h2 class="pb"><span class="n">12</span>Prioritised corrective actions</h2><p>Ordered by marks recoverable. Fixing every item would raise the score to 100.</p>')
    w("<table><thead><tr><th>#</th><th>ID</th><th>Action</th><th>Act / Rule</th><th class='num'>Marks</th><th>Owner</th><th>Effort</th></tr></thead><tbody>")
    for i, x in enumerate(todo, 1):
        p = cat.BY_ID[x["parameter_id"]]
        a = ACTIONS.get(p.id) or ("Meet the requirement: " + p.requirement, "Engineering" if p.cat == "TS" else ("Legal / privacy lead" if p.kind == cat.RECORDS else "Privacy lead"), "Low")
        w(f"<tr><td>{i}</td><td>{p.id}</td><td>{e(a[0])}</td><td>{legal(p)}</td><td class='num'>+{fm(x['max'] - x['earned'])}</td><td>{e(a[1])}</td><td>{e(a[2])}</td></tr>")
    w("</tbody></table>")

    # ---- 13. final scoring summary
    w('<h2><span class="n">13</span>Final scoring summary</h2><table><thead><tr><th>Category</th><th class="num">Applicable max</th><th class="num">Earned</th><th class="num">%</th></tr></thead><tbody>')
    for k, cc in ((k, r["categories"][k]) for k in cat.CATS if k in r["categories"]):
        w(f"<tr><td>{e(cc['label'])}</td><td class='num'>{fm(cc['max'])}</td><td class='num'>{fm(cc['earned'])}</td><td class='num'>{fm(cc['pct']) if cc['pct'] is not None else 'n/a'}</td></tr>")
    w(f"<tr class='cathead'><td>Overall</td><td class='num'>{fm(r['applicable_max'])}</td><td class='num'>{fm(r['marks_earned'])}</td><td class='num'>{fm(r['score'])}</td></tr></tbody></table>")
    na_lines = [x for x in lines.values() if x["status"] == "not_applicable"]
    na_review = [x for x in na_lines if x.get("applicability") == cat.HUMAN_REVIEW]
    na_cond = [x for x in na_lines if x not in na_review and cat.BY_ID[x["parameter_id"]].kind == cat.CONDITIONAL]
    na_other = len(na_lines) - len(na_review) - len(na_cond)
    why = [f"{len(na_cond)} conditional duties that do not apply to this organisation"] if na_cond else []
    why += [f"{na_other} checks with nothing to test on this site (for example no form, no cookie or no third-party service)"] if na_other else []
    why += [f"{len(na_review)} held back for human review"] if na_review else []
    w(f"<p><b>DPDP compliance score: {fm(r['score'])} / 100</b> — {c.get('pass', 0)} Pass, {c.get('partial', 0)} Partial, {c.get('fail', 0)} Fail across {len(scored)} applicable parameters; "
      f"{len(na_lines)} parameters not applicable" + (f" ({'; '.join(why)})" if why else "") + "; "
      f"{c.get('information', 0)} shown for information only.</p>")

    # ---- appendix A: inventory and criteria
    w(f'<h2 class="pb">Appendix A — Parameter inventory and marking criteria (catalogue {e(r["catalogue_version"])})</h2>'
      '<p>For each parameter: the requirement, the law, the marks, the evidence needed, how it is assessed, and the Pass / Partial / Fail criteria.</p>')
    for k, label in cat.CATS.items():
        w(f"<h3>{e(label)}</h3>")
        for p in [p for p in cat.PARAMS if p.cat == k]:
            w(f"<div class='inv'><div class='h'><span>{p.id} · {e(p.name)}</span><span>{fm(p.max_marks)} mark{'' if p.max_marks == 1 else 's'} (weight {p.weight})</span></div><dl>"
              f"<dt>Requirement</dt><dd>{e(p.requirement)}</dd><dt>DPDP Act</dt><dd>{e(p.act)}</dd><dt>DPDP Rules</dt><dd>{e(p.rule)}</dd><dt>Type</dt><dd>{e(p.kind)}</dd>"
              f"<dt>Evidence required</dt><dd>{e(p.evidence)}</dd><dt>Method</dt><dd>{e(p.method)}</dd><dt>Pass</dt><dd>{e(p.pass_)}</dd><dt>Partial</dt><dd>{e(p.partial)}</dd>"
              f"<dt>Fail</dt><dd>{e(p.fail)}</dd></dl></div>")

    # ---- appendix B: differences from the 51-parameter method
    diffs = [x for x in r["lines"] if x.get("differs")]
    w('<h2 class="pb">Appendix B — Where this result differs from the 51-parameter method (v2)</h2><p>Same scan, same evidence. These are the results where the method 3 criteria, or a closer reading of the '
      'stored evidence, give a different answer from method 2.0.0. They are listed so the change can be reviewed, not hidden.</p>')
    if diffs:
        w("<table><thead><tr><th>ID</th><th>Parameter</th><th>This result</th><th>Why it differs</th></tr></thead><tbody>" +
          "".join(f"<tr><td>{x['parameter_id']}</td><td>{e(cat.BY_ID[x['parameter_id']].name)}</td><td>{chip(x['status'])}</td><td>{e(x['differs'])}</td></tr>" for x in diffs) + "</tbody></table>")
    else:
        w("<p>No result differs from the 51-parameter method's reading of the same evidence.</p>")
    w("<h3>How the 51 v2 parameters map to the 96</h3><p>Combined v2 parameters are split so each requirement has its own marks: v2 G7 (security safeguards) → F04-F07; v2 G3 (rights) → R01, R03, R04, R06; "
      "v2 C3 (affirmative action) → C02, C03; v2 K1 (children) → K01-K04; v2 S1 (SDF) → S01-S06; v2 G8 (breach) → F10, F11; v2 TS4 (headers) → TS05-TS10; v2 C1 → C08, C09. "
      "New parameters with no v2 equivalent: N02, N07, N11, N14, C05, C07, C14, L02, F03, RT2, RT3, RT5, G02, G04, R02, R08, T02, TS15.</p>")

    # ---- appendix C: every URL found, and the scan's transparency
    w(f'<h2 class="pb">Appendix C — Every URL found, and what became of it ({len(d.frontier)})</h2>')
    w("<table><tbody>"
      f"<tr><td>URLs discovered</td><td class='num'>{len(d.frontier)}</td></tr><tr><td>Web pages inspected one at a time</td><td class='num'>{len(d.pages)}</td></tr><tr><td>Documents read (PDF, DOCX)</td><td class='num'>{len(d.documents)}</td></tr>"
      f"<tr><td>Failed or not reached</td><td class='num'>{fr_status.get('failed', 0) + fr_status.get('pending', 0)}</td></tr>"
      f"<tr><td>Excluded (another host, duplicate, variant)</td><td class='num'>{fr_status.get('excluded', 0)}</td></tr>"
      f"<tr><td>Clean reloads of pages that tracked before a choice</td><td class='num'>{len(recheck.get('pages') or []) if recheck else 'not run'}</td></tr>"
      f"<tr><td>Crawl</td><td class='num'>{_ist(d.scan.get('started_at'))} → {_ist(d.scan.get('completed_at'))}</td></tr>"
      f"<tr><td>Analysis</td><td class='num'>{_ist(d.scan.get('analysis_started_at'))} → {_ist(d.scan.get('analysis_completed_at'))}</td></tr></tbody></table>"
      "<p class='small'>Pages were found from the homepage's links, every page's links and the sitemap, on the submitted host only; privacy pages were inspected first, then other legal pages, then the rest.</p>")
    w("<h3>Every URL found</h3><table><thead><tr><th>#</th><th>URL</th><th>Found by</th><th>Status</th><th>Reason</th></tr></thead><tbody>")
    for i, x in enumerate(d.frontier, 1):
        w(f"<tr><td>{i}</td><td class='url'>{e(x['original_url'])}</td><td class='small'>{e(str(x.get('discovery_source') or ''))}</td><td>{e(x['status'])}"
          f"{(' (HTTP ' + str(x['http_status']) + ')') if x.get('http_status') and x['http_status'] != 200 else ''}</td><td class='small'>{e((x.get('status_reason') or '')[:180])}</td></tr>")
    w("</tbody></table>")
    body = "".join(H)
    return (f"<!doctype html><html><head><meta charset='utf-8'><meta name=\"consiva-report-scan\" content=\"{e(d.scan_id, quote=True)}\">"
            f"<meta name=\"consiva-report-site\" content=\"{e(site_host, quote=True)}\"><meta name=\"consiva-report-version\" content=\"{e(r['catalogue_version'], quote=True)}\">"
            f"<title>Enterprise DPDP assessment — {e(site_host)}</title><style>{_FONTS}{CSS}</style></head><body>{body}</body></html>")

