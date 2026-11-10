"""The DPDP Website Readiness Scorecard: ten parameters, 100 marks, in three categories (Task01/DPDP_Website_Readiness_Scorecard.xlsx).

One engine still decides everything. The 24 detailed checks of scoring/domain_score.py stay inside (their evidence, outcomes and legal basis are the detail behind each row, "Level 2"); this module is
the ten-row view over them, with the workbook's weights and a written decision table per row (docs/SCORECARD.md). Nothing is scored here that is not decided by a rule and backed by evidence.

Per row, one of five outcomes (the workbook has four; the fifth is what keeps a check we could not run from being scored as a failure, Functional Scope L-03, GR-18):
  pass            full marks
  partial         half the marks (every weight is even, so a whole number)
  fail            0 marks
  not_assessed    left out of the denominator: the check applies but could not be run
  not_applicable  left out of the denominator: the check does not apply (no consent tool is needed; no personal-data form)

Score = marks awarded / marks in play x 100, a whole number. Bands by the workbook's thresholds (90, 75, 50) with neutral names; fewer than 60% of the marks assessable gives "Insufficient evidence"
and no score; a Quick Scan is held at 99 and 100 needs a Deep Scan at full coverage and high confidence. There is no hard-fail cap: a failed row is the first finding, worded as a finding.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from app.scoring import domain_score as ds
from app.scoring import legal_map
from app.scoring.ten_checks import _find, _is_verified

SCORECARD_VERSION = "3.4.0"
RULES = {**ds.RULES, "scoring_rules": SCORECARD_VERSION, "scorecard": "DPDP_Website_Readiness_Scorecard.xlsx"}

PASS, PARTIAL, FAIL, NOT_ASSESSED, NA = "pass", "partial", "fail", "not_assessed", "not_applicable"
IN_PLAY = (PASS, PARTIAL, FAIL)
CATEGORIES = ("Cookie Consent", "Privacy Notice", "Forms & Security")
BANDS = (("Substantially addressed", 90), ("Largely addressed", 75), ("Partially addressed", 50), ("Needs attention", 0))
INSUFFICIENT = ds.INSUFFICIENT

#: id, category, name, marks, the internal checks (scoring/domain_score.py) behind it
ROWS: tuple[tuple[str, str, str, int, tuple[str, ...]], ...] = (
    ("P1", "Cookie Consent", "Consent banner shown on first visit", 8, ("B1",)),
    ("P2", "Cookie Consent", "Accept and Reject offered with equal ease", 8, ("B3",)),
    ("P3", "Cookie Consent", "No non-essential cookies or trackers before a choice", 12, ("B2",)),
    ("P4", "Cookie Consent", "Accept triggers trackers; Reject blocks them", 12, ("B4",)),
    ("P5", "Cookie Consent", "Consent can be withdrawn or changed at any time", 8, ("B5", "B6")),
    ("P6", "Privacy Notice", "Privacy notice published and reachable", 6, ("A1", "A5")),
    ("P7", "Privacy Notice", "Notice itemises data collected and specific purposes", 10, ("A2", "A3")),
    ("P8", "Privacy Notice", "DPO or grievance officer contact published", 10, ("C1", "C2")),
    ("P9", "Privacy Notice", "Data Principal rights and how to exercise them", 10, ("C3", "C4", "A4")),
    ("P10", "Forms & Security", "Forms collecting personal data have consent and a notice link, over HTTPS", 8, ("G1", "G2", "F1", "F2")),
    ("P11", "Forms & Security", "SSL certificate installed and valid for more than 30 days", 4, ()),
    ("P12", "Forms & Security", "Secure TLS version (TLS 1.3 preferred, TLS 1.2 accepted)", 4, ()),
)
#: The rows that read the certificate and TLS record (scanner/tls_assessment.py) rather than the 24 detailed checks; their legal basis is that of the secure-serving check F1 (Act s.8(5), Rules r.6).
TLS_ROWS = ("P11", "P12")
#: A certificate with more than this many days left earns full marks; a valid one with this many or fewer earns half.
SSL_FULL_DAYS = 30
WEIGHT = {r[0]: r[3] for r in ROWS}
assert sum(WEIGHT.values()) == 100 and all(w % 2 == 0 for w in WEIGHT.values())

#: id -> what is missing, why it matters, the action, owner, effort (the fixed phrase library; EV-06)
PHRASES: dict[str, tuple[str, str, str, str, str]] = {
    "P1": ("No consent banner was found while analytics or advertising activity was observed.", "Visitors are not offered a choice about activity that needs one.", "Add a consent mechanism that holds analytics and advertising until a choice is made.", "Website owner", "Medium"),
    "P2": ("No reject control was found as easily as Accept.", "A choice that is harder to refuse than to accept is not an even choice.", "Offer Reject All beside Accept All on the first layer of the banner.", "Website owner", "Low"),
    "P3": ("Analytics or advertising ran, or identifiers were stored, before any choice was made.", "Activity that depends on consent should not start before the visitor chooses.", "Hold analytics and advertising tags, including tag-manager tags, until Accept.", "Website owner", "Medium"),
    "P4": ("Analytics or advertising was still observed after Reject, or Reject did not hold across pages.", "A refusal that is not honoured has no effect for the visitor.", "Check the consent platform configuration and tag triggers so Reject stops the activity on every page.", "Website owner", "Medium"),
    "P5": ("Consent could not be reopened, or withdrawing it did not stop the activity.", "Consent should be as easy to withdraw as to give.", "Add a persistent cookie-settings control that reopens the choice, and make withdrawal stop the tags.", "Website owner", "Medium"),
    "P6": ("No privacy notice was found, or it is not linked from every page or has too little text.", "Without a reachable notice a visitor cannot be told what happens to their data.", "Publish the notice and link it from every page.", "Legal or privacy lead", "Low"),
    "P7": ("The notice does not itemise the personal data collected, or does not state the purposes.", "A visitor cannot tell what is collected or why.", "List each category of personal data and the specific purpose for it.", "Privacy lead", "Low"),
    "P8": ("The business contact for privacy questions, or the grievance route with a response period, is not fully published.", "A visitor with a question or complaint needs a published way to ask.", "Publish a business contact (email or phone) and a grievance route with a response period of at most 90 days.", "Privacy lead", "Low"),
    "P9": ("The notice does not fully list the rights, how to exercise them, how to withdraw consent and how to complain to the Board.", "A right that cannot be exercised has little value to the visitor.", "List the rights and give a route for each, for withdrawing consent, and for complaining to the Board.", "Privacy lead", "Low"),
    "P11": ("The site has no valid SSL certificate, or the certificate expires within 30 days.", "A missing, invalid or lapsing certificate lets browsers warn visitors away or lets data be read in transit.", "Install a trusted certificate for the exact host name and renew it well before it expires, ideally with automatic renewal.", "Engineering", "Low"),
    "P12": ("The site does not offer TLS 1.3 (TLS 1.2 is accepted, TLS 1.1 or below is not).", "Older TLS versions have known weaknesses and are retired by browsers.", "Enable TLS 1.3 on the web server or CDN, keep TLS 1.2 as the fallback, and switch off TLS 1.1 and below.", "Engineering", "Low"),
    "P10": ("A form that collects personal data lacks a consent control or notice link, has a pre-ticked box, or is not served securely.", "Details are collected without telling the visitor why, or in the open.", "Add an unticked consent control or a notice link beside each form and serve every form over HTTPS.", "Website owner", "Medium"),
}

#: What is missing and the action when a row is only partly met, where that differs from the row's general phrase (id -> missing, action).
PARTIAL_PHRASES: dict[str, tuple[str, str]] = {
    "P5": ("The choice can be reopened, but withdrawing it did not stop the analytics or advertising activity.", "Make withdrawal take effect at once: update the consent state of the tags (for example Google consent mode) and hold analytics and advertising tags when consent is withdrawn."),
}


def _ok(state: str) -> bool:
    return state == "earned"


def assess_scorecard(
    evidence: dict | None, policy_analysis: dict | None, scan_pages: dict | None, visual: dict | None, screenshot_page_url: str | None,
    mode: str = "quick", scanned_at: str | None = None, scan_signals: dict | None = None,
) -> dict[str, Any]:
    """The scorecard for one scan. Same inputs as domain_score.assess_scanner, whose result is the detail behind every row."""
    base = ds.assess_scanner(evidence, policy_analysis, scan_pages, visual, screenshot_page_url, mode=mode, scanned_at=scanned_at, scan_signals=scan_signals)
    internal = {p["ref"]: p for p in base["parameters"]}
    a = policy_analysis if policy_analysis and policy_analysis.get("status") != "not_run" else None
    sig = (((evidence or {}).get("consent_signals") or [{}])[0]) if (evidence or {}).get("consent_signals") else {}
    sev = sig.get("evidence") if isinstance(sig.get("evidence"), dict) else {}
    signals = scan_signals or {}
    try:
        scan_day = date.fromisoformat((scanned_at or "")[:10])
    except ValueError:
        scan_day = None

    def st(ref: str) -> str:
        return internal[ref]["state"]

    def req_ok(rid: str) -> bool:
        return _is_verified(_find(a, rid))

    has_banner = bool(sig) and sig.get("mechanism_type") != "none" and sig.get("banner_displayed") is not False
    banner_needed_off = st("B1") == "earned" and not has_banner                                                      # tracks nothing: no banner is needed
    prereq_p1 = st("B1") == "not_met"                                                                              # tracking with no banner: the rows that depend on a banner are blocked by P1
    rows: list[dict[str, Any]] = []

    def row(rid: str, outcome: str, found: str, subs: list[tuple[str, str, str]], blocked_by: str | None = None, evidence_override: list[dict] | None = None) -> None:
        _, cat, name, marks, refs = next(r for r in ROWS if r[0] == rid)
        ev_refs = evidence_override if evidence_override is not None else [e for r in refs if internal[r]["state"] in ("earned", "partial", "not_met", "prerequisite") for e in internal[r]["evidence"]][:3]
        if not ev_refs and blocked_by:
            ev_refs = next((x["evidence"] for x in rows if x["id"] == blocked_by), [])      # a blocked row rests on the evidence of the row that blocks it
        reason = "" if outcome in IN_PLAY else found
        provisions = []
        for r in (("F1",) if rid in TLS_ROWS else refs):
            for s in internal[r]["statutory"]:
                if s["provision"] not in [p["provision"] for p in provisions]:
                    provisions.append(s)
        rows.append({
            "id": rid, "category": cat, "name": name, "marks": marks, "outcome": outcome,
            "awarded": marks if outcome == PASS else marks // 2 if outcome == PARTIAL else 0 if outcome == FAIL else None,
            "found": found if outcome in IN_PLAY else "", "reason": reason, "evidence": ev_refs, "blocked_by": blocked_by,
            "sub_checks": [{"id": i, "name": n, "result": r} for i, n, r in subs], "internal": list(refs), "statutory": provisions,
            "limitation": None if rid in TLS_ROWS else next((internal[r]["limitation"] for r in refs if internal[r]["limitation"]), None),
        })

    def unknown(*states: str) -> bool:
        return any(s == "not_assessable" for s in states)

    def result_of(s: str) -> str:
        return {"earned": "met", "partial": "partly", "not_met": "not met", "prerequisite": "not met", "not_assessable": "not recorded", "not_applicable": "not applicable"}[s]

    # ── P1 consent banner ──
    banner_pages = signals.get("banner_by_page")
    granular = sig.get("has_granular_choices")
    missing_pages = [x["url"] for x in banner_pages if not x.get("banner")] if banner_pages else []
    subs = [("P1.1", "Banner visible in a fresh session", result_of(st("B1"))),
            ("P1.2", "Banner shown on inner pages until a choice is made", "not recorded" if not banner_pages else ("met" if not missing_pages else "not met")),
            ("P1.3", "Purposes or categories described", "not recorded" if granular is None else ("met" if granular else "not met"))]
    if banner_needed_off:
        row("P1", NA, "No consent banner was found and no analytics or advertising activity was observed, so none is needed.", subs)
    elif st("B1") == "not_met":
        row("P1", FAIL, internal["B1"]["found"], subs)
    elif unknown(st("B1")):
        row("P1", NOT_ASSESSED, internal["B1"]["reason"], subs)
    elif missing_pages or granular is False:
        row("P1", PARTIAL, "A banner was found" + (f" but not on {len(missing_pages)} inner page{'s' if len(missing_pages) != 1 else ''} read" if missing_pages else "") + ("; it does not describe purposes or categories." if granular is False else "."), subs)
    else:
        row("P1", PASS, "A consent banner was shown on first visit" + (" and on every inner page read." if banner_pages else "."), subs)

    def banner_dependent(rid: str, ref: str, decide, deep_only: bool = False) -> None:
        s0 = st(ref)
        if banner_needed_off or s0 == "not_applicable":
            row(rid, NA, "There is no consent mechanism to interact with, and none is needed.", [])
        elif deep_only and mode == "quick":
            row(rid, NOT_ASSESSED, internal[ref]["reason"] or "Beyond the reach of a Quick Scan; a Deep Scan assesses it.", [])     # outside what this scan type reaches: left out, never failed or blocked
        elif prereq_p1:
            row(rid, FAIL, "No consent banner was found, so this could not be met.", [], blocked_by="P1")
        else:
            decide()

    # ── P2 Accept and Reject with equal ease ──
    cmp_ = sev.get("button_comparison") if isinstance(sev, dict) else None
    subs2 = [("P2.1", "Reject button on the first layer", result_of(st("B3"))),
             ("P2.2", "Reject comparable to Accept", "not recorded" if not cmp_ else ("met" if cmp_.get("comparable") else "not met")),
             ("P2.3", "Category toggles available", "not recorded" if granular is None else ("met" if granular else "not met"))]

    def p2() -> None:
        if st("B3") == "not_met":
            row("P2", FAIL, internal["B3"]["found"], subs2)
        elif unknown(st("B3")):
            row("P2", NOT_ASSESSED, internal["B3"]["reason"], subs2)
        elif cmp_ and not cmp_.get("comparable"):
            row("P2", PARTIAL, "A reject control exists on the first layer but is visibly less prominent than Accept (" + str(cmp_.get("why") or "smaller or styled as a link") + ").", subs2)
        else:
            row("P2", PASS, "A reject control is offered on the first layer" + (" with comparable size and style." if cmp_ else "."), subs2)
    banner_dependent("P2", "B3", p2)

    # ── P3 nothing before a choice ──
    storage = (signals.get("storage_signals") or {})
    keys = storage.get("pre_consent_tracking_keys") or []
    subs3 = [("P3.1", "No non-essential cookies before a choice", result_of(st("B2"))), ("P3.2", "No tracker requests before a choice", result_of(st("B2"))),
             ("P3.3", "No tracking identifiers in browser storage", "not recorded" if "pre_consent_tracking_keys" not in storage else ("met" if not keys else "not met"))]
    if st("B2") == "not_met":
        row("P3", FAIL, internal["B2"]["found"], subs3)
    elif unknown(st("B2")):
        row("P3", NOT_ASSESSED, internal["B2"]["reason"], subs3)
    elif st("B2") == "partial":
        row("P3", PARTIAL, internal["B2"]["found"], subs3)
    elif keys:
        row("P3", PARTIAL, f"No analytics or advertising cookie or request before a choice, but {len(keys)} tracking identifier{'s' if len(keys) != 1 else ''} in browser storage ({', '.join(keys[:3])}).", subs3)
    else:
        row("P3", PASS, internal["B2"]["found"], subs3)

    # ── P4 Accept triggers, Reject blocks ──
    rp = sev.get("reject_persistence") if isinstance(sev, dict) else None
    later = [p for p in (rp or {}).get("further_pages", []) if p.get("loaded")]
    leak = [p for p in later if p.get("non_essential")]
    subs4 = [("P4.1", "Accept fires the consented categories", "met" if any("post_accept" in (t.get("consent_states") or []) for t in ((evidence or {}).get("trackers") or [])) else "not recorded"),
             ("P4.2", "Reject leaves no non-essential cookies or requests", result_of(st("B4"))),
             ("P4.3", "Reject holds on further pages", "not recorded" if not later else ("met" if not leak else "not met")),
             ("P4.4", "Pre-existing trackers cleared after Reject", "not recorded")]

    def p4() -> None:
        if st("B4") == "not_met":
            row("P4", FAIL, internal["B4"]["found"], subs4)
        elif unknown(st("B4")):
            row("P4", NOT_ASSESSED, internal["B4"]["reason"], subs4)
        elif st("B4") == "partial":
            row("P4", PARTIAL, internal["B4"]["found"], subs4)
        elif leak:
            row("P4", PARTIAL, f"Reject held on the first page, but analytics or advertising was contacted on {len(leak)} of {len(later)} further pages opened afterwards.", subs4)
        else:
            row("P4", PASS, internal["B4"]["found"] + (f" It held on {len(later)} further pages." if later else ""), subs4)
    banner_dependent("P4", "B4", p4)

    # ── P5 withdrawal (behaviour only: what the notice says about withdrawal is P9, never here) ──
    wd = sev.get("withdrawal") if isinstance(sev, dict) else None
    subs5 = [("P5.1", "A persistent cookie-settings entry point", "not recorded" if not wd else "met" if wd.get("reopen_control") else "not met" if wd.get("reason") == "no_reopen_control" else "not recorded"),
             ("P5.2", "Withdrawal stops the trackers", result_of(st("B5"))), ("P5.3", "Withdrawal holds on further pages", result_of(st("B6")))]

    def p5() -> None:
        if st("B5") == "not_assessable":
            row("P5", NOT_ASSESSED, internal["B5"]["reason"], subs5)
        elif st("B5") == "not_met" and "No option to reopen" in (internal["B5"]["found"] or ""):
            row("P5", FAIL, internal["B5"]["found"], subs5)
        elif st("B5") == "not_met" or st("B6") == "not_met":
            row("P5", PARTIAL, "The choice can be reopened, but withdrawing it did not stop the activity" + (" on further pages." if st("B5") != "not_met" else "."), subs5)
        else:
            row("P5", PASS, internal["B5"]["found"], subs5)
    banner_dependent("P5", "B5", p5)

    # ── P6 notice reachable ──
    ln = signals.get("notice_link_signals") or {}
    linked_all = bool(ln.get("pages")) and ln.get("with_link") == ln.get("pages")
    chars = next((d.get("characters") for d in ((a or {}).get("documents") or []) if d.get("characters")), None)
    short = chars is not None and chars < 1500
    subs6 = [("P6.1", "A privacy link on every page read", "not recorded" if not ln.get("pages") else ("met" if linked_all else "not met")),
             ("P6.2", "The notice loads and has substantive text", result_of(st("A1")) if chars is None else ("met" if not short else "not met"))]
    if st("A1") == "not_met":
        row("P6", FAIL, internal["A1"]["found"], subs6)
    elif unknown(st("A1")):
        row("P6", NOT_ASSESSED, internal["A1"]["reason"], subs6)
    elif (ln.get("pages") and not linked_all) or short:
        row("P6", PARTIAL, "The notice was found" + ("" if linked_all or not ln.get("pages") else f" but is linked from {ln['with_link']} of {ln['pages']} pages read") + (" and has very little text." if short else "."), subs6)
    else:
        row("P6", PASS, "The notice is published and reachable" + (" from every page read." if linked_all else "."), subs6)

    def prereq_a1(rid: str) -> bool:
        if st("A1") == "not_met":
            row(rid, FAIL, "The privacy notice this depends on was not found.", [], blocked_by="P6")
            return True
        if unknown(st("A1")) or st("A2") == "not_assessable":
            row(rid, NOT_ASSESSED, internal["A1"]["reason"] or "The notice could not be read, so what it says was not assessed.", [])
            return True
        return False

    # ── P7 data and purposes ──
    if not prereq_a1("P7"):
        s2, s3 = st("A2"), st("A3")
        subs7 = [("P7.1", "An itemised list of personal data", result_of(s2)), ("P7.2", "Specific purposes stated", result_of(s3))]
        if _ok(s2) and _ok(s3):
            row("P7", PASS, "The notice itemises the personal data collected and states the purposes.", subs7)
        elif s2 in ("earned", "partial") or s3 in ("earned", "partial"):
            row("P7", PARTIAL, "Only part is evidenced: " + ("the data is itemised but the purposes are not specific." if _ok(s2) else "the purposes are stated but the data is not itemised." if _ok(s3) else "one of the two is partly stated."), subs7)
        else:
            row("P7", FAIL, "The notice neither itemises the personal data nor states the purposes.", subs7)

    # ── P8 contact and grievance (Act s.8(9), s.8(10), Rules r.9, r.14(3): business contact information, not a name) ──
    if not prereq_a1("P8"):
        c1, c2, timeline = st("C1"), st("C2"), req_ok("grievance_timeline")
        subs8 = [("P8.1", "Business contact for privacy questions", result_of(c1)), ("P8.2", "Grievance route with a response period", "met" if _ok(c2) and timeline else "partly" if c2 in ("earned", "partial") else "not met")]
        if _ok(c1) and _ok(c2) and timeline:
            row("P8", PASS, "A business contact for privacy questions and a grievance route with a response period are published.", subs8)
        elif c1 in ("earned", "partial") or c2 in ("earned", "partial"):
            gap = [x for x, ok in (("a business contact for privacy questions", _ok(c1)), ("a grievance route", _ok(c2)), ("a stated response period", timeline)) if not ok]
            row("P8", PARTIAL, "Only part is published; missing: " + ", ".join(gap) + ".", subs8)
        else:
            row("P8", FAIL, "No business contact for privacy questions and no grievance route were found.", subs8)

    # ── P9 rights and routes ──
    if not prereq_a1("P9"):
        c3, c4 = st("C3"), st("C4")
        withdraw, board = req_ok("consent_withdrawal"), req_ok("complaint_to_board")
        subs9 = [("P9.1", "Rights listed", result_of(c3)), ("P9.2", "A route to exercise rights", "met" if req_ok("rights_means") else "not met"), ("P9.3", "A route to withdraw consent", "met" if withdraw else "not met"),
                 ("P9.4", "How to complain to the Data Protection Board", "met" if board else "not met")]
        missing = [x for x, ok in (("a route to withdraw consent", withdraw), ("how to complain to the Board", board), ("the right of nomination", c4 in ("earned", "not_assessable"))) if not ok]
        if c3 == "not_met":
            row("P9", FAIL, "No statement of rights or of how to exercise them was found.", subs9)
        elif _ok(c3) and not missing:
            row("P9", PASS, "The rights are listed with a route to exercise them, to withdraw consent and to complain to the Board.", subs9)
        else:
            row("P9", PARTIAL, "Rights are published; missing: " + (", ".join(missing) if missing else "part of the rights or the route to exercise them") + ".", subs9)

    # ── P10 forms and security ──
    g1, g2, f1, f2 = st("G1"), st("G2"), st("F1"), st("F2")
    mixed = (signals.get("mixed_content") or {})
    mixed_pages = mixed.get("pages_with_insecure_content") or []
    subs10 = [("P10.1", "Forms that collect personal details identified", result_of(g1)), ("P10.2", "A consent control or notice link beside each form", result_of(g1)),
              ("P10.3", "No pre-ticked consent box", result_of(g2)),
              ("P10.4", "HTTPS and no mixed content", "met" if _ok(f1) and f2 in ("earned", "not_applicable") and not mixed_pages else "not met" if "not_met" in (f1, f2) or mixed_pages else "not recorded")]
    if g1 == "not_applicable":
        row("P10", NA, "No form that collects personal details was found.", subs10)
    elif f2 == "not_met":
        row("P10", FAIL, internal["F2"]["found"], subs10)
    elif g1 == "not_met":
        row("P10", FAIL, internal["G1"]["found"], subs10)
    elif unknown(g1) and unknown(f1):
        row("P10", NOT_ASSESSED, internal["G1"]["reason"], subs10)
    elif g1 == "partial" or g2 == "not_met" or f1 == "not_met" or f2 == "partial" or mixed_pages:
        gaps = [x for x, bad in (("some forms have no consent control or notice link", g1 == "partial"), ("a consent box is pre-ticked", g2 == "not_met"), ("a page is not served over HTTPS", f1 == "not_met"),
                                 ("a form submits to another organisation's address", f2 == "partial"), (f"insecure content loads on {len(mixed_pages)} page(s)", bool(mixed_pages))) if bad]
        row("P10", PARTIAL, "Forms were found, but " + "; ".join(gaps) + ".", subs10)
    else:
        row("P10", PASS, "Every form that collects personal details has a consent control or notice link, no consent box is pre-ticked, and the pages are served over HTTPS.", subs10)

    # ── P11 SSL certificate and P12 TLS version (one verified handshake, scanner/tls_assessment.py; an observation not made is not assessed, never passed) ──
    tls = signals.get("tls_signals") or {}
    cert, tver = tls.get("certificate") or {}, tls.get("tls") or {}
    host_page = f"https://{tls['host']}" if tls.get("host") else None

    def tls_ev(text: str) -> list[dict]:
        return [{"observed": text, "page": host_page, "at": scanned_at}]

    cstatus, days, version = cert.get("status"), cert.get("days_remaining"), tver.get("version")
    subs11 = [("P11.1", "A certificate is installed and trusted for the host", "not recorded" if cstatus in (None, "unreachable") else "met" if cstatus == "valid" else "not met"),
              ("P11.2", f"More than {SSL_FULL_DAYS} days of validity remain", "met" if cstatus == "valid" and isinstance(days, int) and days > SSL_FULL_DAYS else "not met" if cstatus in ("valid", "expired", "invalid", "no_https") else "not recorded")]
    if not tls or cstatus in (None, "unreachable"):
        row("P11", NOT_ASSESSED, "The site's certificate was not checked in this scan." if not tls else "The certificate could not be checked: " + (cert.get("reason") or "the handshake did not complete") + ".", subs11)
    elif cstatus == "valid" and isinstance(days, int):
        org, cn = cert.get("issuer_org") or "", cert.get("issuer") or ""
        issuer = f"; issuer {org} ({cn})" if org and cn and org != cn else f"; issuer {org or cn}" if (org or cn) else ""
        text = f"SSL certificate valid; expires {cert.get('not_after')} ({days} day{'s' if days != 1 else ''} remaining){issuer}."
        if days > SSL_FULL_DAYS:
            row("P11", PASS, f"A valid SSL certificate is installed and has {days} days left.", subs11, evidence_override=tls_ev(text))
        else:
            row("P11", PARTIAL, f"A valid SSL certificate is installed but it expires in {days} day{'s' if days != 1 else ''} (within {SSL_FULL_DAYS} days).", subs11, evidence_override=tls_ev(text))
    elif cstatus == "valid":
        row("P11", NOT_ASSESSED, "The certificate was accepted but its expiry date could not be read.", subs11)
    elif cstatus == "expired":
        when = f" on {cert['not_after']}" if cert.get("not_after") else ""
        row("P11", FAIL, f"The SSL certificate has expired{when}.", subs11, evidence_override=tls_ev(f"SSL certificate expired{when}."))
    elif cstatus == "no_https":
        row("P11", FAIL, "The site was reached over http and no SSL certificate is in use.", subs11, evidence_override=tls_ev("The site was served over http, with no SSL certificate."))
    else:
        row("P11", FAIL, "The SSL certificate is not valid for this site (" + (cert.get("reason") or "it is not trusted") + ").", subs11, evidence_override=tls_ev("SSL certificate not trusted: " + (cert.get("reason") or "not valid for the host") + "."))
    subs12 = [("P12.1", "TLS 1.2 or higher is offered", "not recorded" if not version else "met" if version in ("1.2", "1.3") else "not met"),
              ("P12.2", "TLS 1.3 is offered", "not recorded" if not version else "met" if version == "1.3" else "not met")]
    if not tls or (not version and cstatus != "no_https"):
        row("P12", NOT_ASSESSED, "The TLS version was not checked in this scan." if not tls else "The TLS version could not be determined: " + (tver.get("reason") or "the handshake did not complete") + ".", subs12)
    elif cstatus == "no_https":
        row("P12", FAIL, "The site was reached over http, so there is no TLS connection.", subs12, blocked_by="P11", evidence_override=tls_ev("The site was served over http, with no TLS connection."))
    elif version == "1.3":
        row("P12", PASS, "The server negotiates TLS 1.3.", subs12, evidence_override=tls_ev("TLS 1.3 detected."))
    elif version == "1.2":
        row("P12", PARTIAL, "The server negotiates TLS 1.2 but not TLS 1.3.", subs12, evidence_override=tls_ev("TLS 1.2 detected; TLS 1.3 not offered."))
    else:
        row("P12", FAIL, f"The best TLS version the server offers is {version}, which is retired.", subs12, evidence_override=tls_ev(f"Best TLS version offered: {version}."))

    return _result(base, rows, internal, mode, scan_day)


def _result(base: dict[str, Any], rows: list[dict[str, Any]], internal: dict[str, dict], mode: str, scan_day: date | None) -> dict[str, Any]:
    rows.sort(key=lambda r: int(r["id"][1:]))
    in_play = sum(r["marks"] for r in rows if r["outcome"] in IN_PLAY)
    awarded = sum(r["awarded"] or 0 for r in rows if r["outcome"] in IN_PLAY)
    na_marks = sum(r["marks"] for r in rows if r["outcome"] == NA)
    unassessed = sum(r["marks"] for r in rows if r["outcome"] == NOT_ASSESSED)
    coverage = round(100 * (100 - unassessed) / 100)
    share = None if in_play == 0 else 100 * awarded / in_play
    level = base["confidence"]["level"]
    notes: list[str] = []
    score: int | None = None if share is None else int(share + 0.5)
    band = INSUFFICIENT
    indicative = None
    if score is not None:
        band = next(name for name, floor in BANDS if score >= floor)
        if coverage < 60:
            band, indicative, score = INSUFFICIENT, score, None
            notes.append(f"Fewer than 60% of the marks ({coverage}%) could be assessed, so no settled band and no score are given.")
        elif level == "low":
            order = [b[0] for b in BANDS][::-1]
            band = order[max(0, order.index(band) - 1)]
            notes.append("Evidence confidence is low, so the band is held one level lower.")
        if score is not None and score >= 100 and not (mode == "deep" and coverage == 100 and level == "high"):
            score = 99
            notes.append("A score of 100 needs a Deep Scan at full coverage with high evidence confidence, so it is held at 99.")
    else:
        notes.append("No row could be assessed.")
    findings = []
    for r in sorted((x for x in rows if x["outcome"] in (PARTIAL, FAIL)), key=lambda x: (-(x["marks"] - (x["awarded"] or 0)), x["id"])):
        miss, why, action, owner, effort = PHRASES[r["id"]]
        if r["id"] in PARTIAL_PHRASES and r["outcome"] == PARTIAL:
            miss, action = PARTIAL_PHRASES[r["id"]]          # the half-way case needs a different fix from the missing one
        lost = r["marks"] - (r["awarded"] or 0)
        severity = "High" if lost >= 10 else "Medium" if lost >= 5 else "Low"
        conf = {"high": "High", "medium": "Medium", "low": "Low"}[level]
        findings.append({
            "ref": r["id"], "domain": r["category"], "title": r["name"], "outcome": ds.NOT_DETECTED if r["outcome"] == FAIL else ds.OBSERVED, "state": r["outcome"], "severity": severity, "confidence": conf,
            "observed": ("Worth investigating: " if severity == "High" and level == "low" else "") + (r["found"] or miss), "evidence": r["evidence"], "why_it_matters": why, "action": action,
            "owner": owner, "effort": effort, "consequences": [x["id"] for x in rows if x["blocked_by"] == r["id"]], "points_at_stake": lost,
            "statutory": [{**s, "in_force": legal_map_in_force(s, scan_day)} for s in r["statutory"]],
        })
    n_na = sum(1 for r in rows if r["outcome"] == NA)
    n_un = sum(1 for r in rows if r["outcome"] == NOT_ASSESSED)
    explanation = ("The score is the share of the assessable marks the website earned, expressed out of 100"
                   + (f"; {n_na} row{'s' if n_na != 1 else ''} did not apply and {n_un} could not be assessed, and {'they are' if n_na + n_un != 1 else 'it is'} left out of the calculation" if (n_na or n_un) else "") + ".")
    cats = [{"category": c, "marks": sum(r["marks"] for r in rows if r["category"] == c),
             "awarded": sum(r["awarded"] or 0 for r in rows if r["category"] == c and r["outcome"] in IN_PLAY),
             "assessable": sum(r["marks"] for r in rows if r["category"] == c and r["outcome"] in IN_PLAY)} for c in CATEGORIES]
    out = {k: v for k, v in base.items() if k not in ("parameters", "findings", "top_findings", "priority_actions", "more_actions", "remedies", "removed", "beyond_quick_scan", "domains", "points")}
    out.update({
        "version": SCORECARD_VERSION, "rules": RULES, "engine_detail": ds.SCORE_VERSION, "score": score, "indicative_score": indicative, "score_text": "No meaningful score" if score is None else f"{score}/100",
        "scale": 100, "band": band, "band_notes": notes, "coverage": coverage, "explanation": explanation, "rows": rows, "categories": cats, "details": list(internal.values()),
        "points": {"awarded": awarded, "in_play": in_play, "reachable": 100, "not_applicable": na_marks, "not_assessable": unassessed, "beyond_scan_type": 0},
        "findings": findings, "top_findings": findings[:3], "priority_actions": findings[:5], "more_actions": max(0, len(findings) - 5), "remedies": [], "removed": [
            {"ref": r["id"], "name": r["name"], "points": r["marks"], "state": r["outcome"], "reason": r["reason"]} for r in rows if r["outcome"] in (NA, NOT_ASSESSED)],
        "beyond_quick_scan": [],
    })
    validate_scorecard(out)
    return out


def legal_map_in_force(s: dict, on: date | None) -> bool:
    return bool(s.get("in_force")) if "in_force" in s else (on is not None and on >= legal_map.MAY_2027)


def validate_scorecard(r: dict[str, Any]) -> None:
    """The validated-result gate for the scorecard: no asserting row without evidence, no marks on a row that asserts nothing, no prohibited wording, a whole-number score."""
    for x in r["rows"]:
        if x["outcome"] in IN_PLAY and not x["evidence"]:
            raise ds.ResultRefused(f"{x['id']}: an asserting row has no evidence")
        if x["outcome"] not in IN_PLAY and x["awarded"] is not None:
            raise ds.ResultRefused(f"{x['id']}: a row that asserts nothing carries marks")
        if x["blocked_by"] and x["outcome"] != FAIL:
            raise ds.ResultRefused(f"{x['id']}: blocked but not scored as the workbook's rule says")
        if ds.prohibited_wording(x["found"] + " " + x["reason"]):
            raise ds.ResultRefused(f"{x['id']}: prohibited wording")
    if sum(x["marks"] for x in r["rows"]) != 100:
        raise ds.ResultRefused("the weights do not sum to 100")
    from app.services import report_qc            # calculation QC: the rows, the categories, the points and the score reconcile
    if problems := report_qc.check_scorecard(r):
        raise ds.ResultRefused("calculation check failed: " + "; ".join(problems))
    if r["score"] is not None and (not isinstance(r["score"], int) or not 0 <= r["score"] <= 100):
        raise ds.ResultRefused("score is not a whole number from 0 to 100")
    for f in r["findings"]:
        if ds.prohibited_wording(f["observed"] + " " + f["why_it_matters"] + " " + f["action"]):
            raise ds.ResultRefused(f"finding {f['ref']}: prohibited wording")
    if ds.prohibited_wording(r["explanation"] + " " + r["disclaimer"]):
        raise ds.ResultRefused("prohibited wording in the report text")
