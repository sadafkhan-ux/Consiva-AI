"""The ten-check privacy assessment and the Privacy Readiness Score: the one scoring engine.

Everything that prints a score or a check result (the web page, the email, the dashboard, every PDF) reads the result of `assess_ten_checks`, served by
`GET /api/v1/consent/scans/{id}/assessment`. Nothing else computes a score. It is a pure function of the scan's stored evidence and its policy analysis, so the same
stored scan always gives the same answer, whoever asks and whenever.

Marks: Passed 1, Partially Passed 0.5, Failed 0. Not Applicable and Not Assessed carry no marks and are left out of the maximum. Score = marks awarded / applicable
marks x 100, rounded half up to one decimal; none when nothing applies. A check that could not be run (a page that could not be reached, a policy that could not be
read) is Not Assessed, never scored 0. See docs/PRIVACY_READINESS_TEN_CHECKS.md for what each check needs for each result.

This module was ported from the browser engine it replaces (frontend report/tenChecks.ts) and is held to the same results by backend/tests/fixtures/ten_check_golden.json,
the recorded output of that engine on 159 scenarios (tests/test_ten_checks_parity.py). The few JavaScript behaviours that show in its text (round half up, number
printing, Indian digit grouping, ASCII-only word characters) are reproduced on purpose.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit

from app.scoring import disclosure

# ── JavaScript-compatible helpers ─────────────────────────────────────────────────────────────────────────────────────────────────

_F = re.IGNORECASE | re.ASCII          # JavaScript's \w and \b are ASCII; /i folds case


def _js_round(x: float) -> float:
    """Math.round: halves go up (Python's round() goes to even)."""
    return math.floor(x + 0.5)


def _num(n: float) -> str:
    """String(n) for the numbers this module prints: 9 not 9.0, 94.4 not 94.40000000000001."""
    if n == int(n):
        return str(int(n))
    return repr(n)


def _fmt(n: float) -> str:
    """The module's fmt(): two decimals at most, no trailing zeros."""
    return _num(_js_round(n * 100) / 100)


def round_score(v: float) -> float:
    """0 to 100, one decimal, half up (31.25 prints as 31.3)."""
    return math.floor(v * 10 + 0.5 + 1e-9) / 10


def _en_in(n: int) -> str:
    """Number.toLocaleString("en-IN") for whole numbers: 24,449 and 1,24,449 (the last three digits, then pairs)."""
    s = str(abs(int(n)))
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        head = re.sub(r"(\d)(?=(\d\d)+$)", r"\1,", head)
        s = f"{head},{tail}"
    return f"-{s}" if n < 0 else s


def format_score(score: float | None, unavailable: str = "No meaningful score") -> str:
    """The one way a score is written everywhere: the number out of 100, no percent sign."""
    return unavailable if score is None else f"{_fmt(score)}/100"


def _clip(t: str, n: int = 150) -> str:
    """A passage cut to n characters at a word boundary when one is near, so a quotation never ends mid-word."""
    if len(t) <= n:
        return t
    cut = t[: n - 1]
    sp = cut.rfind(" ")
    return f"{(cut[:sp] if sp > n * 0.6 else cut).rstrip()}…"


def _path_of(u: str) -> str:
    try:
        return urlsplit(urljoin("https://x.invalid", u)).path
    except ValueError:
        return u


def _plural(n: int, one: str, many: str) -> str:
    return one if n == 1 else many


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:]


# ── the marks ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

MARKS: dict[str, float | None] = {"Passed": 1, "Partially Passed": 0.5, "Failed": 0, "Not Assessed": None, "Not Applicable": None}

KIND_LABEL = {"dpdp": "DPDP-related", "practice": "Best practice", "technical": "Technical check", "transparency": "Report transparency"}

ADVICE = {
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


def marks_label(awarded: float | None, max_: float | None) -> str:
    return "N/A" if max_ is None else f"{_fmt(awarded or 0)} / {_fmt(max_)}"


@dataclass
class Draft:
    result: str
    explanation: str
    basis: str | None = None
    evidence: str | None = None
    source_url: str | None = None
    completed: bool | None = None
    limitation: str | None = None
    quality: str | None = None
    coverage: str | None = None


def _make(id_: int, name: str, legal: str | None, kind: str, d0: Draft) -> dict[str, Any]:
    # A check that could not be run to the end is Not Assessed: left out of the score, not scored 0.
    d = d0
    if d0.result == "Failed" and d0.completed is False:
        d = Draft(**{**d0.__dict__, "result": "Not Assessed"})
    m = MARKS[d.result]
    if d.basis is not None:
        basis = d.basis
    elif d.result == "Passed":
        basis = "Verified"
    elif d.result == "Partially Passed":
        basis = "Partially verified"
    elif d.result == "Not Applicable":
        basis = "Not applicable"
    else:
        basis = "Evidence unavailable" if d.completed is False else "Not evidenced"
    if d.quality is not None:
        quality = d.quality
    else:
        quality = {"Passed": "Specific", "Partially Passed": "Limited", "Not Assessed": "Not assessed", "Not Applicable": "n/a"}.get(d.result, "None")
    return {
        "kind": kind, "basis": basis, "quality": quality, "coverage": d.coverage,
        "id": id_, "name": name, "result": d.result, "awarded": m, "max": None if m is None else 1,
        "evidence": d.evidence if d.evidence is not None else "", "source_url": d.source_url, "explanation": d.explanation, "legal": legal,
        "completed": False if d.result == "Not Assessed" else (True if d.completed is None else d.completed), "limitation": d.limitation,
        "marks_text": "Not assessed" if d.result == "Not Assessed" else marks_label(m, None if m is None else 1),
    }


def score_checks(checks: list[dict[str, Any]]) -> dict[str, Any]:
    """The scoring in one place: marks awarded and the applicable maximum, the score, the counts, and every figure as it is printed."""
    applicable = [c for c in checks if c["max"] is not None]
    awarded = sum((c["awarded"] or 0) for c in applicable)
    applicable_max = sum((c["max"] or 0) for c in applicable)
    score = None if applicable_max == 0 else round_score((awarded / applicable_max) * 100)

    def count(r: str) -> int:
        return sum(1 for c in checks if c["result"] == r)

    counts = {"passed": count("Passed"), "partial": count("Partially Passed"), "failed": count("Failed"), "not_assessed": count("Not Assessed"), "not_applicable": count("Not Applicable")}
    if score is None:
        formula = "No check applies to this website, so no meaningful score can be calculated."
    else:
        na, nas = counts["not_applicable"], counts["not_assessed"]
        formula = (
            f"Score = marks awarded ÷ applicable marks × 100 = {_fmt(awarded)} ÷ {_fmt(applicable_max)} × 100 = {_fmt(score)}"
            + (f" ({na} check{_plural(na, '', 's')} not applicable, left out of the maximum)" if na else "")
            + (f" ({nas} check{_plural(nas, '', 's')} could not be assessed, left out of the maximum)" if nas else "")
        )
    return {
        "awarded": awarded, "applicable_max": applicable_max, "score": score, "counts": counts, "formula": formula,
        "total_text": "No applicable check" if score is None else f"{_fmt(awarded)} / {_fmt(applicable_max)}", "score_text": format_score(score),
    }


# ── what the policy analysis says ───────────────────────────────────────────────────────────────────────────────────────────────

NO_POLICY_WORDS = "No privacy policy text was available to read, so this could not be evidenced (not a finding that the law was broken)."
_LEGAL_DOC = re.compile(r"/(privacy|cookie)[-_]?(policy|policies|notice|statement)?/?$", _F)
_LEGAL_PATH = _LEGAL_DOC


def _find(a: dict | None, id_: str) -> dict | None:
    if not a:
        return None
    return next((r for r in a.get("requirements", []) if r.get("id") == id_), None)


def _is_verified(r: dict | None) -> bool:
    return bool(r) and r.get("status") == "verified" and len((r.get("quote") or "").strip()) > 0


def _read(a: dict | None) -> bool:
    return bool(a) and a.get("status") == "analysed" and len(a.get("requirements", [])) > 0


def _cut_policy_docs(a: dict) -> list[dict]:
    return [d for d in a.get("documents", []) if d.get("truncated") and _LEGAL_DOC.search(_path_of(d["url"]))]


def _cut_policy_draft(a: dict, what: str, source_limit: str | None) -> Draft:
    cut = _cut_policy_docs(a)[0]
    url_no_scheme = re.sub(r"^https?://", "", cut["url"], flags=re.IGNORECASE)
    return Draft(
        result="Failed", completed=False, basis="Partially extracted", source_url=cut["url"],
        explanation=f"{_cap(what)} was not found in the first {_en_in(cut['characters'])} characters of {url_no_scheme}, which is longer than the scan keeps; the rest was not reviewed, so this is not assessed.",
        limitation=source_limit if source_limit is not None else "Only the first part of a very long policy page was read.",
    )


def _policy_check(a: dict | None, req_id: str, what: str, source_limit: str | None, found_url: str | None = None, unread: str = "Content extraction failed") -> Draft:
    if a and a.get("status") == "not_run":
        return Draft(result="Failed", completed=False, basis="Assessment incomplete", source_url=found_url,
                     explanation="The policy analysis has not run for this scan, so what the policy says was not assessed.", limitation="The policy analysis has not run.")
    if not _read(a):
        if found_url:
            if unread == "Crawling restricted":
                expl = "Policy page found, but the site does not allow it to be scanned: not evidenced (a limit of the scan, not a legal finding)."
                lim = "The site restricts access to the privacy policy page."
            elif unread == "Access failed":
                expl = "Policy page found, but it did not load: not evidenced (a limit of the scan, not a legal finding)."
                lim = "The privacy policy page did not load."
            else:
                expl = "Policy page found, but its text could not be read: not evidenced (a limit of the scan, not a legal finding)."
                lim = "A privacy policy page was found but its text could not be read."
            return Draft(result="Failed", completed=False, source_url=found_url, basis=unread, explanation=expl, limitation=lim)
        return Draft(result="Failed", completed=False, basis="Not evidenced", explanation=NO_POLICY_WORDS, limitation=source_limit)
    r = _find(a, req_id)
    if not r:
        return Draft(result="Failed", completed=False, explanation=f"The policy analysis did not assess {what}.", limitation=source_limit)
    if r.get("status") == "verified" and not _is_verified(r):
        return Draft(result="Failed", completed=False, basis="Evidence unavailable", source_url=r.get("source_url"),
                     explanation="The analysis marked this as verified but gave no quoted passage, so it is not counted.", limitation=source_limit)
    if r.get("status") == "verified":
        return Draft(result="Passed", evidence=_clip(r["quote"]), source_url=r.get("source_url"),
                     explanation=f"The policy states {what}; the quoted passage was verified word for word against the policy text.")
    if r.get("status") == "partial":
        return Draft(result="Partially Passed", evidence=_clip(r["quote"]), source_url=r.get("source_url"),
                     explanation=f"The policy says part of {what}: {r.get('detail') or 'not every element was found'}.")
    if _cut_policy_docs(a):
        return _cut_policy_draft(a, what, source_limit)
    docs = a.get("documents", [])
    return Draft(result="Failed", explanation=f"{_cap(what)} was not found in the policy text that was read (not a finding that the law was broken; it may be given elsewhere).",
                 source_url=docs[0]["url"] if docs else None, limitation=source_limit)


_GENERIC_SCOPE = re.compile(r"\b(personal (data|information|details)|your (data|information|details)|(data|information) (that )?we (collect|hold|process|receive)|all (of )?(your |the )?(data|information)|(we|company) (retain|keep|store|hold)s? (your|personal|the))\b", _F)
_CATEGORY_ONLY = re.compile(r"\b(contact (form|us)|enquir(y|ies)|inquir(y|ies)|form submissions?|submissions?|newsletters?|subscribers?|job applications?|applicants?|resumes?|surveys?|support (tickets?|requests?)|server logs?|log files?|backups?|cookies?)\b", _F)
_RECIPIENT_NAMED = re.compile(r"\b(analytics|advertis\w*|marketing|payment (processors?|providers?|gateways?)|cloud|hosting|(sub-?)?processors?|affiliates?|subsidiar\w+|group compan\w+|government|law enforcement|regulator\w*|authorit\w+|courts?|legal (process|obligations?|requirements?)|crm|email (service|delivery) providers?|shipping|logistics|auditors?|professional advis[eo]rs?)\b", _F)


def retention_is_category_only(quote: str) -> bool:
    """True when a retention passage names a period or criterion for one kind of data only."""
    return bool(_CATEGORY_ONLY.search(quote)) and not _GENERIC_SCOPE.search(quote)


def sharing_names_recipients(quote: str) -> bool:
    """True when a sharing passage names at least one kind of recipient (not only "service providers" or "third parties")."""
    return bool(_RECIPIENT_NAMED.search(quote))


def _pick_best(rs: list[dict | None]) -> dict | None:
    return next((r for r in rs if _is_verified(r)), None) or next((r for r in rs if r and r.get("status") == "partial"), None)


# ── the assessment ────────────────────────────────────────────────────────────────────────────────────────────────────────────────

def assess_ten_checks(
    evidence: dict | None, policy_analysis: dict | None, scan_pages: dict | None, visual: dict | None, screenshot_page_url: str | None,
    cross_check: bool = True,
) -> dict[str, Any]:
    """The ten checks and the score for one scan.

    `evidence`: the scan's evidence as the API serves it (pages, policies, cookies, trackers, consent_signals). `policy_analysis`: the analysis result, or None when it has not run.
    `scan_pages`: scannedCount-style page accounting (see ten_inputs.build_scan_pages). `visual`: pages scanned/found and the consent comparison (ten_inputs.build_visual).
    `screenshot_page_url`: the address of the homepage the preview was taken of.
    `cross_check`: the 1.1.0 additions (scoring/disclosure.py): checks 2 to 5 compare what the scan observed the site collecting with what the policy says, check 7 lists the parts of the
    contact found, and check 9's evidence names what was seen in each consent state. Always on in the product;
    False reproduces the 1.0.0 results, which tests/test_ten_checks_parity.py holds to the recorded browser engine.
    """
    ev = evidence
    a = policy_analysis
    sp = scan_pages
    sp_failed = (sp or {}).get("failed") or []
    scanned = (sp or {}).get("scanned_count")
    if scanned is None:
        scanned = (visual or {}).get("pages_scanned")
    if scanned is None:
        scanned = 0
    failed_pages = len(sp_failed) if sp is not None else 0
    skipped_pages = sum(x["count"] for x in ((sp or {}).get("skipped") or []))
    discovered_sp = (sp or {}).get("discovered") if sp is not None else None
    page_note = (
        f"{scanned} page{_plural(scanned, '', 's')} of the entered domain {_plural(scanned, 'was', 'were')} scanned"
        f"{f' of {discovered_sp} found' if discovered_sp is not None else ''}; the text of pages other than the privacy policy is not kept."
        if scanned > 0 else None
    )

    # 1. Privacy policy availability
    def legal_first(items: list[dict]) -> list[dict]:
        return sorted(items, key=lambda x: 0 if _LEGAL_PATH.search(_path_of(x["url"])) else 1)

    policies = legal_first([p for p in ((ev or {}).get("policies") or []) if re.search(r"privacy", p.get("policy_type") or "", _F)])
    a_docs = (a or {}).get("documents", []) if a else []
    privacy_docs = legal_first([d for d in a_docs if re.search(r"privacy", d.get("policy_type") or "", _F)])
    doc = privacy_docs[0] if privacy_docs else (a_docs[0] if a_docs else None)
    failed_policy = next((f for f in sp_failed if re.search(r"privacy|policy", f["url"], _F)), None)
    if failed_policy is None:
        robots = [{"url": p["url"], "reason": "Blocked by robots.txt"} for p in ((ev or {}).get("pages") or [])
                  if re.search(r"privacy|policy", p["url"], _F) and p.get("discovered_via") == "robots_disallowed"]
        failed_policy = robots[0] if robots else None
    policy_blocked = bool(failed_policy) and bool(re.search(r"robots|restrict|denied|forbidden|401|403", failed_policy["reason"], _F))
    first_pol_url = policies[0]["url"] if policies else None
    if policies or doc:
        if _read(a) or (doc and doc.get("characters", 0) > 0):
            if (doc and doc.get("truncated")) or (policies and policies[0].get("text_state") == "partial"):
                c1 = Draft(result="Passed", basis="Partially extracted", source_url=first_pol_url or (doc["url"] if doc else None),
                           evidence=f"Privacy policy page found ({_en_in((doc or {}).get('characters', 0))} characters of text read).",
                           explanation="A privacy policy page was found and readable text extracted, but the text was cut at the storage limit, so the rest was not reviewed.",
                           limitation="Only the first part of a very long policy was read.")
            else:
                c1 = Draft(result="Passed", source_url=first_pol_url or (doc["url"] if doc else None),
                           evidence=f"Privacy policy page found{f' ({_en_in(doc['characters'])} characters of text read)' if doc else ''}.",
                           explanation="A privacy policy page was found on the scanned pages and its text was read.")
        else:
            not_run = bool(a) and a.get("status") == "not_run"
            c1 = Draft(result="Partially Passed", basis="Assessment incomplete" if not_run else "Content extraction failed", source_url=first_pol_url,
                       evidence="Privacy policy page found.",
                       explanation="A privacy policy page was found, but the policy analysis has not run, so what it says was not assessed." if not_run
                       else "A privacy policy page was found, but its text could not be read, so what it says could not be assessed.",
                       limitation="The policy text was not available.")
    elif failed_policy:
        reason = failed_policy["reason"]
        c1 = Draft(result="Failed", completed=False, basis="Crawling restricted" if policy_blocked else "Access failed", source_url=failed_policy["url"],
                   explanation=(f"A privacy policy page was found at this address, but the site does not allow it to be scanned ({reason}), so it was not read. This is not a finding that the policy is missing."
                                if policy_blocked else f"A privacy policy page was found at this address but did not load ({reason}), so it could not be read."),
                   limitation="The site restricts access to this page." if policy_blocked else "The privacy policy page did not load.")
    elif scanned == 0:
        c1 = Draft(result="Failed", completed=False, basis="Scan failed", explanation="The website could not be accessed, so no page could be scanned and the search for a privacy policy was not made.", limitation="No page loaded.")
    else:
        stop = (sp or {}).get("stop_reason")
        c1 = Draft(result="Failed", basis="Not found in scanned pages",
                   explanation=(f"No privacy policy page was found after scanning {scanned} page{_plural(scanned, '', 's')}{f' of the {discovered_sp} found' if discovered_sp is not None else ''} on the entered domain "
                                "(privacy and cookie pages are tried first). It may exist on another domain or on a page the scan did not reach, so this is limited to the pages actually searched, and is not a finding that the law was broken."),
                   limitation=stop if stop is not None else "Only the entered domain was searched.")

    # 2-5 policy content
    no_pol = "No privacy policy text was available to read; the text of other pages is not kept."
    found_url = first_pol_url or (failed_policy["url"] if failed_policy else None)
    unread = "Content extraction failed" if policies else (("Crawling restricted" if policy_blocked else "Access failed") if failed_policy else "Content extraction failed")
    c2 = _policy_check(a, "notice_data_itemised", "the personal data it collects", no_pol, found_url, unread)
    c3 = _policy_check(a, "notice_purposes", "why personal data is collected and used", no_pol, found_url, unread)
    c4 = _policy_check(a, "data_sharing", "sharing of personal data with processors and other third parties", no_pol, found_url, unread)
    c5 = _policy_check(a, "retention", "how long personal data is kept", no_pol, found_url, unread)
    trackers = (ev or {}).get("trackers") or []
    cookies = (ev or {}).get("cookies") or []
    sq = ((_find(a, "data_sharing") or {}).get("quote")) or ""
    if c4.result == "Passed" and not sharing_names_recipients(sq):
        seen = list(dict.fromkeys(t["vendor"] for t in trackers if re.search(r"analytic|market|advert", t.get("category") or "", _F) and t.get("vendor")))[:3]
        c4.result = "Partially Passed"
        c4.quality = "General"
        c4.coverage = "Recipients not named"
        c4.explanation = ("The policy says data is shared with service providers, but the passage names no kind of recipient (analytics, advertising, payment, hosting, authorities and so on), so who receives it is not established."
                          + (f" The scan observed {', '.join(seen)}, which the passage does not name." if seen else ""))
        c4.limitation = "Only the sharing passage the analysis verified was assessed; the policy may name recipients elsewhere."
    rq = ((_find(a, "retention") or {}).get("quote")) or ""
    if c5.result == "Passed" and retention_is_category_only(rq):
        c5.result = "Partially Passed"
        c5.quality = "Limited"
        c5.coverage = "One kind of data only"
        c5.explanation = "The policy gives a retention period for one kind of data only (for example form submissions). Periods or criteria for the other personal data it collects are not evidenced."
        c5.limitation = "Retention for the policy's other data categories is not established by the verified passage."
    if cross_check:
        disclosure.cross_check(a, ev, c2, c3, c4, c5)

    # 6. user rights
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
            c6 = Draft(result="Passed", evidence=_clip((shown or {}).get("quote") or ""), source_url=(shown or {}).get("source_url"),
                       explanation=f"The policy names {rights_found} of 3 user rights and the means of making a request.", coverage=f"{rights_found} of 3 rights named")
        elif best:
            sh = shown or best
            if _is_verified(means) and rights_found == 1:
                why = "the means of making a request and one right, but fewer rights than are needed to say the rights are explained"
            elif _is_verified(means):
                why = "the means of making a request, but no right is named"
            elif rights_found > 0:
                why = "rights are named, but no means of making a request"
            else:
                why = "some wording, not a complete statement of rights and the means"
            c6 = Draft(result="Partially Passed", evidence=_clip(sh["quote"]), source_url=sh.get("source_url"), quality="Limited", coverage=f"{rights_found} of 3 rights named",
                       explanation=f"Only part is evidenced: {why}.")
        elif _cut_policy_docs(a):
            c6 = _cut_policy_draft(a, "a statement of user rights and how to exercise them", no_pol)
        else:
            c6 = Draft(result="Failed", explanation="No statement of user rights or of how to exercise them was found in the policy text that was read. This is not a finding that the law was broken.",
                       source_url=a["documents"][0]["url"] if a.get("documents") else None, limitation=no_pol)

    # 7. contact and grievance
    if not _read(a):
        c7 = _policy_check(a, "grievance_contact", "a privacy contact or grievance mechanism", no_pol, found_url, unread)
    else:
        g, d, t = _find(a, "grievance_contact"), _find(a, "dpo_contact"), _find(a, "grievance_timeline")
        g_ok, d_ok, t_ok = _is_verified(g), _is_verified(d), _is_verified(t)
        best = _pick_best([g, d, t])
        if g_ok and (d_ok or t_ok):
            who = "a business contact and a response period" if (d_ok and t_ok) else ("a business contact of a Data Protection Officer or answering person" if d_ok else "a response period")
            c7 = Draft(result="Passed", evidence=_clip(g["quote"]), source_url=g.get("source_url"), explanation=f"A grievance contact was found together with {who}.")
        elif best:
            why = ("a grievance contact, but neither a response period nor a business contact" if g_ok else ("a business contact, but no grievance contact" if d_ok else "some wording, not a complete contact"))
            c7 = Draft(result="Partially Passed", evidence=_clip(best["quote"]), source_url=best.get("source_url"), explanation=f"Only part is evidenced: {why}.")
        elif _cut_policy_docs(a):
            c7 = _cut_policy_draft(a, "a privacy contact, Data Protection Officer or grievance mechanism", no_pol)
        else:
            c7 = Draft(result="Failed", explanation="No privacy contact, Data Protection Officer or grievance mechanism was found in the policy text that was read. This is not a finding that the law was broken: it may be published elsewhere.",
                       source_url=a["documents"][0]["url"] if a.get("documents") else None, limitation=no_pol)

    if cross_check:
        disclosure.contact_advisory(a, c7)

    # 8. cookie and tracker detection
    detection_ran = scanned > 0 and ev is not None
    unclassified = sum(1 for c in cookies if not c.get("category") and not c.get("vendor")) + sum(1 for t in trackers if not t.get("category") and not t.get("vendor"))
    names = ", ".join(list(dict.fromkeys([t["vendor"] for t in trackers if t.get("vendor")] + [c["name"] for c in cookies]))[:4])
    class_note = f"{unclassified} observed item{_plural(unclassified, '', 's')} could not be classified as necessary, analytics or advertising." if unclassified > 0 else None
    cov_note = f"Detection covers {scanned} of {discovered_sp} pages." if scanned > 0 and discovered_sp is not None and discovered_sp > scanned else None
    lim8 = " ".join(x for x in ["Absence of a tracker is not proof that none exists under all conditions.", cov_note, class_note] if x)
    if len(cookies) + len(trackers) == 0:
        seen8 = "No cookie or third-party request was observed on the scanned pages."
    else:
        seen8 = f"{len(cookies)} cookie{_plural(len(cookies), '', 's')} and {len(trackers)} third-party request{_plural(len(trackers), '', 's')} observed{f' ({names})' if names else ''}."
    tracker_page = trackers[0].get("page_url") if trackers else None
    if not detection_ran:
        c8 = Draft(result="Failed", completed=False, basis="Scan failed" if scanned == 0 else "Evidence unavailable",
                   explanation="No page loaded, so cookies and trackers could not be detected." if scanned == 0 else "The scan's cookie and tracker evidence was not loaded, so the detection could not be assessed.",
                   limitation="Detection could not be completed.")
    elif failed_pages > 0:
        c8 = Draft(result="Partially Passed", evidence=seen8, source_url=tracker_page,
                   explanation=f"Detection ran on the pages that loaded, but {failed_pages} page{_plural(failed_pages, '', 's')} did not load, so tracking there could not be observed.", limitation=lim8)
    else:
        c8 = Draft(result="Passed", evidence=seen8, source_url=tracker_page,
                   explanation="Cookies and third-party requests were detected on every scanned page. This records what the site does; it does not assess whether the policy discloses it.",
                   limitation=lim8, coverage=f"{scanned} of {discovered_sp} pages" if cov_note else None)

    # 9. consent behaviour
    cons = (visual or {}).get("consent") if visual is not None else None
    signals = (ev or {}).get("consent_signals") or []
    sig = signals[0] if signals else None
    has_banner = bool(sig) and sig.get("mechanism_type") != "none" and sig.get("banner_displayed") is not False
    tracking = re.compile(r"analytic|market|advert|track", _F)
    loader = re.compile(r"googletagmanager\.com/(gtm\.js|gtag/js)", _F)
    denied = re.compile(r"[?&]gcs=G100(&|$)", _F)

    def track_cookie(state: str) -> int:
        return sum(1 for c in cookies if tracking.search(c.get("category") or "") and state in c.get("consent_states", []))

    def track_req(state: str) -> int:
        return sum(1 for t in trackers if tracking.search(t.get("category") or "") and state in t.get("consent_states", []))

    def sends(state: str) -> list[dict]:
        return [t for t in trackers if tracking.search(t.get("category") or "") and state in t.get("consent_states", []) and not loader.search(t.get("script_src") or "")]

    def denied_only(state: str) -> bool:
        x = sends(state)
        return len(x) > 0 and all(denied.search(t.get("script_src") or "") for t in x)

    pre_cookies, reject_cookies = track_cookie("pre_consent"), track_cookie("post_reject")
    pre_tracking = pre_cookies + (len(sends("pre_consent")) if trackers else track_req("pre_consent"))
    reject_tracking = reject_cookies + (len(sends("post_reject")) if trackers else track_req("post_reject"))
    pre_denied = pre_cookies == 0 and denied_only("pre_consent")
    reject_denied = reject_cookies == 0 and denied_only("post_reject")
    any_tracking = any(tracking.search(c.get("category") or "") for c in cookies) or any(tracking.search(t.get("category") or "") for t in trackers)
    if not cons:
        c9 = Draft(result="Failed", completed=False, basis="Evidence unavailable", explanation="The consent states were not loaded, so Accept and Reject could not be compared.", limitation="Consent behaviour could not be assessed.")
    else:
        known_ = cons["known"]

        def known(n: int) -> int:
            return 0 if trackers else n

        accept_t, reject_t = cons["accept_tested"], cons["reject_tested"]
        pre_clean = known(known_[0]) == 0 and pre_tracking == 0
        reject_clean = known(known_[2]) == 0 and (not reject_t or reject_tracking == 0)
        line = (f"Analytics/marketing requests: before consent {known_[0]}, after Accept {known_[1] if accept_t else 'not tested'}, after Reject {known_[2] if reject_t else 'not tested'}. "
                f"Analytics/marketing cookies and requests seen before consent: {pre_tracking}.")
        if cross_check:
            line = f"{line} {disclosure.consent_events(ev, accept_t, reject_t)}"
        home = screenshot_page_url
        if not has_banner and not any_tracking and unclassified == 0 and known_[0] == 0 and known_[1] == 0 and known_[2] == 0:
            c9 = Draft(result="Not Applicable", evidence=line, source_url=home,
                       explanation="No consent banner was found and every cookie and request observed was classified as necessary or functional (none as analytics, marketing or advertising), so there was no consent interface to test.")
        elif not has_banner:
            c9 = Draft(result="Failed", basis="Not tested", evidence=line, source_url=home,
                       explanation="Analytics, marketing or advertising cookies or requests were observed, but no consent banner was displayed to the scanner, so Accept and Reject could not be tested.",
                       limitation="A banner that was not shown to the scanner may still exist.")
        elif not accept_t and not reject_t:
            c9 = Draft(result="Failed", completed=False, basis="Not tested", evidence=line, source_url=home,
                       explanation="A banner was found, but neither Accept nor Reject could be clicked and confirmed, so only the state before consent was observed.", limitation="Accept and Reject were not tested.")
        elif accept_t and reject_t and pre_clean and reject_clean:
            c9 = Draft(result="Passed", evidence=line, source_url=home, quality="Limited", coverage="Homepage only",
                       limitation="Accept and Reject were tested once, on the homepage. Other pages and other tracking technologies were not tested, so this does not show that every consent requirement is met across the website.",
                       explanation="All three states were tested on the homepage: no analytics or marketing before consent, and none after Reject.")
        elif accept_t and reject_t and (pre_clean or pre_denied) and (reject_clean or reject_denied):
            c9 = Draft(result="Partially Passed",
                       evidence=f"{line} Requests that carry data: {len(sends('pre_consent'))} before consent and {len(sends('post_reject'))} after Reject, sent in consent-denied mode (gcs=G100); no analytics or marketing cookie was set before consent.",
                       source_url=home,
                       explanation="Google tags sent requests before consent and after Reject in consent-denied mode, and no analytics or marketing cookie was set. The requests still reach Google, so this is not a clean result.")
        elif not pre_clean and not reject_clean:
            c9 = Draft(result="Failed", evidence=line, source_url=home, explanation="Analytics or marketing requests were observed before consent and again after Reject.")
        else:
            if not pre_clean:
                why = "Analytics or marketing requests were observed before any consent action"
            elif not reject_clean:
                why = "Analytics or marketing requests were still observed after Reject"
            else:
                why = "Only one of Accept and Reject could be tested"
            c9 = Draft(result="Partially Passed", evidence=line, source_url=home, explanation=f"{why}.",
                       limitation=(f"{'Accept' if not accept_t else 'Reject'} was not tested." if (not accept_t or not reject_t) else None))

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

    # 10. evidence and transparency: can each positive finding of checks 1 to 9 be traced to a page the scan actually read? Check 10 is not counted in its own evidence.
    def norm(u: str) -> str:
        u = u.lower()
        u = re.sub(r"^https?://", "", u)
        u = re.sub(r"^www\.", "", u)
        u = re.sub(r"[#?].*$", "", u)
        return re.sub(r"/$", "", u)

    pages_ev = (ev or {}).get("pages") or []
    read_pages: set[str] = set()
    for p in pages_ev:
        if p.get("discovered_via") not in ("failed", "timeout", "robots_disallowed"):
            for u in (p["url"], p.get("final_url") or ""):
                if u:
                    read_pages.add(norm(u))
    for r in ((sp or {}).get("rows") or []):
        if r.get("scanned") and r["url"]:
            read_pages.add(norm(r["url"]))
    for d in a_docs:
        if d.get("url"):
            read_pages.add(norm(d["url"]))
    for p in ((ev or {}).get("policies") or []):
        if p.get("url"):
            read_pages.add(norm(p["url"]))
    if screenshot_page_url:
        read_pages.add(norm(screenshot_page_url))

    def in_scan(u: str | None) -> bool:
        return bool(u) and (len(read_pages) == 0 or norm(u) in read_pages)

    gaps = [c["id"] for c in first if c["result"] == "Not Assessed"]
    rest = [c for c in first if c["result"] not in ("Not Applicable", "Not Assessed")]
    positive = [c for c in rest if c["result"] in ("Passed", "Partially Passed")]
    absence = len(rest) - len(positive)
    witnessed = sum(1 for c in positive if c["evidence"] != "" and in_scan(c["source_url"]))
    frac = 0 if not positive else witnessed / len(positive)
    gap_note = (f" Check{_plural(len(gaps), '', 's')} {', '.join(str(g) for g in gaps)} could not be assessed and {_plural(len(gaps), 'is', 'are')} listed under the limitations." if gaps else "")
    abs_note = (f" {absence} completed result{' records' if absence == 1 else 's record'} only what was searched for and not found, and {_plural(absence, 'is', 'are')} not counted as evidence." if absence > 0 else "")
    of_line = f"{witnessed} of {len(positive)} findings (checks 1 to 9; this check is not counted in its own evidence) trace to an evidence excerpt and a page the scan read."
    pages_did_not_load = f"{failed_pages} page{_plural(failed_pages, '', 's')} did not load." if failed_pages > 0 else None
    if ev is None:
        c10 = Draft(result="Failed", completed=False, basis="Evidence unavailable", explanation="The scan's evidence was not loaded, so the other results could not be traced to their source.", limitation="Evidence unavailable.")
    elif len(rest) == 0:
        c10 = Draft(result="Failed", completed=False, basis="Evidence unavailable", explanation=f"None of the other checks could be assessed, so there is no result to trace to its source.{gap_note}", limitation="No check could be assessed.")
    elif len(positive) == 0:
        c10 = Draft(result="Failed", evidence="", explanation=f"No finding rests on positive evidence: every completed result records only that something was not found.{abs_note}{gap_note}", limitation=pages_did_not_load)
    elif frac >= 0.8:
        c10 = Draft(result="Passed", evidence=of_line, source_url=None, quality="Specific", explanation=f"Each finding can be traced to its excerpt and to a page that was read.{abs_note}{gap_note}",
                    coverage=f"{scanned} of {discovered_sp} pages read" if scanned > 0 and discovered_sp is not None and discovered_sp > scanned else None)
    elif frac >= 0.5:
        c10 = Draft(result="Partially Passed", evidence=of_line, quality="Limited",
                    explanation=f"Some findings can be traced to a page that was read; the others carry no excerpt or name no page the scan read.{abs_note}{gap_note}", limitation=pages_did_not_load)
    else:
        c10 = Draft(result="Failed", evidence=of_line,
                    explanation=f"Most findings cannot be traced to a page the scan read, so they cannot be independently reviewed. Missing evidence is not proof of a violation.{abs_note}{gap_note}", limitation=pages_did_not_load)
    checks = [*first, _make(10, "Privacy Evidence and Transparency", None, "transparency", c10)]

    sc = score_checks(checks)
    limitations: list[str] = []
    if page_note:
        limitations.append(page_note)
    if failed_pages > 0:
        limitations.append(f"{failed_pages} page{_plural(failed_pages, '', 's')} did not load (see the page list).")
    if skipped_pages > 0:
        limitations.append(f"{skipped_pages} page{' was' if skipped_pages == 1 else 's were'} found but not scanned.")
    by_limit: dict[str, list[int]] = {}
    for c in checks:
        if c["result"] == "Not Assessed":
            key = c["limitation"] if c["limitation"] is not None else c["explanation"]
            by_limit.setdefault(key, []).append(c["id"])
    for text, ids in by_limit.items():
        limitations.append(f"Check{_plural(len(ids), '', 's')} {', '.join(str(i) for i in ids)} could not be assessed (left out of the score): {text}")
    visual_found = (visual or {}).get("pages_found") if visual is not None else None
    discovered = discovered_sp if discovered_sp is not None else visual_found
    assessed_n = sum(1 for c in checks if c["max"] is not None)
    limited_basis = assessed_n < 5
    if scanned > 0:
        where = f"{scanned} of the {discovered} pages found" if (discovered is not None and discovered > scanned) else f"{scanned} {_plural(scanned, 'page', 'pages')}"
    else:
        where = "no page"
    if sc["score"] is None:
        summary = "No meaningful score: no check could be assessed on the evidence available. This is not a finding about the website."
    else:
        nas = sc["counts"]["not_assessed"]
        summary = (f"{sc['total_text']} marks across {assessed_n} assessed check{_plural(assessed_n, '', 's')}, from {where} on the entered domain."
                   + (f" {nas} could not be assessed and {_plural(nas, 'is', 'are')} left out." if nas else "")
                   + (" Fewer than half the checks could be assessed, so these marks are provisional." if limited_basis else "")
                   + " Consent was tested once, on the homepage. An indicative readiness reading, not a legal determination or certification.")
    recs = [{"id": c["id"], "text": ADVICE[c["id"]]} for c in checks if c["result"] in ("Partially Passed", "Failed")]
    if any(c["result"] == "Not Assessed" for c in checks):
        recs.append({"id": 0, "text": "Run the scan again when the pages it could not read are reachable, so the checks left out can be assessed."})
    return {
        "checks": checks, "awarded": sc["awarded"], "applicable_max": sc["applicable_max"], "score": sc["score"], "counts": sc["counts"], "formula": sc["formula"],
        "total_text": sc["total_text"], "score_text": sc["score_text"], "incomplete": sc["counts"]["not_assessed"], "limited_basis": limited_basis, "summary": summary,
        "recommendations": recs, "pages": {"discovered": discovered, "scanned": scanned, "failed": failed_pages, "skipped": skipped_pages}, "limitations": limitations,
    }
