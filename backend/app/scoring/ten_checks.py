"""The Privacy Readiness Score: ten predefined checks and the one score they give.

THE scoring engine. The dashboard, the PDF reports and both APIs read its result
(`GET /api/v1/consent/scans/{id}/score`, `GET /api/v1/consent-agent/scans/{id}/score`);
nothing else calculates a score. Methodology: docs/PRIVACY_READINESS_TEN_CHECKS.md.

    score % = marks awarded / applicable marks x 100   (one decimal, half up)
    Passed = 1, Partially Passed = 0.5, Failed = 0
    Not Assessed and Not Applicable carry no marks and are left out of the maximum.

Every check is assessed on its own evidence. A check that could not be run (the page did
not load, the policy text could not be read, the analysis did not run) is Not Assessed and
left out of the score, never scored 0: a page that could not be reached is not a failed
check. The wording never says the law was broken; a missing disclosure is "not evidenced".

Two layers, so a new check needs a change in one place only:

  build_inputs(...)  the scan's stored records -> `TenInputs` (the page accounting and the
                     consent comparison the checks read)
  assess(inputs)     the ten checks and the score. Pure: reads its input, invents nothing.

Ported from the browser engine (frontend/src/report/tenChecks.ts, scanPages.ts,
reportVisual.ts), and checked against it case by case: tests/test_ten_checks_parity.py
replays every input that engine was tested on and requires the identical result.

TO ADD A CHECK: write a `_check_<name>(inputs, ...) -> Draft` function, add it to the list in
`assess()` with the next id, add its advice to `ADVICE`, and add a test. The score, the
counts, the limitations and the recommendations pick it up from there.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit

METHODOLOGY = "privacy-readiness-10"

Result = str     # "Passed" | "Partially Passed" | "Failed" | "Not Assessed" | "Not Applicable"

MARKS: dict[str, float | None] = {
    "Passed": 1, "Partially Passed": 0.5, "Failed": 0, "Not Assessed": None, "Not Applicable": None,
}

#: The words printed beside a check for its kind. A technical observation or a best practice is never presented as a
#: required DPDP item.
KIND_LABEL = {"dpdp": "DPDP-related", "practice": "Best practice", "technical": "Technical check",
              "transparency": "Report transparency"}

#: What to do about a check that did not pass in full. Plain advice about the website's own text or behaviour; it never
#: says the law was broken.
ADVICE: dict[int, str] = {
    1: "Publish a privacy policy page that is linked from the homepage and can be read by the scan.",
    2: "List each category of personal data collected, including data collected automatically.",
    3: "State the purpose for which each category of personal data is used.",
    4: "Name the kinds of recipients data is shared with (processors, analytics and advertising providers, authorities).",
    5: "State a retention period or criterion for every category of personal data, not for one.",
    6: "Explain each right (access, correction and erasure, nomination) and how to exercise it.",
    7: "Give a grievance contact, a response period and a Data Protection Officer or business contact.",
    8: "Review the cookies and trackers observed and classify each as necessary, analytics or advertising.",
    9: "Confirm that analytics and advertising stay off before consent and after Reject, on every page with a banner.",
    10: "Make sure each finding cites the page and the passage it came from.",
}


# ── Inputs ──────────────────────────────────────────────────────────────────────────────────────────────────────────

@dataclass
class ScanPages:
    """The pages one scan visited, as the checks read them (frontend scanPages.ts, ScanPagesView)."""
    rows: list[dict]                     # {"url", "scanned"}
    scanned_count: int
    failed: list[dict]                   # {"url", "reason"}, tried and did not load
    skipped: list[dict]                  # {"reason", "count"}, found and not tried
    discovered: int | None
    stop_reason: str | None


@dataclass
class Consent:
    """Known analytics/marketing requests per consent state, and which states were really exercised."""
    known: list[int]                     # [before consent, after Accept, after Reject]
    accept_tested: bool
    reject_tested: bool


@dataclass
class Visual:
    pages_scanned: int
    pages_found: int | None
    consent: Consent


@dataclass
class TenInputs:
    """Everything the ten checks read. `evidence` and `policy_analysis` are the API's JSON shapes
    (ScanEvidenceResponse, PolicyAnalysisResponse); policy_analysis is None when the analysis did not run."""
    evidence: dict | None
    policy_analysis: dict | None
    scan_pages: ScanPages | None
    visual: Visual | None
    screenshot_page_url: str | None = None


# ── Small helpers, matching the browser engine's arithmetic and wording exactly ─────────────────────────────────────

def _js_round(v: float) -> float:
    """Math.round: half up (toward +infinity), not Python's half-to-even."""
    return math.floor(v + 0.5)


def _num(v: float) -> str:
    """A number as JavaScript's String() prints it: 10, 7.5, 33.33."""
    if v == int(v):
        return str(int(v))
    return repr(float(v))


def _fmt(n: float) -> str:
    return _num(_js_round(n * 100) / 100)


def round_score(v: float) -> float:
    """0 to 100 to one decimal, half up (not banker's rounding), so 31.25 prints as 31.3 and never as 31.2."""
    return math.floor(v * 10 + 0.5 + 1e-9) / 10


def marks_label(awarded: float | None, max_: float | None) -> str:
    return "N/A" if max_ is None else f"{_fmt(awarded or 0)} / {_fmt(max_)}"


def _en_in(n: int) -> str:
    """A whole number grouped the Indian way (toLocaleString("en-IN")): 1,23,456."""
    s = str(abs(int(n)))
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join([*parts, tail])
    return ("-" if n < 0 else "") + s


def _clip(t: str, n: int = 150) -> str:
    """A passage cut to `n` characters, at a word boundary when one is near, so a quotation never ends mid-word."""
    if len(t) <= n:
        return t
    cut = t[: n - 1]
    sp = cut.rfind(" ")
    return f"{(cut[:sp] if sp > n * 0.6 else cut).rstrip()}…"


def _s(n: int, one: str = "", many: str = "s") -> str:
    return one if n == 1 else many


def _first(*vals: Any) -> Any:
    """JavaScript's `a ?? b ?? c`: the first value that is not None."""
    for v in vals:
        if v is not None:
            return v
    return None


def _test(pattern: str, text: str | None) -> bool:
    return re.search(pattern, text or "", re.IGNORECASE) is not None


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


# ── Scoring ─────────────────────────────────────────────────────────────────────────────────────────────────────────

def score_checks(checks: list[dict]) -> dict:
    """The scoring, in one place: marks awarded and the applicable maximum, the score, the counts, and every figure as
    it is printed, so the table, the total and the score cannot disagree."""
    applicable = [c for c in checks if c["max"] is not None]
    awarded = sum((c["awarded"] or 0) for c in applicable)
    applicable_max = sum((c["max"] or 0) for c in applicable)
    score = None if applicable_max == 0 else round_score(awarded / applicable_max * 100)

    def count(r: str) -> int:
        return sum(1 for c in checks if c["result"] == r)

    counts = {"passed": count("Passed"), "partial": count("Partially Passed"), "failed": count("Failed"),
              "not_assessed": count("Not Assessed"), "not_applicable": count("Not Applicable")}
    if score is None:
        formula = "No check applies to this website, so no meaningful score can be calculated."
    else:
        na, nx = counts["not_applicable"], counts["not_assessed"]
        formula = (
            f"Score = marks awarded ÷ applicable marks × 100 = {_fmt(awarded)} ÷ {_fmt(applicable_max)} × 100 = {_fmt(score)}"
            + (f" ({na} check{_s(na)} not applicable, left out of the maximum)" if na else "")
            + (f" ({nx} check{_s(nx)} could not be assessed, left out of the maximum)" if nx else "")
        )
    return {
        "awarded": awarded, "applicable_max": applicable_max, "score": score, "counts": counts, "formula": formula,
        "total_text": "No applicable check" if score is None else f"{_fmt(awarded)} / {_fmt(applicable_max)}",
        "score_text": "No meaningful score" if score is None else f"{_fmt(score)}%",
    }


Draft = dict  # result, basis?, evidence?, source_url?, explanation, completed?, limitation?, quality?, coverage?


def _make(id_: int, name: str, legal: str | None, kind: str, d0: Draft) -> dict:
    # A check that could not be run to the end is Not Assessed: left out of the score, not scored 0.
    d = {**d0, "result": "Not Assessed"} if d0["result"] == "Failed" and d0.get("completed") is False else d0
    result = d["result"]
    m = MARKS[result]
    basis = d.get("basis") or (
        "Verified" if result == "Passed" else "Partially verified" if result == "Partially Passed"
        else "Not applicable" if result == "Not Applicable" else "Evidence unavailable" if d.get("completed") is False
        else "Not evidenced"
    )
    quality = d.get("quality") or (
        "Specific" if result == "Passed" else "Limited" if result == "Partially Passed"
        else "Not assessed" if result == "Not Assessed" else "n/a" if result == "Not Applicable" else "None"
    )
    completed = False if result == "Not Assessed" else _first(d.get("completed"), True)
    return {
        "id": id_, "name": name, "result": result, "awarded": m, "max": None if m is None else 1,
        "evidence": d.get("evidence") or "", "source_url": d.get("source_url"), "explanation": d["explanation"],
        "legal": legal, "completed": completed, "limitation": d.get("limitation"),
        "marks_text": "Not assessed" if result == "Not Assessed" else marks_label(m, None if m is None else 1),
        "kind": kind, "kind_label": KIND_LABEL[kind], "basis": basis, "quality": quality,
        "coverage": d.get("coverage"),
    }


# ── The policy-content checks ────────────────────────────────────────────────────────────────────────────────────────

NO_POLICY_WORDS = ("No privacy policy text was available to read, so this could not be evidenced "
                   "(not a finding that the law was broken).")


def _find(a: dict | None, req_id: str) -> dict | None:
    if not a:
        return None
    return next((r for r in a.get("requirements") or [] if r.get("id") == req_id), None)


def _is_verified(r: dict | None) -> bool:
    """Verified only when it carries the quoted passage it was verified by: a "verified" status with no quote is not evidence."""
    return bool(r) and r.get("status") == "verified" and len((r.get("quote") or "").strip()) > 0


def _read(a: dict | None) -> bool:
    return bool(a) and a.get("status") == "analysed" and len(a.get("requirements") or []) > 0


def _docs(a: dict | None) -> list[dict]:
    return (a or {}).get("documents") or []


def _policy_check(a: dict | None, req_id: str, what: str, source_limit: str | None, found_url: str | None = None,
                  unread: str = "Content extraction failed") -> Draft:
    """A check that rests on one policy requirement."""
    if a and a.get("status") == "not_run":
        return {"result": "Failed", "completed": False, "basis": "Assessment incomplete", "source_url": found_url,
                "explanation": "The policy analysis has not run for this scan, so what the policy says was not assessed.",
                "limitation": "The policy analysis has not run."}
    if not _read(a):
        if found_url:
            return {
                "result": "Failed", "completed": False, "source_url": found_url, "basis": unread,
                "explanation": (
                    "Policy page found, but the site does not allow it to be scanned: not evidenced (a limit of the scan, not a legal finding)."
                    if unread == "Crawling restricted" else
                    "Policy page found, but it did not load: not evidenced (a limit of the scan, not a legal finding)."
                    if unread == "Access failed" else
                    "Policy page found, but its text could not be read: not evidenced (a limit of the scan, not a legal finding)."
                ),
                "limitation": (
                    "The site restricts access to the privacy policy page." if unread == "Crawling restricted" else
                    "The privacy policy page did not load." if unread == "Access failed" else
                    "A privacy policy page was found but its text could not be read."
                ),
            }
        return {"result": "Failed", "completed": False, "basis": "Not evidenced", "explanation": NO_POLICY_WORDS,
                "limitation": source_limit}
    r = _find(a, req_id)
    if r is None:
        return {"result": "Failed", "completed": False, "explanation": f"The policy analysis did not assess {what}.",
                "limitation": source_limit}
    if r.get("status") == "verified" and not _is_verified(r):
        return {"result": "Failed", "completed": False, "basis": "Evidence unavailable", "source_url": r.get("source_url"),
                "explanation": "The analysis marked this as verified but gave no quoted passage, so it is not counted.",
                "limitation": source_limit}
    if r.get("status") == "verified":
        return {"result": "Passed", "evidence": _clip(r.get("quote") or ""), "source_url": r.get("source_url"),
                "explanation": f"The policy states {what}; the quoted passage was verified word for word against the policy text."}
    if r.get("status") == "partial":
        return {"result": "Partially Passed", "evidence": _clip(r.get("quote") or ""), "source_url": r.get("source_url"),
                "explanation": f"The policy says part of {what}: {r.get('detail') or 'not every element was found'}."}
    docs = _docs(a)
    if any(d.get("truncated") for d in docs):
        return {"result": "Failed", "basis": "Partially extracted",
                "explanation": f"{_cap(what)} was not found in the part of the policy text that was read; the text was cut at the storage limit, so the rest was not reviewed.",
                "source_url": docs[0].get("url") if docs else None, "limitation": source_limit}
    return {"result": "Failed",
            "explanation": f"{_cap(what)} was not found in the policy text that was read (not a finding that the law was broken; it may be given elsewhere).",
            "source_url": docs[0].get("url") if docs else None, "limitation": source_limit}


# Evidence QUALITY, as against evidence that merely exists: these read the verified passage itself and lower a Passed to
# Partially Passed with the gap stated. Documented rules of the methodology; none is specific to one website.
_GENERIC_SCOPE = (r"\b(personal (data|information|details)|your (data|information|details)|(data|information) (that )?we "
                  r"(collect|hold|process|receive)|all (of )?(your |the )?(data|information)|(we|company) (retain|keep|store|hold)s? "
                  r"(your|personal|the))\b")
_CATEGORY_ONLY = (r"\b(contact (form|us)|enquir(y|ies)|inquir(y|ies)|form submissions?|submissions?|newsletters?|subscribers?|"
                  r"job applications?|applicants?|resumes?|surveys?|support (tickets?|requests?)|server logs?|log files?|backups?|cookies?)\b")
_RECIPIENT_NAMED = (r"\b(analytics|advertis\w*|marketing|payment (processors?|providers?|gateways?)|cloud|hosting|(sub-?)?processors?|"
                    r"affiliates?|subsidiar\w+|group compan\w+|government|law enforcement|regulator\w*|authorit\w+|courts?|"
                    r"legal (process|obligations?|requirements?)|crm|email (service|delivery) providers?|shipping|logistics|auditors?|"
                    r"professional advis[eo]rs?)\b")


def _ascii(pattern: str, text: str) -> bool:
    # JavaScript's \w and \b are ASCII-only; Python's are Unicode unless told otherwise.
    return re.search(pattern, text, re.IGNORECASE | re.ASCII) is not None


def retention_is_category_only(quote: str) -> bool:
    """True when a retention passage names a period or criterion for one kind of data only."""
    return _ascii(_CATEGORY_ONLY, quote) and not _ascii(_GENERIC_SCOPE, quote)


def sharing_names_recipients(quote: str) -> bool:
    """True when a sharing passage names at least one kind of recipient (not only "service providers" or "third parties")."""
    return _ascii(_RECIPIENT_NAMED, quote)


def _pick_best(rs: list[dict | None]) -> dict | None:
    return next((r for r in rs if _is_verified(r)), None) or next((r for r in rs if r and r.get("status") == "partial"), None)


# ── The assessment ──────────────────────────────────────────────────────────────────────────────────────────────────

_LEGAL_PATH = r"/(privacy|cookie)[-_]?(policy|policies|notice|statement)?/?\Z"
_TRACKING = r"analytic|market|advert|track"
_LOADER = r"googletagmanager\.com/(gtm\.js|gtag/js)"
_DENIED = r"[?&]gcs=G100(&|\Z)"


def _path_of(u: str) -> str:
    try:
        return urlsplit(urljoin("https://x.invalid", u)).path or "/"
    except ValueError:
        return u


def _legal_first(items: list[dict]) -> list[dict]:
    return sorted(items, key=lambda x: 0 if re.search(_LEGAL_PATH, _path_of(x.get("url") or ""), re.IGNORECASE) else 1)


def _norm(u: str) -> str:
    u = u.lower()
    u = re.sub(r"^https?://", "", u, count=1)
    u = re.sub(r"^www\.", "", u, count=1)
    u = re.sub(r"[#?].*\Z", "", u, count=1, flags=re.DOTALL)
    return re.sub(r"/\Z", "", u, count=1)


def assess(inputs: TenInputs) -> dict:
    """The ten checks and the one score they give."""
    ev = inputs.evidence
    a = inputs.policy_analysis
    sp = inputs.scan_pages
    vis = inputs.visual
    scanned = _first(sp.scanned_count if sp else None, vis.pages_scanned if vis else None, 0)
    failed_pages = len(sp.failed) if sp else 0
    skipped_pages = sum(x["count"] for x in sp.skipped) if sp else 0
    disc = sp.discovered if sp else None
    page_note = (
        f"{scanned} page{_s(scanned)} of the entered domain {'was' if scanned == 1 else 'were'} scanned"
        f"{f' of {disc} found' if disc is not None else ''}; the text of pages other than the privacy policy is not kept."
        if scanned > 0 else None
    )
    ev_pages = (ev or {}).get("pages") or []
    ev_policies = (ev or {}).get("policies") or []
    docs = _docs(a)

    # 1. Privacy policy availability. The page at the policy's own address comes before a page that only mentions one.
    policies = _legal_first([p for p in ev_policies if _test("privacy", p.get("policy_type"))])
    privacy_docs = _legal_first([d for d in docs if _test("privacy", d.get("policy_type"))])
    doc = privacy_docs[0] if privacy_docs else (docs[0] if docs else None)
    # A privacy page the scan found a link to but could not load is not a missing policy: it is a page that could not be read.
    failed_policy = next((f for f in (sp.failed if sp else []) if _test("privacy|policy", f["url"])), None) or next(
        ({"url": p["url"], "reason": "Blocked by robots.txt"} for p in ev_pages
         if _test("privacy|policy", p.get("url")) and p.get("discovered_via") == "robots_disallowed"), None)
    policy_blocked = bool(failed_policy) and _test("robots|restrict|denied|forbidden|401|403", failed_policy["reason"])
    p0 = policies[0] if policies else None
    if policies or doc:
        if _read(a) or (doc and doc.get("characters", 0) > 0):
            chars = _en_in((doc or {}).get("characters") or 0)
            if (doc and doc.get("truncated")) or (p0 or {}).get("text_state") == "partial":
                c1: Draft = {"result": "Passed", "basis": "Partially extracted", "source_url": _first((p0 or {}).get("url"), (doc or {}).get("url")),
                             "evidence": f"Privacy policy page found ({chars} characters of text read).",
                             "explanation": "A privacy policy page was found and readable text extracted, but the text was cut at the storage limit, so the rest was not reviewed.",
                             "limitation": "Only the first part of a very long policy was read."}
            else:
                c1 = {"result": "Passed", "source_url": _first((p0 or {}).get("url"), (doc or {}).get("url")),
                      "evidence": f"Privacy policy page found{f' ({chars} characters of text read)' if doc else ''}.",
                      "explanation": "A privacy policy page was found on the scanned pages and its text was read."}
        else:
            not_run = bool(a) and a.get("status") == "not_run"
            c1 = {"result": "Partially Passed", "basis": "Assessment incomplete" if not_run else "Content extraction failed",
                  "source_url": (p0 or {}).get("url"), "evidence": "Privacy policy page found.",
                  "explanation": ("A privacy policy page was found, but the policy analysis has not run, so what it says was not assessed."
                                  if not_run else "A privacy policy page was found, but its text could not be read, so what it says could not be assessed."),
                  "limitation": "The policy text was not available."}
    elif failed_policy:
        reason = failed_policy["reason"]
        c1 = {"result": "Failed", "completed": False, "basis": "Crawling restricted" if policy_blocked else "Access failed",
              "source_url": failed_policy["url"],
              "explanation": (f"A privacy policy page was found at this address, but the site does not allow it to be scanned ({reason}), so it was not read. This is not a finding that the policy is missing."
                              if policy_blocked else f"A privacy policy page was found at this address but did not load ({reason}), so it could not be read."),
              "limitation": "The site restricts access to this page." if policy_blocked else "The privacy policy page did not load."}
    elif scanned == 0:
        c1 = {"result": "Failed", "completed": False, "basis": "Scan failed",
              "explanation": "The website could not be accessed, so no page could be scanned and the search for a privacy policy was not made.",
              "limitation": "No page loaded."}
    else:
        c1 = {"result": "Failed", "basis": "Not found in scanned pages",
              "explanation": (f"No privacy policy page was found after scanning {scanned} page{_s(scanned)}"
                              f"{f' of the {disc} found' if disc is not None else ''} on the entered domain (privacy and cookie pages are tried first). "
                              "It may exist on another domain or on a page the scan did not reach, so this is limited to the pages actually searched, "
                              "and is not a finding that the law was broken."),
              "limitation": _first(sp.stop_reason if sp else None, "Only the entered domain was searched.")}

    # 2-5. Policy content.
    no_pol = "No privacy policy text was available to read; the text of other pages is not kept."
    found_url = _first((p0 or {}).get("url"), (failed_policy or {}).get("url"))
    unread = ("Content extraction failed" if policies else
              ("Crawling restricted" if policy_blocked else "Access failed") if failed_policy else "Content extraction failed")
    c2 = _policy_check(a, "notice_data_itemised", "the personal data it collects", no_pol, found_url, unread)
    c3 = _policy_check(a, "notice_purposes", "why personal data is collected and used", no_pol, found_url, unread)
    c4 = _policy_check(a, "data_sharing", "sharing of personal data with processors and other third parties", no_pol, found_url, unread)
    c5 = _policy_check(a, "retention", "how long personal data is kept", no_pol, found_url, unread)

    ev_trackers = (ev or {}).get("trackers") or []
    ev_cookies = (ev or {}).get("cookies") or []
    sq = (_find(a, "data_sharing") or {}).get("quote") or ""
    if c4["result"] == "Passed" and not sharing_names_recipients(sq):
        seen = list(dict.fromkeys(
            t["vendor"] for t in ev_trackers if _test("analytic|market|advert", t.get("category")) and t.get("vendor")
        ))[:3]
        c4.update({
            "result": "Partially Passed", "quality": "General", "coverage": "Recipients not named",
            "explanation": ("The policy says data is shared with service providers, but the passage names no kind of recipient "
                            "(analytics, advertising, payment, hosting, authorities and so on), so who receives it is not established."
                            + (f" The scan observed {', '.join(seen)}, which the passage does not name." if seen else "")),
            "limitation": "Only the sharing passage the analysis verified was assessed; the policy may name recipients elsewhere.",
        })
    rq = (_find(a, "retention") or {}).get("quote") or ""
    if c5["result"] == "Passed" and retention_is_category_only(rq):
        c5.update({
            "result": "Partially Passed", "quality": "Limited", "coverage": "One kind of data only",
            "explanation": ("The policy gives a retention period for one kind of data only (for example form submissions). "
                            "Periods or criteria for the other personal data it collects are not evidenced."),
            "limitation": "Retention for the policy's other data categories is not established by the verified passage.",
        })

    # 6. User rights.
    if not _read(a):
        c6 = _policy_check(a, "rights_means", "the user rights and how to exercise them", no_pol, found_url, unread)
    else:
        means = _find(a, "rights_means")
        rights = [_find(a, i) for i in ("rights_access", "rights_correction_erasure", "rights_nominate")]
        rights_found = sum(1 for r in rights if _is_verified(r))
        best = _pick_best([means, *rights])
        named_right = next((r for r in rights if _is_verified(r)), None)
        shown = named_right or best
        if _is_verified(means) and rights_found >= 2:
            c6 = {"result": "Passed", "evidence": _clip((shown or {}).get("quote") or ""), "source_url": (shown or {}).get("source_url"),
                  "explanation": f"The policy names {rights_found} of 3 user rights and the means of making a request.",
                  "coverage": f"{rights_found} of 3 rights named"}
        elif best:
            pick = shown or best
            part = ("the means of making a request and one right, but fewer rights than are needed to say the rights are explained"
                    if _is_verified(means) and rights_found == 1 else
                    "the means of making a request, but no right is named" if _is_verified(means) else
                    "rights are named, but no means of making a request" if rights_found > 0 else
                    "some wording, not a complete statement of rights and the means")
            c6 = {"result": "Partially Passed", "evidence": _clip(pick.get("quote") or ""), "source_url": pick.get("source_url"),
                  "quality": "Limited", "coverage": f"{rights_found} of 3 rights named", "explanation": f"Only part is evidenced: {part}."}
        else:
            c6 = {"result": "Failed",
                  "explanation": "No statement of user rights or of how to exercise them was found in the policy text that was read. This is not a finding that the law was broken.",
                  "source_url": docs[0].get("url") if docs else None, "limitation": no_pol}

    # 7. Contact and grievance.
    if not _read(a):
        c7 = _policy_check(a, "grievance_contact", "a privacy contact or grievance mechanism", no_pol, found_url, unread)
    else:
        g, d, t = _find(a, "grievance_contact"), _find(a, "dpo_contact"), _find(a, "grievance_timeline")
        g_ok, d_ok, t_ok = _is_verified(g), _is_verified(d), _is_verified(t)
        best = _pick_best([g, d, t])
        if g_ok and (d_ok or t_ok):
            withwhat = ("a business contact and a response period" if d_ok and t_ok else
                        "a business contact of a Data Protection Officer or answering person" if d_ok else "a response period")
            c7 = {"result": "Passed", "evidence": _clip(g.get("quote") or ""), "source_url": g.get("source_url"),
                  "explanation": f"A grievance contact was found together with {withwhat}."}
        elif best:
            part = ("a grievance contact, but neither a response period nor a business contact" if g_ok else
                    "a business contact, but no grievance contact" if d_ok else "some wording, not a complete contact")
            c7 = {"result": "Partially Passed", "evidence": _clip(best.get("quote") or ""), "source_url": best.get("source_url"),
                  "explanation": f"Only part is evidenced: {part}."}
        else:
            c7 = {"result": "Failed",
                  "explanation": "No privacy contact, Data Protection Officer or grievance mechanism was found in the policy text that was read. This is not a finding that the law was broken: it may be published elsewhere.",
                  "source_url": docs[0].get("url") if docs else None, "limitation": no_pol}

    # 8. Cookie and tracker detection.
    detection_ran = scanned > 0 and bool(ev)
    unclassified = (sum(1 for c in ev_cookies if not c.get("category") and not c.get("vendor"))
                    + sum(1 for t in ev_trackers if not t.get("category") and not t.get("vendor")))
    names = ", ".join(list(dict.fromkeys([*(t["vendor"] for t in ev_trackers if t.get("vendor")), *(c.get("name") for c in ev_cookies)]))[:4])
    class_note = (f"{unclassified} observed item{_s(unclassified)} could not be classified as necessary, analytics or advertising."
                  if unclassified > 0 else None)
    cov_note = (f"Detection covers {scanned} of {disc} pages." if scanned > 0 and disc is not None and disc > scanned else None)
    lim8 = " ".join(x for x in ["Absence of a tracker is not proof that none exists under all conditions.", cov_note, class_note] if x)
    nc, nt = len(ev_cookies), len(ev_trackers)
    seen8 = ("No cookie or third-party request was observed on the scanned pages." if nc + nt == 0 else
             f"{nc} cookie{_s(nc)} and {nt} third-party request{_s(nt)} observed{f' ({names})' if names else ''}.")
    t0_url = ev_trackers[0].get("page_url") if ev_trackers else None
    if not detection_ran:
        c8: Draft = {"result": "Failed", "completed": False, "basis": "Scan failed" if scanned == 0 else "Evidence unavailable",
                     "explanation": ("No page loaded, so cookies and trackers could not be detected." if scanned == 0 else
                                     "The scan's cookie and tracker evidence was not loaded, so the detection could not be assessed."),
                     "limitation": "Detection could not be completed."}
    elif failed_pages > 0:
        c8 = {"result": "Partially Passed", "evidence": seen8, "source_url": t0_url,
              "explanation": f"Detection ran on the pages that loaded, but {failed_pages} page{_s(failed_pages)} did not load, so tracking there could not be observed.",
              "limitation": lim8}
    else:
        c8 = {"result": "Passed", "evidence": seen8, "source_url": t0_url,
              "explanation": "Cookies and third-party requests were detected on every scanned page. This records what the site does; it does not assess whether the policy discloses it.",
              "limitation": lim8, "coverage": f"{scanned} of {disc} pages" if cov_note else None}

    # 9. Consent behaviour.
    cons = vis.consent if vis else None
    signals = (ev or {}).get("consent_signals") or []
    sig = signals[0] if signals else None
    has_banner = bool(sig) and sig.get("mechanism_type") != "none" and sig.get("banner_displayed") is not False

    def track_cookie(state: str) -> int:
        return sum(1 for c in ev_cookies if _test(_TRACKING, c.get("category")) and state in (c.get("consent_states") or []))

    def track_req(state: str) -> int:
        return sum(1 for t in ev_trackers if _test(_TRACKING, t.get("category")) and state in (t.get("consent_states") or []))

    # A tag loader sends nothing about the visitor; a Google request in consent-denied mode (gcs=G100) is a cookieless ping.
    # Neither is the same as an analytics request made with consent, so they are counted apart.
    def sends(state: str) -> list[dict]:
        return [t for t in ev_trackers if _test(_TRACKING, t.get("category")) and state in (t.get("consent_states") or [])
                and not _test(_LOADER, t.get("script_src"))]

    def denied_only(state: str) -> bool:
        x = sends(state)
        return len(x) > 0 and all(_test(_DENIED, t.get("script_src")) for t in x)

    pre_cookies, reject_cookies = track_cookie("pre_consent"), track_cookie("post_reject")
    pre_tracking = pre_cookies + (len(sends("pre_consent")) if ev_trackers else track_req("pre_consent"))
    reject_tracking = reject_cookies + (len(sends("post_reject")) if ev_trackers else track_req("post_reject"))
    pre_denied = pre_cookies == 0 and denied_only("pre_consent")
    reject_denied = reject_cookies == 0 and denied_only("post_reject")
    any_tracking = any(_test(_TRACKING, c.get("category")) for c in ev_cookies) or any(_test(_TRACKING, t.get("category")) for t in ev_trackers)
    if not cons:
        c9: Draft = {"result": "Failed", "completed": False, "basis": "Evidence unavailable",
                     "explanation": "The consent states were not loaded, so Accept and Reject could not be compared.",
                     "limitation": "Consent behaviour could not be assessed."}
    else:
        def known(n: int) -> int:
            # With the request list in hand, what a request carries decides; the summary count only stands in without one.
            return 0 if ev_trackers else n

        k = cons.known
        pre_clean = known(k[0]) == 0 and pre_tracking == 0
        reject_clean = known(k[2]) == 0 and (not cons.reject_tested or reject_tracking == 0)
        line = (f"Analytics/marketing requests: before consent {k[0]}, after Accept {k[1] if cons.accept_tested else 'not tested'}, "
                f"after Reject {k[2] if cons.reject_tested else 'not tested'}. Analytics/marketing cookies and requests seen before consent: {pre_tracking}.")
        home = inputs.screenshot_page_url
        if not has_banner and not any_tracking and unclassified == 0 and k[0] == 0 and k[1] == 0 and k[2] == 0:
            c9 = {"result": "Not Applicable", "evidence": line, "source_url": home,
                  "explanation": "No consent banner was found and every cookie and request observed was classified as necessary or functional (none as analytics, marketing or advertising), so there was no consent interface to test."}
        elif not has_banner:
            c9 = {"result": "Failed", "basis": "Not tested", "evidence": line, "source_url": home,
                  "explanation": "Analytics, marketing or advertising cookies or requests were observed, but no consent banner was displayed to the scanner, so Accept and Reject could not be tested.",
                  "limitation": "A banner that was not shown to the scanner may still exist."}
        elif not cons.accept_tested and not cons.reject_tested:
            c9 = {"result": "Failed", "completed": False, "basis": "Not tested", "evidence": line, "source_url": home,
                  "explanation": "A banner was found, but neither Accept nor Reject could be clicked and confirmed, so only the state before consent was observed.",
                  "limitation": "Accept and Reject were not tested."}
        elif cons.accept_tested and cons.reject_tested and pre_clean and reject_clean:
            c9 = {"result": "Passed", "evidence": line, "source_url": home, "quality": "Limited", "coverage": "Homepage only",
                  "limitation": "Accept and Reject were tested once, on the homepage. Other pages and other tracking technologies were not tested, so this does not show that every consent requirement is met across the website.",
                  "explanation": "All three states were tested on the homepage: no analytics or marketing before consent, and none after Reject."}
        elif cons.accept_tested and cons.reject_tested and (pre_clean or pre_denied) and (reject_clean or reject_denied):
            c9 = {"result": "Partially Passed",
                  "evidence": (f"{line} Requests that carry data: {len(sends('pre_consent'))} before consent and {len(sends('post_reject'))} after Reject, "
                               "sent in consent-denied mode (gcs=G100); no analytics or marketing cookie was set before consent."),
                  "source_url": home,
                  "explanation": "Google tags sent requests before consent and after Reject in consent-denied mode, and no analytics or marketing cookie was set. The requests still reach Google, so this is not a clean result."}
        elif not pre_clean and not reject_clean:
            c9 = {"result": "Failed", "evidence": line, "source_url": home,
                  "explanation": "Analytics or marketing requests were observed before consent and again after Reject."}
        else:
            what = ("Analytics or marketing requests were observed before any consent action" if not pre_clean else
                    "Analytics or marketing requests were still observed after Reject" if not reject_clean else
                    "Only one of Accept and Reject could be tested")
            c9 = {"result": "Partially Passed", "evidence": line, "source_url": home, "explanation": f"{what}.",
                  "limitation": (f"{'Accept' if not cons.accept_tested else 'Reject'} was not tested."
                                 if not cons.accept_tested or not cons.reject_tested else None)}

    first = [
        _make(1, "Privacy Policy Availability", "DPDP Act s.5(1); Rules r.3(a), 3(b)", "dpdp", c1),
        _make(2, "Personal Data Collection Disclosure", "DPDP Act s.5(1)(i); Rules r.3(b)(i)", "dpdp", c2),
        _make(3, "Purpose of Data Processing", "DPDP Act s.5(1)(i); Rules r.3(b)(ii)", "dpdp", c3),
        _make(4, "Data Sharing and Third Parties", "DPDP Act s.8(2)", "practice", c4),
        _make(5, "Data Retention Disclosure", "DPDP Act s.8(7)", "practice", c5),
        _make(6, "User Rights Disclosure", "DPDP Act s.11, 12, 13, 14; Rules r.14(1)", "dpdp", c6),
        _make(7, "Contact and Grievance Mechanism", "DPDP Act s.8(9), 8(10), 13; Rules r.9, 14(3)", "dpdp", c7),
        _make(8, "Cookie and Tracker Detection", "Relates to DPDP Act s.6(1); Rules r.3", "technical", c8),
        _make(9, "Consent Behaviour", "Relates to DPDP Act s.6(1), 6(4); Rules r.3(c)(i)", "technical", c9),
    ]

    # 10. Evidence and transparency: can each positive finding of checks 1 to 9 be traced to a page the scan actually read?
    read_pages = {_norm(u) for u in [
        *(u for p in ev_pages if p.get("discovered_via") not in ("failed", "timeout", "robots_disallowed")
          for u in (p.get("url"), _first(p.get("final_url"), ""))),
        *(r["url"] for r in (sp.rows if sp else []) if r["scanned"]),
        *(d.get("url") for d in docs), *(p.get("url") for p in ev_policies), _first(inputs.screenshot_page_url, ""),
    ] if u}

    def in_scan(u: str | None) -> bool:
        return bool(u) and (len(read_pages) == 0 or _norm(u) in read_pages)

    gaps = [c["id"] for c in first if c["result"] == "Not Assessed"]
    rest = [c for c in first if c["result"] not in ("Not Applicable", "Not Assessed")]
    positive = [c for c in rest if c["result"] in ("Passed", "Partially Passed")]
    absence = len(rest) - len(positive)
    witnessed = sum(1 for c in positive if c["evidence"] != "" and in_scan(c["source_url"]))
    frac = 0 if not positive else witnessed / len(positive)
    gap_note = (f" Check{_s(len(gaps))} {', '.join(map(str, gaps))} could not be assessed and {'is' if len(gaps) == 1 else 'are'} listed under the limitations."
                if gaps else "")
    abs_note = (f" {absence} completed result{' records' if absence == 1 else 's record'} only what was searched for and not found, and {'is' if absence == 1 else 'are'} not counted as evidence."
                if absence > 0 else "")
    of_line = f"{witnessed} of {len(positive)} findings (checks 1 to 9; this check is not counted in its own evidence) trace to an evidence excerpt and a page the scan read."
    pages_lim = f"{failed_pages} page{_s(failed_pages)} did not load." if failed_pages > 0 else None
    if not ev:
        c10: Draft = {"result": "Failed", "completed": False, "basis": "Evidence unavailable",
                      "explanation": "The scan's evidence was not loaded, so the other results could not be traced to their source.",
                      "limitation": "Evidence unavailable."}
    elif not rest:
        c10 = {"result": "Failed", "completed": False, "basis": "Evidence unavailable",
               "explanation": f"None of the other checks could be assessed, so there is no result to trace to its source.{gap_note}",
               "limitation": "No check could be assessed."}
    elif not positive:
        c10 = {"result": "Failed", "evidence": "",
               "explanation": f"No finding rests on positive evidence: every completed result records only that something was not found.{abs_note}{gap_note}",
               "limitation": pages_lim}
    elif frac >= 0.8:
        c10 = {"result": "Passed", "evidence": of_line, "source_url": None, "quality": "Specific",
               "explanation": f"Each finding can be traced to its excerpt and to a page that was read.{abs_note}{gap_note}",
               "coverage": f"{scanned} of {disc} pages read" if scanned > 0 and disc is not None and disc > scanned else None}
    elif frac >= 0.5:
        c10 = {"result": "Partially Passed", "evidence": of_line, "quality": "Limited",
               "explanation": f"Some findings can be traced to a page that was read; the others carry no excerpt or name no page the scan read.{abs_note}{gap_note}",
               "limitation": pages_lim}
    else:
        c10 = {"result": "Failed", "evidence": of_line,
               "explanation": f"Most findings cannot be traced to a page the scan read, so they cannot be independently reviewed. Missing evidence is not proof of a violation.{abs_note}{gap_note}",
               "limitation": pages_lim}
    checks = [*first, _make(10, "Privacy Evidence and Transparency", None, "transparency", c10)]

    sc = score_checks(checks)
    limitations: list[str] = []
    if page_note:
        limitations.append(page_note)
    if failed_pages > 0:
        limitations.append(f"{failed_pages} page{_s(failed_pages)} did not load (see the page list).")
    if skipped_pages > 0:
        limitations.append(f"{skipped_pages} page{' was' if skipped_pages == 1 else 's were'} found but not scanned.")
    # Checks that could not be completed, grouped by the limit they share, so one limit is said once.
    by_limit: dict[str, list[int]] = {}
    for c in checks:
        if c["result"] == "Not Assessed":
            by_limit.setdefault(_first(c["limitation"], c["explanation"]), []).append(c["id"])
    for text, ids in by_limit.items():
        limitations.append(f"Check{_s(len(ids))} {', '.join(map(str, ids))} could not be assessed (left out of the score): {text}")
    discovered = _first(disc, vis.pages_found if vis else None)
    assessed_n = sum(1 for c in checks if c["max"] is not None)
    limited_basis = assessed_n < 5
    where = ((f"{scanned} of the {discovered} pages found" if discovered is not None and discovered > scanned
              else f"{scanned} {'page' if scanned == 1 else 'pages'}") if scanned > 0 else "no page")
    nx = sc["counts"]["not_assessed"]
    summary = ("No meaningful score: no check could be assessed on the evidence available. This is not a finding about the website."
               if sc["score"] is None else
               f"{sc['total_text']} marks across {assessed_n} assessed check{_s(assessed_n)}, from {where} on the entered domain."
               + (f" {nx} could not be assessed and {'is' if nx == 1 else 'are'} left out." if nx else "")
               + (" Fewer than half the checks could be assessed, so this score is provisional." if limited_basis else "")
               + " Consent was tested once, on the homepage. An indicative readiness reading, not a legal determination or certification.")

    recommendations = [{"id": c["id"], "text": ADVICE[c["id"]]} for c in checks if c["result"] in ("Partially Passed", "Failed")]
    if any(c["result"] == "Not Assessed" for c in checks):
        recommendations.append({"id": 0, "text": "Run the scan again when the pages it could not read are reachable, so the checks left out can be assessed."})

    return {
        "methodology": METHODOLOGY,
        "checks": checks, "awarded": sc["awarded"], "applicable_max": sc["applicable_max"], "score": sc["score"],
        "counts": sc["counts"], "formula": sc["formula"], "total_text": sc["total_text"], "score_text": sc["score_text"],
        "incomplete": nx, "limited_basis": limited_basis, "summary": summary, "recommendations": recommendations,
        "pages": {"discovered": discovered, "scanned": scanned, "failed": failed_pages, "skipped": skipped_pages},
        "limitations": limitations,
    }


# ── From the scan's stored records to the inputs ─────────────────────────────────────────────────────────────────────

_LOADED_VIA = {"seed", "link", "sitemap"}
STOP_REASON = {
    "queue_exhausted": "The scan stopped because no more pages of the entered domain were found to scan.",
    "page_limit": "The scan stopped because it reached the page limit.",
    "deadline": "The scan stopped at its time limit, so pages that were still queued were not scanned.",
    "slow_site": "The scan stopped early because the pages it tried kept timing out, so the site was too slow to scan more of it.",
    "attempt_limit": "The scan stopped because too many of the pages it tried could not be scanned (they failed, timed out, or led away from the entered domain).",
    "cancelled": "The scan was cancelled before every queued page was scanned.",
}
SKIP_REASON = {
    "robots_disallowed": "blocked by the site's robots.txt",
    "slow_site": "not tried: the site's pages kept timing out",
    "duplicate_url": "the same address as a page already queued",
    "redirected_to_already_scanned_page": "redirected to a page already scanned",
    "page_limit": "found after the page limit was reached",
    "time_limit": "found but not reached before the time limit",
    "scan_cancelled": "not reached because the scan was cancelled",
    "not_reached": "found but not reached",
    "subdomain": "on a subdomain or another host, outside the entered domain",
    "subdomain_not_scanned": "on a subdomain or another host, outside the entered domain",
}
_STATES = ("pre_consent", "post_accept", "post_reject")


def _page_status(p: dict) -> tuple[str, bool]:
    via = p.get("discovered_via")
    if via in _LOADED_VIA:
        return (f"Scanned (HTTP {p['http_status']})" if p.get("http_status") else "Scanned"), True
    if via == "timeout":
        return "Not scanned: timed out", False
    if via == "failed":
        return "Not scanned: failed to load", False
    if via == "robots_disallowed":
        return "Not scanned: blocked by robots.txt", False
    return "Recorded; load result not stored for this scan", False


def build_scan_pages(pages: list[dict] | None, meta: dict | None) -> ScanPages | None:
    if pages is None and meta is None:
        return None
    meta = meta or {}
    records = [(p, *_page_status(p)) for p in pages or []]
    rows = [{"url": p["url"], "scanned": scanned, "status": text} for p, text, scanned in records]
    loaded = sum(1 for r in rows if r["scanned"])
    scanned_count = _first(meta.get("pages_scanned"), loaded)
    found = meta.get("pages_discovered")
    skipped_by = meta.get("skipped_by_reason") or {}
    skipped = [{"reason": SKIP_REASON.get(reason, f"skipped ({reason.replace('_', ' ')})"), "count": n}
               for reason, n in skipped_by.items() if isinstance(n, (int, float)) and n > 0 and reason != "duplicate_url"]
    unattempted = meta.get("pages_not_attempted_budget_exhausted") or 0
    page_limit_skips = skipped_by.get("page_limit")
    if unattempted > 0 and not (isinstance(page_limit_skips, (int, float)) and page_limit_skips > 0):
        skipped.append({"reason": "found after the limit was reached", "count": unattempted})
    stop = meta.get("stop_reason")
    return ScanPages(
        rows=[{"url": r["url"], "scanned": r["scanned"]} for r in rows],
        scanned_count=scanned_count,
        failed=[{"url": r["url"], "reason": r["status"].replace("Not scanned: ", "", 1)}
                for r in rows if not r["scanned"] and r["status"].startswith("Not scanned")],
        skipped=skipped,
        discovered=None if found is None else max(found, scanned_count),
        stop_reason=STOP_REASON.get(stop) if stop else None,
    )


def build_visual(evidence: dict | None, meta: dict | None) -> Visual | None:
    if evidence is None:
        return None
    # A state in which nothing was clicked is the same page seen again, not what happens after a choice.
    accept_tested = not (meta is not None and meta.get("accept_interaction") != "clicked")
    reject_tested = not (meta is not None and meta.get("reject_interaction") != "clicked")
    trackers = evidence.get("trackers") or []
    known = [sum(1 for t in trackers if t.get("category") in ("analytics", "marketing") and s in (t.get("consent_states") or []))
             for s in _STATES]
    pages = _first((meta or {}).get("pages_scanned"), len(evidence.get("pages") or []))
    pages_found = max((meta or {}).get("pages_discovered") or 0, pages) if meta is not None and "pages_discovered" in meta else None
    return Visual(pages_scanned=pages, pages_found=pages_found,
                  consent=Consent(known=known, accept_tested=accept_tested, reject_tested=reject_tested))


def build_inputs(*, evidence: dict | None, meta: dict | None, policy_analysis: dict | None,
                 screenshot_page_url: str | None) -> TenInputs:
    """`evidence`, `meta` (the website_scan stage's metadata) and `policy_analysis` in their API JSON shapes."""
    return TenInputs(
        evidence=evidence,
        policy_analysis=None if not policy_analysis or policy_analysis.get("status") == "not_run" else policy_analysis,
        scan_pages=build_scan_pages((evidence or {}).get("pages") if evidence is not None else None, meta),
        visual=build_visual(evidence, meta),
        screenshot_page_url=screenshot_page_url,
    )
