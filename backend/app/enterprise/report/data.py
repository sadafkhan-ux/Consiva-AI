"""Everything the Enterprise report says, read from the Enterprise tables and the catalogue, as plain dataclasses.

Nothing is estimated and nothing is hard-coded: a figure comes from a row, and a figure that cannot be known is `None`, which the renderer prints as "not recorded". Reading is split in two:
`gather` runs the queries (each one tagged with a leading SQL comment so a test double can answer it) and `assemble` turns the raw rows into the dataclasses, as a pure function.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.enterprise.coverage import coverage_declaration
from app.enterprise.scoring.catalogue import (
    GROUP_LABELS, IN_FORCE_CONSENT_MANAGER, IN_FORCE_MAIN, LEGAL_REVIEW_STATUS, PARAMETERS, STATUTORY, TOTAL_POINTS,
)

LISTING_CAP = 200          # URL rows shown in the report; the total is always stated
EVIDENCE_CAP = 300         # evidence references listed; the total is always stated
PAGE_ROWS_CAP = 40         # per-page security rows shown
SOURCES_FILE = Path(__file__).resolve().parents[1] / "registry" / "sources.json"
POLICY_PATTERN = r"(privacy|cookie|terms|conditions|grievance|redress|data-protection|dpdp|personal-data)"
_POLICY_KINDS = (
    ("Cookie policy", re.compile(r"cookie", re.I)),
    ("Privacy policy", re.compile(r"privacy|data-protection|dpdp|personal-data", re.I)),
    ("Terms and conditions", re.compile(r"terms|conditions", re.I)),
    ("Grievance redressal", re.compile(r"grievance|redress", re.I)),
)
SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2, "info": 3}
CONSENT_TESTS = ("accept", "reject", "reject_persistence", "withdrawal", "purposes", "button_comparison")


# ---- the dataclasses ---------------------------------------------------------------------------------------------------------------------------------------------------

@dataclass
class ScanFacts:
    scan_id: str
    website_url: str
    domain: str
    crawl_status: str
    analysis_status: str
    created_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    analysis_started_at: datetime | None = None
    analysis_completed_at: datetime | None = None
    stop_reason: str | None = None
    error: str | None = None
    analysis_error: str | None = None
    previous_scan_id: str | None = None
    options: dict = field(default_factory=dict)


@dataclass
class UrlRow:
    url: str
    final_url: str | None
    depth: int | None
    status: str
    analysis_status: str | None
    http_status: int | None
    status_reason: str | None
    discovery_sources: list[str] = field(default_factory=list)


@dataclass
class UrlInventory:
    total: int | None
    shown: list[UrlRow] = field(default_factory=list)
    cap: int = LISTING_CAP


@dataclass
class PolicyRow:
    url: str
    kinds: list[str]
    title: str | None
    title_source: str | None
    content_hash: str | None
    size: int | None
    pages: int | None = None            # for a document: the pages read


@dataclass
class CookieRow:
    identity: str
    name: str
    domain: str
    path: str
    first_party: bool | None
    secure: bool | None
    http_only: bool | None
    same_site: str | None
    session: bool | None
    expires: str | None
    pages: list[str] = field(default_factory=list)
    consent_states: list[str] = field(default_factory=list)


@dataclass
class HostRow:
    host: str
    third_party: bool
    requests: int
    script_requests: int
    pages: list[str] = field(default_factory=list)
    consent_states: list[str] = field(default_factory=list)
    types: dict[str, int] = field(default_factory=dict)
    classified: list[str] = field(default_factory=list)      # "analytics (Vendor)" for what the catalogue named before any choice or after one


@dataclass
class FormRow:
    page_url: str
    action: str
    action_host: str
    method: str
    fields: list[dict]
    collects: list[str]
    consent_controls: list[dict]
    privacy_link: bool | None
    hidden_field_count: int | None


@dataclass
class IframeRow:
    page_url: str
    src: str
    title: str
    host: str
    third_party: bool


@dataclass
class SecurityPage:
    page_url: str
    https: bool | None
    present: list[str]
    missing: list[str]
    redirects: int | None


@dataclass
class SecurityResults:
    tls: dict | None
    pages_total: int
    pages_https: int
    header_counts: dict[str, int]
    headers_checked: list[str]
    insecure_requests: int | None
    insecure_examples: list[str]
    pages: list[SecurityPage] = field(default_factory=list)
    pages_cap: int = PAGE_ROWS_CAP


@dataclass
class ConsentTest:
    name: str
    tested: bool | None
    reason: str | None
    facts: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class PreConsent:
    pages_observed: int
    pages_with_non_essential: int
    non_essential: list[str] = field(default_factory=list)       # "Vendor (category)"


@dataclass
class DocumentRow:
    url: str
    final_url: str | None
    content_type: str | None
    status: str
    extraction_status: str | None
    extraction_method: str | None
    pages_read: int | None
    analysis_status: str | None
    status_reason: str | None


@dataclass
class ParameterRow:
    id: str
    name: str
    group: str
    group_label: str
    kind: str
    points: int
    partial_allowed: bool
    human_review_required: bool
    conditional: bool
    legal_refs: list[str]
    in_force: str | None
    penalty_ceiling_cr: int | None
    schedule_item: int | None
    state: str
    earned: float | None
    in_play: float | None
    note: str | None
    confidence: str | None
    assessor: str | None
    reviewed_by: str | None
    review_reason: str | None
    evidence_refs: list[str]
    adjustment: str | None


@dataclass
class ScoreView:
    id: str | None
    score: int | None
    version: str
    basis: str
    coverage: int
    reason_no_score: str | None
    points_earned: float | None
    points_in_play: float | None
    not_yet_in_force: list[str]
    by_group: dict[str, dict]
    needs_attention: dict[str, list[str]]
    as_of: str
    calculated_at: datetime | None
    supporting_scale: float | None = None


@dataclass
class FindingRow:
    id: str
    finding_key: str
    parameter_id: str
    title: str
    description: str
    severity: str
    state: str
    legal_refs: list[str]
    affected_urls: list[str]
    affected_count: int
    evidence_refs: list[str]
    expected: str
    observed: str
    reason: str
    confidence: str | None
    recommendation: str
    human_review: bool
    verification_status: str
    schedule_item: int | None
    penalty_ceiling_cr: int | None


@dataclass
class EvidenceRef:
    ref: str
    evidence_id: str | None
    kind: str | None
    source_url: str | None
    page_number: int | None
    content_hash: str | None
    detection_method: str | None
    captured_at: datetime | None
    cited_by: list[str] = field(default_factory=list)


@dataclass
class LegalSource:
    id: str
    title: str
    published: str | None
    gazette_identifier: str | None
    sha256: str | None
    note: str | None


@dataclass
class LegalFramework:
    sources: list[LegalSource]
    verified_on: str | None
    how_verified: str | None
    register_status: str | None
    commencement: list[tuple[str, str]]          # (date, what comes into force)
    main_date: str
    consent_manager_date: str
    legal_review_status: str


@dataclass
class ExposureRow:
    parameter_id: str
    state: str
    schedule_item: int | None
    maximum_penalty_crore: int
    legal_refs: list[str]


@dataclass
class ReportData:
    generated_at: datetime
    scan: ScanFacts
    declaration: dict
    completion: str | None
    urls: UrlInventory
    policies: list[PolicyRow]
    cookies: list[CookieRow]
    hosts: list[HostRow]
    forms: list[FormRow]
    iframes: list[IframeRow]
    security: SecurityResults
    consent: list[ConsentTest]
    pre_consent: PreConsent
    documents: list[DocumentRow]
    parameters: list[ParameterRow]
    score: ScoreView | None
    findings: list[FindingRow]
    evidence: list[EvidenceRef]
    evidence_total: int
    exposure: list[ExposureRow]
    legal: LegalFramework
    legal_review_status: str
    unreadable_observations: int = 0
    pages_observed: int = 0


# ---- the queries --------------------------------------------------------------------------------------------------------------------------------------------------------

_Q_SCAN = text("/* report:scan */ select id, website_url, domain, status, analysis_status, options, stop_reason, error, created_at, started_at, completed_at, analysis_started_at, "
               "analysis_completed_at, analysis_error, previous_scan_id from ent_scans where id = :scan_id")
_Q_URLS = text("/* report:urls */ select original_url, final_url, depth, status, analysis_status, http_status, status_reason, discovery_sources from ent_frontier "
               "where scan_id = :scan_id order by depth, normalized_url limit :cap")
_Q_POLICIES = text("""/* report:policies */
    select distinct on (e.source_url) e.source_url, e.content_hash, e.byte_size,
           (select substring(h.content from '(?i)<title[^>]*>([^<]{0,250})</title>') from ent_evidence h
             where h.scan_id = e.scan_id and h.kind = 'page_html' and h.source_url = e.source_url order by h.captured_at desc limit 1) as title,
           substring(e.content from 1 for 200) as opening
    from ent_evidence e where e.scan_id = :scan_id and e.kind = 'page_text' and e.source_url ~* :pat order by e.source_url, e.captured_at desc""")
_Q_POLICY_DOCS = text("""/* report:policy_docs */
    select source_url, count(*) as pages, sum(byte_size) as size,
           encode(sha256(convert_to(string_agg(content_hash, '' order by page_number), 'UTF8')), 'hex') as content_hash
    from ent_evidence where scan_id = :scan_id and kind = 'document_text' and source_url ~* :pat group by source_url order by source_url""")
_Q_OBSERVATIONS = text("/* report:observations */ select id, kind, source_url, detection_method, content, captured_at from ent_evidence "
                       "where scan_id = :scan_id and kind in ('cookie', 'network', 'header', 'form', 'consent') order by captured_at, id")
_Q_DOCUMENTS = text(r"""/* report:documents */
    select f.original_url, f.final_url, f.content_type, f.status, f.extraction_status, f.extraction_method, f.analysis_status, f.status_reason,
           (select count(*) from ent_evidence e where e.scan_id = f.scan_id and e.frontier_id = f.id and e.kind = 'document_text') as pages_read
    from ent_frontier f where f.scan_id = :scan_id and lower(f.normalized_url) ~ '\.(pdf|docx?|odt|rtf|xlsx?|pptx?)(\?|#|$)' order by f.normalized_url""")
_Q_ASSESSMENTS = text("/* report:assessments */ select parameter_id, state, evidence_refs, confidence, note, assessor, model, reviewed_by, review_reason, created_at "
                      "from ent_assessments where scan_id = :scan_id and status = 'current' order by parameter_id")
_Q_SCORE = text("/* report:score */ select id, version, as_of, score, coverage, basis, result, created_at from ent_score_results where scan_id = :scan_id order by created_at desc limit 1")
_Q_FINDINGS = text("""/* report:findings */
    select id, finding_key, parameter_id, title, description, severity, state, legal_refs, affected_urls, affected_count, evidence_refs, expected, observed, reason, confidence,
           recommendation, human_review, verification_status, schedule_item, penalty_ceiling_cr
    from ent_findings where scan_id = :scan_id
    order by case severity when 'high' then 0 when 'medium' then 1 when 'low' then 2 else 3 end, parameter_id, finding_key""")
_Q_EVIDENCE = text("/* report:evidence */ select id, kind, source_url, page_number, content_hash, detection_method, captured_at from ent_evidence "
                   "where scan_id = :scan_id and id = any(cast(:ids as uuid[]))")


async def _rows(db: AsyncSession, stmt, **params) -> list[dict]:
    return [dict(r) for r in (await db.execute(stmt, params)).mappings().all()]


def load_legal_framework() -> LegalFramework:
    """The legal sources, their gazette identifiers and dates, read from the registry file the Enterprise product keeps (not from the network)."""
    doc = json.loads(SOURCES_FILE.read_text(encoding="utf-8"))
    sources, commencement = [], []
    for d in doc.get("documents", []):
        sources.append(LegalSource(id=d["id"], title=d["title"], published=d.get("published"), gazette_identifier=d.get("gazette_identifier"), sha256=d.get("sha256"),
                                   note=d.get("note") or (d.get("effect") if isinstance(d.get("effect"), str) else d.get("text_read"))))
        if d["id"] == "COMMENCEMENT":
            for key, what in d["effect"].items():
                commencement.append((key.rsplit("_", 1)[-1], what))
    commencement.sort()
    return LegalFramework(sources=sources, verified_on=doc.get("verified_on"), how_verified=doc.get("how_verified"), register_status=doc.get("status"), commencement=commencement,
                          main_date=IN_FORCE_MAIN.isoformat(), consent_manager_date=IN_FORCE_CONSENT_MANAGER.isoformat(), legal_review_status=LEGAL_REVIEW_STATUS)


async def gather(db: AsyncSession, scan_id: uuid.UUID) -> ReportData:
    """Read the scan's stored facts. Raises LookupError if the scan does not exist."""
    scan = await _rows(db, _Q_SCAN, scan_id=scan_id)
    if not scan:
        raise LookupError(f"Enterprise scan {scan_id} not found")
    declaration = await coverage_declaration(db, scan_id)
    raw = {
        "scan": scan[0], "declaration": declaration,
        "urls": await _rows(db, _Q_URLS, scan_id=scan_id, cap=LISTING_CAP),
        "policies": await _rows(db, _Q_POLICIES, scan_id=scan_id, pat=POLICY_PATTERN),
        "policy_docs": await _rows(db, _Q_POLICY_DOCS, scan_id=scan_id, pat=POLICY_PATTERN),
        "observations": await _rows(db, _Q_OBSERVATIONS, scan_id=scan_id),
        "documents": await _rows(db, _Q_DOCUMENTS, scan_id=scan_id),
        "assessments": await _rows(db, _Q_ASSESSMENTS, scan_id=scan_id),
        "score": (await _rows(db, _Q_SCORE, scan_id=scan_id) or [None])[0],
        "findings": await _rows(db, _Q_FINDINGS, scan_id=scan_id),
    }
    wanted = sorted(_evidence_ids(raw))
    raw["evidence"] = await _rows(db, _Q_EVIDENCE, scan_id=scan_id, ids=wanted[:EVIDENCE_CAP * 4]) if wanted else []
    return assemble(raw, generated_at=datetime.now(timezone.utc))


# ---- assembling ---------------------------------------------------------------------------------------------------------------------------------------------------------

def _uuid_of(ref: str) -> str | None:
    if not isinstance(ref, str) or not ref.startswith("ev:"):
        return None
    try:
        return str(uuid.UUID(ref[3:]))
    except ValueError:
        return None


def _evidence_ids(raw: dict) -> set[str]:
    ids: set[str] = set()
    for a in raw.get("assessments", []):
        ids.update(u for u in (_uuid_of(r) for r in (a.get("evidence_refs") or [])) if u)
    for f in raw.get("findings", []):
        ids.update(u for u in (_uuid_of(r) for r in (f.get("evidence_refs") or [])) if u)
    return ids


def _iso(d) -> str | None:
    return d.isoformat() if isinstance(d, (date, datetime)) else (str(d) if d else None)


def _norm_domain(d: str | None) -> str:
    return (d or "").lstrip(".").lower()


def _json(content: str) -> Any | None:
    try:
        return json.loads(content)
    except (TypeError, ValueError):
        return None


def _add(seq: list, value) -> None:
    if value is not None and value not in seq:
        seq.append(value)


def _is_first_party(host: str, domain: str) -> bool:
    host, domain = (host or "").lower(), (domain or "").lower().removeprefix("www.")
    return bool(host) and (host == domain or host.endswith("." + domain) or host == "www." + domain)


def _policy_kinds(url: str) -> list[str]:
    path = urlparse(url).path or url
    return [name for name, rx in _POLICY_KINDS if rx.search(path)]


def _cookie_expiry(raw) -> str | None:
    if raw in (None, -1):
        return None
    try:
        return datetime.fromtimestamp(float(raw), tz=timezone.utc).date().isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _vendor_label(item: dict) -> str:
    return f"{item.get('vendor') or item.get('host') or 'unnamed'} ({item.get('category') or 'unclassified'})"


def _consent_test(name: str, d: dict) -> ConsentTest:
    tested = d.get("tested")
    if tested is None and name == "button_comparison" and "comparable" in d:
        tested = True                      # the comparison reports a verdict rather than a tested flag
    facts: list[tuple[str, str]] = []

    def vendors(items) -> str:
        v = sorted({_vendor_label(x) for x in items or [] if isinstance(x, dict)})
        return ", ".join(v) if v else "no analytics or advertising service contacted"

    if name in ("accept", "reject"):
        if tested:
            facts += [("Cookies in the browser after the choice", str(len(d.get("cookies") or []))), ("Analytics or advertising services contacted after the choice", vendors(d.get("non_essential"))),
                      ("Requests after the choice", str((d.get("requests") or {}).get("total_requests", "not recorded")))]
    elif name == "reject_persistence":
        if tested:
            facts.append(("Entry page after Reject", vendors(d.get("non_essential_after_reject"))))
            for p in d.get("further_pages") or []:
                facts.append((f"Further page {p.get('url', 'not recorded')}", vendors(p.get("non_essential")) if p.get("loaded", True) else "the page did not load"))
    elif name == "withdrawal":
        if tested or d.get("reopen_control"):
            facts += [("Control that reopened the choice", str(d.get("reopen_control") or "not found")), ("Control that withdrew consent", str(d.get("withdraw_control") or "not found"))]
        if tested:
            facts += [("Services started by Accept", vendors(d.get("accepted_non_essential"))), ("Entry page after withdrawal", vendors(d.get("after_withdrawal")))]
            for p in d.get("further_pages") or []:
                facts.append((f"Further page {p.get('url', 'not recorded')} after withdrawal", vendors(p.get("non_essential")) if p.get("loaded", True) else "the page did not load"))
    elif name == "purposes":
        for p in d.get("purposes") or []:
            facts.append((f"Purpose: {p.get('label') or p.get('kind') or 'unnamed'}", f"{vendors(p.get('observed'))}" + (f"; verdict {p['verdict']}" if p.get("verdict") else "")))
    elif name == "button_comparison":
        if "comparable" in d:
            facts.append(("Accept and Reject comparable in size and prominence", "yes" if d["comparable"] else "no"))
            if d.get("why"):
                facts.append(("Reason", str(d["why"])))
            for side in ("accept", "reject"):
                s = d.get(side)
                if isinstance(s, dict):
                    facts.append((f"{side.capitalize()} control", f"{s.get('w', '?')} x {s.get('h', '?')} px, font {s.get('font', '?')} px, {'filled' if s.get('filled') else 'not filled'}"))
    return ConsentTest(name=name, tested=bool(tested) if tested is not None else None, reason=d.get("reason"), facts=facts)


def assemble(raw: dict, *, generated_at: datetime) -> ReportData:
    s = raw["scan"]
    domain = s["domain"]
    declaration = raw["declaration"]
    scan = ScanFacts(
        scan_id=str(s["id"]), website_url=s["website_url"], domain=domain, crawl_status=s["status"], analysis_status=s["analysis_status"], created_at=s.get("created_at"),
        started_at=s.get("started_at"), completed_at=s.get("completed_at"), analysis_started_at=s.get("analysis_started_at"), analysis_completed_at=s.get("analysis_completed_at"),
        stop_reason=s.get("stop_reason"), error=s.get("error"), analysis_error=s.get("analysis_error"),
        previous_scan_id=str(s["previous_scan_id"]) if s.get("previous_scan_id") else None, options=dict(s.get("options") or {}))

    urls = UrlInventory(total=(declaration.get("urls") or {}).get("discovered"), shown=[
        UrlRow(url=u["original_url"], final_url=u.get("final_url"), depth=u.get("depth"), status=u["status"], analysis_status=u.get("analysis_status"), http_status=u.get("http_status"),
               status_reason=u.get("status_reason"), discovery_sources=list(u.get("discovery_sources") or [])) for u in raw.get("urls", [])])

    # ---- policies
    policies: list[PolicyRow] = []
    for p in raw.get("policies", []):
        title = re.sub(r"\s+", " ", p["title"]).strip() if p.get("title") else None
        source = "page title" if title else None
        if not title:
            first = next((ln.strip() for ln in (p.get("opening") or "").splitlines() if ln.strip()), None)
            title, source = (first[:150], "first line of the page text") if first else (None, None)
        policies.append(PolicyRow(url=p["source_url"], kinds=_policy_kinds(p["source_url"]) or ["Policy page"], title=title, title_source=source, content_hash=p.get("content_hash"), size=p.get("byte_size")))
    for p in raw.get("policy_docs", []):
        policies.append(PolicyRow(url=p["source_url"], kinds=_policy_kinds(p["source_url"]) or ["Policy document"], title=None, title_source=None, content_hash=p.get("content_hash"),
                                  size=int(p["size"]) if p.get("size") is not None else None, pages=int(p["pages"])))
    policies.sort(key=lambda r: (r.kinds[0], r.url))

    # ---- observations
    cookies: dict[str, CookieRow] = {}
    hosts: dict[str, HostRow] = {}
    forms: list[FormRow] = []
    iframes: list[IframeRow] = []
    sec_pages: dict[str, SecurityPage] = {}
    tls: dict | None = None
    consent_raw: dict[str, dict] = {}
    insecure_total, insecure_seen, insecure_examples = 0, False, []
    unreadable = 0
    network_pages: dict[str, dict] = {}

    def see_cookie(c: dict, page: str, state: str) -> None:
        domain_ = _norm_domain(c.get("domain"))
        ident = f"{c.get('name', '')}|{domain_}|{c.get('path') or '/'}"
        row = cookies.get(ident)
        if row is None:
            row = cookies[ident] = CookieRow(identity=ident, name=c.get("name", ""), domain=domain_, path=c.get("path") or "/", first_party=c.get("first_party"), secure=c.get("secure"),
                                             http_only=c.get("http_only"), same_site=c.get("same_site"), session=c.get("session"), expires=_cookie_expiry(c.get("expires")))
        _add(row.pages, page)
        _add(row.consent_states, state)

    def see_host(h: dict, page: str, state: str) -> None:
        host = (h.get("host") or "").lower()
        if not host:
            return
        row = hosts.setdefault(host, HostRow(host=host, third_party=bool(h.get("third_party")), requests=0, script_requests=0))
        row.requests += int(h.get("requests") or 0)
        row.script_requests += int((h.get("types") or {}).get("script", 0))
        for t, n in (h.get("types") or {}).items():
            row.types[t] = row.types.get(t, 0) + int(n)
        _add(row.pages, page)
        _add(row.consent_states, state)

    classified_hosts: dict[str, list[str]] = {}

    def see_non_essential(items, state: str) -> None:
        for it in items or []:
            if isinstance(it, dict) and it.get("host"):
                label = f"{it.get('category') or 'unclassified'} ({it.get('vendor') or it['host']}), {state.replace('_', ' ')}"
                _add(classified_hosts.setdefault(it["host"].lower(), []), label)

    pre_with, pre_vendors = 0, []
    for ev in raw.get("observations", []):
        doc = _json(ev["content"])
        if not isinstance(doc, dict):
            unreadable += 1
            continue
        kind, page = ev["kind"], doc.get("page_url") or ev["source_url"]
        if kind == "cookie":
            for c in doc.get("cookies") or []:
                see_cookie(c, page, doc.get("consent_state") or "not recorded")
        elif kind == "network":
            network_pages[page] = doc
            for h in doc.get("hosts") or []:
                see_host(h, page, doc.get("consent_state") or "not recorded")
            see_non_essential(doc.get("non_essential"), doc.get("consent_state") or "pre_consent")
            if doc.get("non_essential"):
                pre_with += 1
                for x in doc["non_essential"]:
                    _add(pre_vendors, _vendor_label(x))
            if "insecure_requests" in doc:
                insecure_seen = True
                insecure_total += int(doc["insecure_requests"] or 0)
                for e in doc.get("insecure_examples") or []:
                    _add(insecure_examples, e)
        elif kind == "form":
            for f in doc.get("forms") or []:
                forms.append(FormRow(page_url=f.get("page_url") or page, action=f.get("action") or page, action_host=f.get("action_host") or "", method=f.get("method") or "get",
                                     fields=list(f.get("fields") or []), collects=list(f.get("collects") or []), consent_controls=list(f.get("consent_controls") or []),
                                     privacy_link=f.get("privacy_link"), hidden_field_count=f.get("hidden_field_count")))
        elif kind == "header":
            if ev["detection_method"] == "tls_probe":
                tls = doc
            else:
                sec = doc.get("security") or {}
                present = sorted((sec.get("present") or {}).keys())
                sec_pages[page] = SecurityPage(page_url=page, https=sec.get("https"), present=present, missing=list(sec.get("missing") or []),
                                               redirects=len(doc["redirect_chain"]) if isinstance(doc.get("redirect_chain"), list) else None)
                for fr in (doc.get("inspection") or {}).get("iframes") or []:
                    src = fr.get("src") or ""
                    h = (urlparse(src).hostname or "").lower()
                    if src:
                        iframes.append(IframeRow(page_url=page, src=src, title=fr.get("title") or "", host=h, third_party=bool(h) and not _is_first_party(h, domain)))
        elif kind == "consent":
            consent_raw[doc.get("test") or ev["detection_method"].removeprefix("consent_test:")] = doc

    for name in ("accept", "reject"):
        d = consent_raw.get(name) or {}
        if d.get("tested"):
            state = d.get("state") or f"post_{name}"
            page = d.get("page_url") or scan.website_url
            for c in d.get("cookies") or []:
                see_cookie(c, page, state)
            for h in (d.get("requests") or {}).get("hosts") or []:
                see_host(h, page, state)
            see_non_essential(d.get("non_essential"), state)
    for r in hosts.values():
        r.classified = classified_hosts.get(r.host, [])
    consent = [_consent_test(n, consent_raw[n]) for n in CONSENT_TESTS if n in consent_raw] + [ConsentTest(n, None, "not_recorded") for n in CONSENT_TESTS if n not in consent_raw]
    consent.sort(key=lambda c: CONSENT_TESTS.index(c.name))

    checked = ["strict-transport-security", "content-security-policy", "x-content-type-options", "referrer-policy", "permissions-policy", "x-frame-options"]
    sec_list = sorted(sec_pages.values(), key=lambda p: p.page_url)
    security = SecurityResults(
        tls=tls, pages_total=len(sec_list), pages_https=sum(1 for p in sec_list if p.https), headers_checked=checked,
        header_counts={h: sum(1 for p in sec_list if h in p.present) for h in checked}, insecure_requests=insecure_total if insecure_seen else None,
        insecure_examples=insecure_examples[:5], pages=sec_list[:PAGE_ROWS_CAP])

    # ---- documents
    documents = [DocumentRow(url=d["original_url"], final_url=d.get("final_url"), content_type=d.get("content_type"), status=d["status"], extraction_status=d.get("extraction_status"),
                             extraction_method=d.get("extraction_method"), pages_read=int(d["pages_read"]) if d.get("pages_read") is not None else None,
                             analysis_status=d.get("analysis_status"), status_reason=d.get("status_reason")) for d in raw.get("documents", [])]

    # ---- score and parameters
    sr = raw.get("score")
    score: ScoreView | None = None
    lines: dict[str, dict] = {}
    if sr:
        res = sr.get("result") or {}
        score = ScoreView(
            id=str(sr["id"]), score=sr.get("score"), version=sr.get("version") or res.get("version") or "not recorded", basis=sr["basis"], coverage=int(sr["coverage"]),
            reason_no_score=res.get("reason_no_score"), points_earned=res.get("points_earned"), points_in_play=res.get("points_in_play"),
            not_yet_in_force=list(res.get("not_yet_in_force") or []), by_group=dict(res.get("by_group") or {}), needs_attention={k: list(v) for k, v in (res.get("needs_attention") or {}).items()},
            as_of=_iso(sr.get("as_of")) or res.get("as_of") or "not recorded", calculated_at=sr.get("created_at"), supporting_scale=res.get("supporting_scale"))
        lines = {x["parameter_id"]: x for x in res.get("lines") or []}
    assessed = {a["parameter_id"]: a for a in raw.get("assessments", [])}
    parameters: list[ParameterRow] = []
    for p in PARAMETERS:
        a, line = assessed.get(p.id), lines.get(p.id)
        state = line["state"] if line else (a["state"] if a else "not_assessed")
        parameters.append(ParameterRow(
            id=p.id, name=p.name, group=p.group, group_label=GROUP_LABELS[p.group], kind=p.kind, points=p.points, partial_allowed=p.partial_allowed, human_review_required=p.human_review,
            conditional=p.conditional, legal_refs=list(p.legal_refs), in_force=_iso(p.in_force), penalty_ceiling_cr=p.penalty_ceiling_cr, schedule_item=p.schedule_item, state=state,
            earned=line["earned"] if line else None, in_play=line["in_play"] if line else None, note=(a.get("note") or None) if a else None, confidence=a.get("confidence") if a else None,
            assessor=a.get("assessor") if a else None, reviewed_by=a.get("reviewed_by") if a else None, review_reason=a.get("review_reason") if a else None,
            evidence_refs=list(a.get("evidence_refs") or []) if a else [], adjustment=line.get("adjustment") if line else None))

    findings = [FindingRow(
        id=str(f["id"]), finding_key=f["finding_key"], parameter_id=f["parameter_id"], title=f["title"], description=f.get("description") or "", severity=f["severity"], state=f["state"],
        legal_refs=list(f.get("legal_refs") or []), affected_urls=list(f.get("affected_urls") or []), affected_count=int(f.get("affected_count") or 0),
        evidence_refs=list(f.get("evidence_refs") or []), expected=f.get("expected") or "", observed=f.get("observed") or "", reason=f.get("reason") or "", confidence=f.get("confidence"),
        recommendation=f.get("recommendation") or "", human_review=bool(f.get("human_review")), verification_status=f.get("verification_status") or "unverified",
        schedule_item=f.get("schedule_item"), penalty_ceiling_cr=f.get("penalty_ceiling_cr")) for f in raw.get("findings", [])]
    findings.sort(key=lambda f: (SEVERITY_ORDER.get(f.severity, 9), f.parameter_id, f.finding_key))

    exposure = sorted(
        (ExposureRow(p.id, p.state, p.schedule_item, p.penalty_ceiling_cr, p.legal_refs) for p in parameters
         if p.state in ("not_met", "partly_met") and p.kind == STATUTORY and p.penalty_ceiling_cr is not None),
        key=lambda e: (-e.maximum_penalty_crore, e.parameter_id))

    # ---- evidence references: who cites which record
    cited: dict[str, list[str]] = {}
    for p in parameters:
        for r in p.evidence_refs:
            _add(cited.setdefault(r, []), f"parameter {p.id}")
    for f in findings:
        for r in f.evidence_refs:
            _add(cited.setdefault(r, []), f"finding {f.finding_key}")
    by_id = {str(e["id"]): e for e in raw.get("evidence", [])}
    evidence: list[EvidenceRef] = []
    for ref in sorted(cited):
        u = _uuid_of(ref)
        e = by_id.get(u) if u else None
        evidence.append(EvidenceRef(ref=ref, evidence_id=u, kind=e["kind"] if e else None, source_url=e["source_url"] if e else None, page_number=e.get("page_number") if e else None,
                                    content_hash=e.get("content_hash") if e else None, detection_method=e.get("detection_method") if e else None, captured_at=e.get("captured_at") if e else None,
                                    cited_by=cited[ref]))

    return ReportData(
        generated_at=generated_at, scan=scan, declaration=declaration, completion=declaration.get("completion"), urls=urls, policies=policies,
        cookies=sorted(cookies.values(), key=lambda c: (c.name, c.domain, c.path)), hosts=sorted(hosts.values(), key=lambda h: (not h.third_party, -h.requests, h.host)),
        forms=sorted(forms, key=lambda f: (f.page_url, f.action)), iframes=sorted(iframes, key=lambda i: (i.page_url, i.src)), security=security, consent=consent,
        pre_consent=PreConsent(pages_observed=len(network_pages), pages_with_non_essential=pre_with, non_essential=sorted(pre_vendors)), documents=documents, parameters=parameters,
        score=score, findings=findings, evidence=evidence[:EVIDENCE_CAP], evidence_total=len(evidence), exposure=exposure, legal=load_legal_framework(),
        legal_review_status=LEGAL_REVIEW_STATUS, unreadable_observations=unreadable, pages_observed=len(network_pages))


def points_total() -> int:
    return TOTAL_POINTS
