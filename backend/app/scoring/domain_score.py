"""The Scanner Score: the seven-domain, 100-point assessment of a public website (Scoring Mechanism v1.0, Annexure A to the Functional Scope v1.0).

One pure function of a scan's stored evidence and its policy analysis, so the same stored scan always gives the same result (SC-06, SC-31). No language model is in the path.
Everything that prints a score reads `assess_scanner`, served by `GET /api/v1/consent/scans/{id}/assessment`.

How a parameter is scored (section 4 of the Scoring Mechanism). Exactly one state each:

  earned          counts, scores its points (or the partial points the rules define in PARTIAL)
  partial         counts, scores the defined partial points
  not_met         counts, scores zero: searched for within a stated scope and not found, or observed and failed. The website's gap.
  prerequisite    counts, scores zero: depends on an artefact the website does not have (no notice, so nothing in it can be evidenced). The website's gap, linked to the blocking finding.
  not_assessable  removed from the calculation: blocked, out of time, outside what this scan type reaches, or outside what the scanner can see yet. OUR limit, never the website's.
  not_applicable  removed from the calculation: the parameter does not apply by an objective test.

Score = points awarded / points in play x 100, a whole number, for Quick and Deep alike: every result is shown out of 100 and nowhere else. Points in play are those of earned, partial, not_met and
prerequisite parameters. See docs/SCANNER_SCORE.md.

The taxonomy of Functional Scope 6.2 (Observed, Not detected, Potential issue, Inconclusive, Not tested, Not applicable, Restricted, Blocked by a prerequisite) is carried on each
parameter as `outcome`. Where the two documents differ, the Scoring Mechanism decides the score (a prerequisite-blocked parameter costs its points) and the Scope decides the wording.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any
from urllib.parse import urlsplit

from app.rules.vendor_orgs import org_of
from app.scoring import legal_map
from app.scoring.ten_checks import (
    _F,
    _LEGAL_PATH,
    _clip,
    _find,
    _is_verified,
    _path_of,
    _plural,
    _read,
    sharing_names_recipients,
)

SCORE_VERSION = "2.2.4"
RULES = {"scoring_rules": SCORE_VERSION, "check_list": "2026.11.1", "classification_scheme": "2026.11.1", "statutory_mapping": "2026.10.1"}

#: Every score is out of 100, whatever the scan type. A Quick Scan cannot assess every parameter; those it cannot reach are left out and the rest are redistributed (SC-14), exactly as for any
#: other parameter that cannot be assessed. 100 itself needs a Deep Scan at full coverage and high confidence (SC-37), so a Quick Scan is held at 99.
SCALE = 100
QUICK_MAX = 99
COMMENCEMENT = "13 May 2027"

# ── the matrix (section 5) ─────────────────────────────────────────────────────────────────────────────────────────────────────────

#: ref, domain, name, points, reach ("both" or "deep")
PARAMETERS: tuple[tuple[str, str, str, int, str], ...] = (
    ("A1", "A", "Privacy notice published and reachable", 8, "both"),
    ("A2", "A", "Notice itemises the personal data collected", 5, "both"),
    ("A3", "A", "Notice states the specified purposes", 5, "both"),
    ("A4", "A", "Notice explains withdrawal, rights and complaints", 5, "both"),
    ("A5", "A", "Notice reachable site-wide and offered beyond English", 3, "deep"),
    ("B1", "B", "A consent mechanism exists where one is needed", 6, "both"),
    ("B2", "B", "Nothing non-essential runs before a choice is made", 7, "both"),
    ("B3", "B", "Reject is offered as easily as Accept", 5, "both"),
    ("B4", "B", "Reject is honoured", 5, "both"),
    ("B5", "B", "Consent can be withdrawn, and withdrawal is honoured", 3, "both"),
    ("B6", "B", "Withdrawal persists across pages", 2, "both"),
    ("C1", "C", "Contact for privacy questions published", 6, "both"),
    ("C2", "C", "Grievance mechanism published", 6, "both"),
    ("C3", "C", "Rights and how to exercise them published", 6, "both"),
    ("C4", "C", "Right of nomination disclosed", 2, "deep"),
    ("D1", "D", "No behavioural tracking on child-directed surfaces", 3, "deep"),
    ("D2", "D", "Age assurance or parental consent route present", 3, "deep"),
    ("E1", "E", "Third-party services identified and disclosed", 4, "deep"),
    ("E2", "E", "Cross-border destinations identifiable", 3, "deep"),
    ("E3", "E", "No undisclosed data-collecting third party", 3, "deep"),
    ("F1", "F", "Served securely, with no insecure content on pages that collect data", 3, "both"),
    ("F2", "F", "Form submissions go to a secure, accounted-for destination", 3, "deep"),
    ("G1", "G", "Forms that collect personal details present consent or a notice link", 3, "both"),
    ("G2", "G", "No pre-selected consent controls", 1, "deep"),
)

DOMAINS: dict[str, dict[str, str]] = {
    "A": {"name": "Notice and transparency", "anchor": "DPDP Act s.5(1), 5(3); Rules r.3", "relation": "source of the obligation"},
    "B": {"name": "Consent behaviour", "anchor": "DPDP Act s.6(1), 6(4) to 6(6); Rules r.3(c)", "relation": "source of the obligation"},
    "C": {"name": "Rights, grievance and contact", "anchor": "DPDP Act s.8(9), 8(10), 13, 11 to 14; Rules r.9, r.14", "relation": "source of the obligation"},
    "D": {"name": "Children and vulnerable persons", "anchor": "DPDP Act s.9; Rules r.10", "relation": "source of the obligation"},
    "E": {"name": "Third parties and cross-border", "anchor": "DPDP Act s.8(1), 8(2), s.16; Rules r.15", "relation": "related context"},
    "F": {"name": "Security of the public surface", "anchor": "DPDP Act s.8(5); Rules r.6", "relation": "related context"},
    "G": {"name": "Collection practice", "anchor": "DPDP Act s.6(1)", "relation": "source of the obligation"},
}

#: The partial points each parameter may award, and only these (SC-17). Whole numbers, so no figure anywhere carries a decimal point (SC-08).
PARTIAL: dict[str, int] = {"A2": 2, "A5": 1, "D2": 1, "E2": 1, "A3": 2, "B2": 3, "B4": 2, "C1": 3, "C2": 3, "C3": 3, "E1": 2, "F2": 2}

#: Readiness indicators: statements the notice makes (or does not) that the Act and Rules relate to a website, but that the Scoring Mechanism does not score. They carry evidence and an outcome like a
#: parameter, never marks, and never move the score (Functional Scope RP-09 "readiness indicators"; a change to what is scored is a change to the scoring rules and needs legal sign-off, GR-24).
#: id, name, the policy requirement the analysis stores for it (None: read from the scan's security signals instead)
INDICATORS: tuple[tuple[str, str, str | None], ...] = (
    ("I1", "Retention and erasure stated", "retention"),
    ("I2", "Security safeguards described", "security"),
    ("I3", "Personal data breach handling described", "breach_response"),
    ("I4", "Cookies and tracking technologies described", "cookies_tracking"),
    ("I5", "Grounds for processing, including legitimate uses, stated", "lawful_basis"),
    ("I6", "Grievance response period of at most 90 days stated", "grievance_timeline"),
    ("I7", "How to complain to the Data Protection Board stated", "complaint_to_board"),
    ("I8", "Security headers sent by the home page", None),
    ("I9", "Each choice by purpose does only what it says", None),
)
_HEADER_NAMES = {"hsts": "Strict-Transport-Security", "csp": "Content-Security-Policy", "x_frame_options": "X-Frame-Options", "x_content_type_options": "X-Content-Type-Options",
                 "referrer_policy": "Referrer-Policy", "permissions_policy": "Permissions-Policy"}

#: What the test of choices by purpose says when it stops short (the same words as scanner/purposes.REASONS; a test keeps the two equal).
PURPOSE_REASONS = {
    "homepage_not_loaded": "The home page did not load, so choices by purpose were not tested.",
    "no_preferences_control": "No control that opens the cookie preferences was found, so choices by purpose were not tested.",
    "no_purpose_choices": "The consent tool offers no choice by purpose (only accept or reject), so there is nothing to test purpose by purpose.",
    "no_save_control": "The preferences offered no control that saves the chosen purposes, so choices by purpose were not tested.",
    "error": "The test of choices by purpose could not be completed.",
}

POINTS = {p[0]: p[3] for p in PARAMETERS}
NAME = {p[0]: p[2] for p in PARAMETERS}
DOMAIN_OF = {p[0]: p[1] for p in PARAMETERS}
REACH = {p[0]: p[4] for p in PARAMETERS}

# ── outcomes (Functional Scope 6.2) ────────────────────────────────────────────────────────────────────────────────────────────────

OBSERVED, NOT_DETECTED, POTENTIAL_ISSUE = "Observed", "Not detected", "Potential issue"
INCONCLUSIVE, NOT_TESTED, NOT_APPLICABLE = "Inconclusive", "Not tested", "Not applicable"
RESTRICTED, BLOCKED, HUMAN_REVIEW = "Restricted", "Blocked by a prerequisite", "Requires human review"
OUTCOMES = (OBSERVED, NOT_DETECTED, POTENTIAL_ISSUE, INCONCLUSIVE, NOT_TESTED, NOT_APPLICABLE, RESTRICTED, BLOCKED, HUMAN_REVIEW)
ASSERTING = (OBSERVED, NOT_DETECTED, POTENTIAL_ISSUE)

EARNED, PART, NOT_MET, PREREQ, NOT_ASSESSABLE, NA = "earned", "partial", "not_met", "prerequisite", "not_assessable", "not_applicable"
IN_PLAY = (EARNED, PART, NOT_MET, PREREQ)

BANDS = ("Early stage", "Needs attention", "Developing", "Substantially addressed", "Evidence-led")
INSUFFICIENT = "Insufficient evidence"

# ── what the product must never say (GR-01 to GR-05, GR-08) ────────────────────────────────────────────────────────────────────────

PROHIBITED = re.compile(
    r"\b(violations?|breach(es)? of the act|non-?compliant|illegal(ly)?|penalty applies|penalties apply"
    r"|(is|are|was|were) (not )?(fully )?compliant|certified|certificate of compliance|trust (mark|seal)|personal data (was|were) (sent|shared|transferred|leaked))\b", _F)


class ResultRefused(Exception):
    """The validated-result gate (Functional Scope 17): a result that would assert something the scan cannot back is never produced (EV-02, EV-07, GR-08, GR-19, GR-20)."""


def prohibited_wording(text: str) -> list[str]:
    return sorted({m.group(0).lower() for m in PROHIBITED.finditer(text or "")})


# ── fixed phrase library (EV-06): the wording follows the parameter and its outcome, never free generation ──────────────────────────

#: ref -> (what is missing, why it matters, recommended action, owner, effort)
PHRASES: dict[str, tuple[str, str, str, str, str]] = {
    "A1": ("No privacy notice was found on the pages read.", "Without a notice, a visitor cannot be told what happens to their data, and nothing in the notice can be checked.",
           "Publish a privacy notice and link it from every page.", "Legal or privacy lead", "Low"),
    "A2": ("The notice does not list the categories of personal data collected.", "A visitor cannot tell what is collected about them.",
           "List each category of personal data collected, including data collected automatically.", "Privacy lead", "Low"),
    "A3": ("The notice does not state the purposes for which the data is used.", "A visitor cannot tell why their data is wanted.",
           "State each purpose beside the data it relates to.", "Privacy lead", "Low"),
    "A4": ("The notice does not give a route to withdraw consent, to exercise rights, or to make a complaint to the Data Protection Board.", "The Act and the Rules expect these three routes to be given in the notice.",
           "Add how to withdraw consent, how to exercise each right, and how to make a complaint to the Board.", "Privacy lead", "Low"),
    "A5": ("The notice is not linked from every page read or not offered in an Eighth Schedule language.", "Visitors may not find the notice or read it in their language.",
           "Link the notice site-wide and offer it in at least one Eighth Schedule language.", "Content owner", "Medium"),
    "B1": ("Analytics or advertising activity was observed and no consent mechanism was found.", "Visitors are not offered a choice about activity that needs one.",
           "Add a consent mechanism that holds analytics and advertising until a choice is made.", "Website owner", "Medium"),
    "B2": ("Analytics or advertising activity was observed before any choice was made.", "Activity that depends on consent should not start before the visitor chooses.",
           "Hold analytics and advertising tags until Accept, including tags added through a tag manager.", "Website owner", "Medium"),
    "B3": ("No reject control was found at the same level as Accept.", "A choice that is harder to refuse than to accept is not an even choice.",
           "Offer Reject All beside Accept All on the first layer of the banner.", "Website owner", "Low"),
    "B4": ("Analytics or advertising activity was still observed after Reject.", "A refusal that is not honoured has no effect for the visitor.",
           "Check the consent platform configuration and the tag triggers so that Reject stops the activity.", "Website owner", "Medium"),
    "B5": ("Withdrawal could not be shown to work.", "Consent should be as easy to withdraw as to give.", "Provide a way to reopen the choice and stop the activity.", "Website owner", "Medium"),
    "B6": ("Withdrawal was not honoured on further pages.", "A choice forgotten on the next page is not a choice.", "Persist the withdrawn state across pages.", "Website owner", "Medium"),
    "C1": ("No business contact for privacy questions was found.", "Visitors need someone to ask.",
           "Publish the business contact (an email address or phone number) of a Data Protection Officer or of a person who can answer privacy questions, in the notice and the footer.", "Privacy lead", "Low"),
    "C2": ("No grievance route with a means of contact was found.", "A visitor with a complaint needs a published way to make it.",
           "Publish a grievance route with a contact and, if possible, a response period.", "Privacy lead", "Low"),
    "C3": ("The rights of a Data Principal and how to exercise them were not fully published.", "A right that cannot be exercised has little value to the visitor.",
           "List the rights and give the route by which each can be exercised.", "Privacy lead", "Low"),
    "C4": ("The right to nominate another person was not disclosed.", "The Act gives this right and the notice should say so.", "Add the right of nomination and how to use it.", "Privacy lead", "Low"),
    "D1": ("Tracking was observed on a child-directed surface.", "Tracking of children needs particular care.", "Remove behavioural tracking from child-directed pages.", "Website owner", "Medium"),
    "D2": ("No route to age assurance or parental consent was found on a child-directed surface.", "Children's data needs verifiable parental consent.", "Add an age-assurance or parental-consent route.", "Privacy lead", "High"),
    "E1": ("Some of the services the site contacts are not named or described in the notice.", "Visitors cannot tell who receives their data.",
           "Name, or describe by category, the services the site contacts.", "Privacy lead", "Low"),
    "E2": ("Services outside India were contacted and the notice does not say data may be processed outside India.", "Visitors should know where data may go.",
           "State that data may be processed outside India.", "Privacy lead", "Low"),
    "E3": ("A service that collects personal details was observed and the notice does not account for it.", "Undisclosed collection cannot be explained to the visitor.",
           "Add the service to the notice or remove it.", "Privacy lead", "Medium"),
    "F1": ("A page was served without a secure connection, or a page that collects data loaded insecure content.", "Data entered on such a page can be read in transit.",
           "Serve every page over HTTPS and remove insecure content.", "Engineering", "Medium"),
    "F2": ("A form submits to an insecure or unaccounted-for destination.", "The visitor cannot tell where their details go.", "Submit forms over HTTPS to a destination the notice names.", "Engineering", "Medium"),
    "G1": ("A form that collects personal details shows no consent control and no link to the notice.", "Details are collected without telling the visitor why.",
           "Add a consent control or a notice link beside the form.", "Website owner", "Low"),
    "G2": ("A consent control on a form is pre-selected.", "Consent should be given by an action, not assumed.", "Remove the pre-selection.", "Website owner", "Low"),
}

#: SC-32: a capability is named in a report only when product and marketing have confirmed it is live. All are unconfirmed until they say so; the closing step is still shown.
CAPABILITY_LIVE: dict[str, bool] = {d: False for d in DOMAINS}
CAPABILITY = {
    "A": "Notice and consent management", "B": "The Consiva consent platform", "C": "Data principal rights and grievance workflows", "D": "Children's data governance",
    "E": "Processor and vendor governance", "F": "Engineering services", "G": "Consent capture on forms",
}

#: Domain F remediation is engineering work rather than a platform module (Scoring 9.1).
QUICK_REMEDY_CAP = 3

_LOADED = ("seed", "link", "sitemap")
#: Primary language subtags of the Eighth Schedule languages, for reading a page's hreflang alternates.
_EIGHTH_SCHEDULE = frozenset({"hi", "bn", "te", "mr", "ta", "ur", "gu", "kn", "ml", "or", "od", "pa", "as", "mai", "sa", "ks", "ne", "sd", "kok", "doi", "mni", "brx", "sat"})
#: What the withdrawal test says when it stops short (the same words as scanner/withdrawal.REASONS; a test keeps the two equal).
WITHDRAWAL_REASONS = {
    "homepage_not_loaded": "The home page did not load, so withdrawal was not tested.",
    "accept_not_clicked": "Accept could not be clicked, so there was no choice to withdraw.",
    "no_reopen_control": "No option to reopen or manage the cookie choice was found, so withdrawal could not be tested.",
    "no_withdraw_control": "The reopened cookie dialog offered no control that withdraws consent (such as Reject all), so withdrawal could not be tested.",
    "error": "The withdrawal test could not be completed.",
}
_CONSENT_LIKE = re.compile(r"agree|consent|terms|privacy|policy|subscribe|newsletter|marketing|promotion|updates|offers|contact me|opt[-\s]?in|accept", re.IGNORECASE)
_TRACK = re.compile(r"analytic|market|advert|track", _F)
_BLOCK_WORDS = re.compile(r"robots|restrict|denied|forbidden|401|403|captcha|blocked", _F)
_NAMED_ROLE = ("name", "designation")


def _norm(u: str) -> str:
    return re.sub(r"/$", "", re.sub(r"[#?].*$", "", re.sub(r"^www\.", "", re.sub(r"^https?://", "", (u or "").lower()))))


def _host(u: str | None) -> str | None:
    try:
        h = urlsplit(u or "").hostname
    except ValueError:
        return None
    return h.lower().removeprefix("www.") if h else None


# ── the assessment ────────────────────────────────────────────────────────────────────────────────────────────────────────────────

def assess_scanner(
    evidence: dict | None, policy_analysis: dict | None, scan_pages: dict | None, visual: dict | None, screenshot_page_url: str | None,
    mode: str = "quick", scanned_at: str | None = None, scan_signals: dict | None = None,
) -> dict[str, Any]:
    """The Scanner Score of one scan. `mode` is "quick" (five pages, consent on the home page) or "deep". Both are scored out of 100 the same way.

    The inputs are those of the ten-check engine it replaces: `evidence` (pages, policies, cookies, trackers, forms, consent_signals), `policy_analysis`, `scan_pages`
    (ten_inputs.build_scan_pages), `visual` (ten_inputs.build_visual) and the homepage address. `scanned_at` is when the scan ran; it is written into every evidence reference (EV-01).
    `scan_signals` is the website-scan stage record: language_signals, notice_link_signals, child_surfaces and form_signals, which the crawler writes (scanner/page_signals.py). A scan made before
    they were recorded has none of them, and the parameters that need them are then not assessable, never failed.
    """
    if mode not in ("quick", "deep"):
        raise ValueError("mode must be 'quick' or 'deep'")
    ev, a, sp = evidence, policy_analysis, scan_pages
    try:
        scan_day = date.fromisoformat((scanned_at or "")[:10])
    except ValueError:
        scan_day = None
    pages = (ev or {}).get("pages") or []
    cookies = (ev or {}).get("cookies") or []
    trackers = (ev or {}).get("trackers") or []
    forms = (ev or {}).get("forms") or []
    sp_failed = (sp or {}).get("failed") or []
    scanned = (sp or {}).get("scanned_count")
    if scanned is None:
        scanned = (visual or {}).get("pages_scanned")
    scanned = scanned or 0
    discovered = (sp or {}).get("discovered")
    read_urls = [r["url"] for r in ((sp or {}).get("rows") or []) if r.get("scanned")] or [p["url"] for p in pages if p.get("discovered_via") in _LOADED]
    home = screenshot_page_url or (read_urls[0] if read_urls else None)
    cons = (visual or {}).get("consent") if visual is not None else None
    detection_ran = scanned > 0 and ev is not None

    def searched(what: str) -> str:
        n = f"{scanned} page{_plural(scanned, '', 's')} read"
        return f"Searched for {what} on {n}{f' of {discovered} found' if discovered is not None else ''}."

    def ref(what: str, page: str | None) -> dict[str, Any]:
        return {"observed": what, "page": page, "at": scanned_at}

    # restricted / failed pages (EX-04)
    blocked_pages = [f for f in sp_failed if _BLOCK_WORDS.search(f.get("reason") or "")]
    attempted = scanned + len(sp_failed)
    restricted = bool(blocked_pages) or (attempted > 0 and len(sp_failed) / attempted > 0.2)

    # ── the privacy notice (A1) and what is in it ────────────────────────────────────────────────────────────────────────────────
    def legal_first(items: list[dict]) -> list[dict]:
        return sorted(items, key=lambda x: 0 if _LEGAL_PATH.search(_path_of(x["url"])) else 1)

    policies = legal_first([p for p in ((ev or {}).get("policies") or []) if re.search(r"privacy", p.get("policy_type") or "", _F)])
    a_docs = (a or {}).get("documents", []) if a else []
    privacy_docs = legal_first([d for d in a_docs if re.search(r"privacy", d.get("policy_type") or "", _F)])
    doc = privacy_docs[0] if privacy_docs else (a_docs[0] if a_docs else None)
    failed_policy = next((f for f in sp_failed if re.search(r"privacy|policy", f["url"], _F)), None)
    if failed_policy is None:
        failed_policy = next(({"url": p["url"], "reason": "Blocked by robots.txt"} for p in pages if re.search(r"privacy|policy", p["url"], _F) and p.get("discovered_via") == "robots_disallowed"), None)
    notice_url = policies[0]["url"] if policies else (doc["url"] if doc else None)
    notice_found = bool(policies or doc)
    text_read = _read(a) or bool(doc and doc.get("characters", 0) > 0)
    policy_ran = bool(a) and a.get("status") != "not_run"
    # a recorded policy-analysis stage that failed or has not run leaves what the notice says unassessed, never scored as absent
    notice_readable = notice_found and _read(a)

    out: dict[str, dict[str, Any]] = {}

    def put(r: str, state: str, outcome: str, awarded: int | None = None, found: str = "", ev_refs: list[dict] | None = None, reason: str = "", blocked_by: str | None = None,
            limitation: str | None = None, confidence: str = "high") -> None:
        pts = POINTS[r]
        if awarded is None:
            awarded = pts if state == EARNED else (PARTIAL[r] if state == PART else 0)
        out[r] = {"ref": r, "domain": DOMAIN_OF[r], "name": NAME[r], "points": pts, "reach": REACH[r], "state": state, "outcome": outcome, "awarded": awarded if state in IN_PLAY else None,
                  "found": found, "evidence": ev_refs or [], "reason": reason, "blocked_by": blocked_by, "limitation": limitation, "confidence": confidence,
                  "statutory": legal_map.statutory_of(r, scan_day)}

    def beyond(r: str) -> bool:
        """Deep-only parameters are outside what a Quick Scan can reach: removed, and itemised (SC-13)."""
        if mode == "quick" and REACH[r] == "deep":
            put(r, NOT_ASSESSABLE, NOT_TESTED, reason="Beyond the reach of a Quick Scan; a Deep Scan assesses it.")
            return True
        return False

    def not_yet(r: str, why: str) -> None:
        put(r, NOT_ASSESSABLE, NOT_TESTED, reason=why)

    # A1
    if not beyond("A1"):
        if notice_found and (text_read or policies):
            # Whether a page read links to the notice is not recorded (a page's discovered_via is only how it was first found, so a footer link found after the sitemap would read as
            # "sitemap"). Linking is therefore stated as a limit of the scan, never scored against the site (SC-04).
            e = [ref(f"Privacy notice page found at {notice_url}" + (f" ({doc['characters']:,} characters of text read)" if doc and doc.get("characters") else ""), notice_url)]
            put("A1", EARNED, OBSERVED, found="A notice page was found and loads.", ev_refs=e, limitation="Whether every page links to it is not assessed in this scan.")
        elif notice_found:
            put("A1", NOT_ASSESSABLE, INCONCLUSIVE, reason="A privacy notice page was found but its text could not be read, so it could not be assessed.", ev_refs=[ref("Privacy notice page found", notice_url)])
        elif failed_policy:
            blocked = bool(_BLOCK_WORDS.search(failed_policy["reason"]))
            put("A1", NOT_ASSESSABLE, RESTRICTED if blocked else INCONCLUSIVE, ev_refs=[ref(f"A privacy page at this address did not load ({failed_policy['reason']})", failed_policy["url"])],
                reason=("The site does not allow the privacy page to be read" if blocked else "The privacy page did not load") + f" ({failed_policy['reason']}), so the notice was not assessed.")
        elif scanned == 0:
            put("A1", NOT_ASSESSABLE, RESTRICTED, reason="No page could be read, so the search for a notice was not made.")
        else:
            put("A1", NOT_MET, NOT_DETECTED, found=f"No privacy notice was found on the {scanned} page{'s' if scanned != 1 else ''} read.", ev_refs=[ref(searched("a privacy or data protection page, by page address and link text"), home)])

    def dependent(r: str, fn: Any) -> None:
        """A parameter that reads the notice: blocked when there is none, removed when we could not read it, otherwise judged on what it says."""
        if beyond(r):
            return
        a1 = out["A1"]
        if a1["state"] == NOT_MET:
            put(r, PREREQ, BLOCKED, found="The notice this depends on was not found.", blocked_by="A1", ev_refs=a1["evidence"])
        elif a1["state"] == NOT_ASSESSABLE or not policy_ran or not notice_readable:
            why = a1["reason"] if a1["state"] == NOT_ASSESSABLE else ("The policy analysis has not run, so what the notice says was not assessed." if not policy_ran else "The notice text could not be read.")
            put(r, NOT_ASSESSABLE, INCONCLUSIVE, reason=why)
        else:
            fn()

    def req(i: str) -> dict | None:
        return _find(a, i)

    def notice_ref(r: dict | None, what: str) -> list[dict]:
        return [ref(f"{what}: “{_clip((r or {}).get('quote') or '', 140)}”", (r or {}).get("source_url") or notice_url)] if r and (r.get("quote") or "").strip() else []

    def content(rid: str, key: str, what: str) -> None:
        def run() -> None:
            r = req(key)
            if _is_verified(r):
                put(rid, EARNED, OBSERVED, found=f"The notice states {what}.", ev_refs=notice_ref(r, "Notice wording"))
            elif r and r.get("status") == "partial":
                put(rid, PART, OBSERVED, found=f"The notice states part of {what}: {r.get('detail') or 'not every element was found'}.", ev_refs=notice_ref(r, "Notice wording"))
            else:
                put(rid, NOT_MET, NOT_DETECTED, found=f"The notice does not state {what}.",
                    ev_refs=[ref(f"Searched the notice text read from {notice_url} for {what}.", notice_url)], limitation="It may be given elsewhere than in the notice that was read.")
        dependent(rid, run)

    content("A2", "notice_data_itemised", "the personal data it collects")
    content("A3", "notice_purposes", "why personal data is collected and used")

    def a4() -> None:
        """Act s.5(1)(ii) and (iii) and Rules r.3(c): the notice says how to withdraw consent, how to exercise rights, and how to make a complaint **to the Board**. A grievance contact alone
        (Act s.8(10), scored under C2) is the Data Fiduciary's own mechanism, not the Board, so it earns half of that element."""
        withdrawal, rights, board, grievance = req("consent_withdrawal"), req("rights_means"), req("complaint_to_board"), req("grievance_contact")
        full = [k for k, r in (("withdraw consent", withdrawal), ("exercise rights", rights), ("complain to the Board", board)) if _is_verified(r)]
        half = [] if _is_verified(board) or not _is_verified(grievance) else ["complain (to the Data Fiduciary, not the Board)"]
        missing = [k for k in ("withdraw consent", "exercise rights", "complain to the Board") if k not in full]
        shown = next((r for r in (withdrawal, rights, board, grievance) if _is_verified(r)), None)
        units = len(full) + 0.5 * len(half)
        pts = POINTS["A4"]
        if len(full) == 3:
            put("A4", EARNED, OBSERVED, found="The notice gives a route to withdraw consent, to exercise rights and to make a complaint to the Board.", ev_refs=notice_ref(shown, "Notice wording"))
        elif units > 0:
            gives = [f"a route to {k}" for k in full + half]
            lacks = [f"a route to {m}" for m in missing if not (half and m == "complain to the Board")] + (["a route to the Board"] if half else [])
            put("A4", PART, OBSERVED, awarded=int(pts * units / 3 + 0.5), found=f"The notice gives {' and '.join(gives)}, but no {' and no '.join(l.removeprefix('a ') for l in lacks)}.", ev_refs=notice_ref(shown, "Notice wording"))
        else:
            put("A4", NOT_MET, NOT_DETECTED, found="The notice gives no route to withdraw consent, to exercise rights or to make a complaint to the Board.", ev_refs=[ref(f"Searched the notice text read from {notice_url} for these three routes.", notice_url)])
    dependent("A4", a4)
    if not beyond("A5"):
        ln, lg = (scan_signals or {}).get("notice_link_signals"), (scan_signals or {}).get("language_signals")
        a1 = out["A1"]
        if a1["state"] == NOT_MET:
            put("A5", PREREQ, BLOCKED, found="The notice this depends on was not found.", blocked_by="A1", ev_refs=a1["evidence"])
        elif a1["state"] == NOT_ASSESSABLE:
            put("A5", NOT_ASSESSABLE, INCONCLUSIVE, reason=a1["reason"])
        elif not (ln and ln.get("pages")) or not lg:
            put("A5", NOT_ASSESSABLE, NOT_TESTED, reason="Whether the notice is linked from every page and offered in another language was not recorded in this scan.")
        else:
            nl = req("notice_language")
            link_ok = ln["with_link"] == ln["pages"]
            switch = bool(lg.get("pages_with_indic_script")) or bool(lg.get("switcher_scripts")) or any(str(h).lower().split("-")[0] in _EIGHTH_SCHEDULE for h in lg.get("hreflang", []))
            lang_ok = _is_verified(nl) or switch
            e = [ref(f"{ln['with_link']} of {ln['pages']} pages read link to the privacy notice" + (f"; no link on {ln['missing'][0]}" if ln.get("missing") else ""), (ln.get("missing") or [home])[0])]
            e.append(ref("Language: " + ("the notice says it is offered in another Indian language" if _is_verified(nl) else "pages carry text, a switcher or alternates in an Eighth Schedule language" if switch else "no Eighth Schedule language was observed on the pages read"), home))
            n_ok = int(link_ok) + int(lang_ok)
            parts = []
            parts.append("The notice is linked from every page read." if link_ok else f"The notice is linked from {ln['with_link']} of {ln['pages']} pages read.")
            parts.append("It is offered in another Indian language." if lang_ok else "No other Indian language was observed.")
            if n_ok == 2:
                put("A5", EARNED, OBSERVED, found=" ".join(parts), ev_refs=e)
            elif n_ok == 1:
                put("A5", PART, OBSERVED, found=" ".join(parts), ev_refs=e)
            else:
                put("A5", NOT_MET, NOT_DETECTED, found=" ".join(parts), ev_refs=e, limitation="A language the page does not label or show in an Indian script cannot be seen.")

    # ── consent behaviour (Domain B) ──────────────────────────────────────────────────────────────────────────────────────────────
    signals = (ev or {}).get("consent_signals") or []
    sig = signals[0] if signals else None
    has_banner = bool(sig) and sig.get("mechanism_type") != "none" and sig.get("banner_displayed") is not False
    vendor = (sig or {}).get("cmp_vendor")

    def tcount(state: str, sends_only: bool = False) -> int:
        n = sum(1 for c in cookies if _TRACK.search(c.get("category") or "") and state in (c.get("consent_states") or []))
        return n + sum(1 for t in trackers if _TRACK.search(t.get("category") or "") and state in (t.get("consent_states") or [])
                       and not (sends_only and re.search(r"googletagmanager\.com/(gtm\.js|gtag/js)", t.get("script_src") or "", _F)))

    def denied_only(state: str) -> bool:
        x = [t for t in trackers if _TRACK.search(t.get("category") or "") and state in (t.get("consent_states") or []) and not re.search(r"googletagmanager\.com/(gtm\.js|gtag/js)", t.get("script_src") or "", _F)]
        return bool(x) and all(re.search(r"[?&]gcs=G100(&|$)", t.get("script_src") or "", _F) for t in x) and not any(
            _TRACK.search(c.get("category") or "") and state in (c.get("consent_states") or []) for c in cookies)

    any_tracking = any(_TRACK.search(c.get("category") or "") for c in cookies) or any(_TRACK.search(t.get("category") or "") for t in trackers)
    unclassified = sum(1 for x in (*cookies, *trackers) if not x.get("category") and not x.get("vendor"))
    names = ", ".join(list(dict.fromkeys(t["vendor"] for t in trackers if t.get("vendor") and _TRACK.search(t.get("category") or "")))[:4])

    if not beyond("B1"):
        if not detection_ran:
            put("B1", NOT_ASSESSABLE, RESTRICTED if restricted else INCONCLUSIVE, reason="The scan could not observe cookies and requests, so the need for a consent mechanism was not assessed.")
        elif sig and sig.get("mechanism_type") != "none" and sig.get("banner_displayed") is False:
            put("B1", NOT_ASSESSABLE, INCONCLUSIVE, reason="A consent tool was detected but its banner was never shown to the scanner, so whether visitors are offered a choice was not established.")
        elif has_banner:
            put("B1", EARNED, OBSERVED, found="A consent mechanism was found" + (f" ({vendor})." if vendor else "."), ev_refs=[ref("Consent mechanism detected on the home page" + (f" ({vendor})" if vendor else ""), home)])
        elif not any_tracking and unclassified == 0:
            put("B1", EARNED, OBSERVED, found="No consent mechanism was found and no analytics or advertising activity was observed, so none is needed.",
                ev_refs=[ref(f"No analytics or advertising request or cookie observed across {scanned} page{_plural(scanned, '', 's')} read", home)])
        elif not any_tracking:
            put("B1", NOT_ASSESSABLE, INCONCLUSIVE, reason=f"No consent mechanism was found and {unclassified} observed item{_plural(unclassified, ' could', 's could')} not be classified, so whether one is needed is not established.")
        else:
            put("B1", NOT_MET, POTENTIAL_ISSUE, found=f"Analytics or advertising activity was observed ({names or 'see inventory'}) and no consent mechanism was found.",
                ev_refs=[ref(f"{tcount('pre_consent')} analytics or advertising cookies and requests seen" + (f" ({names})" if names else ""), home), ref(searched("a consent banner or consent platform"), home)])

    tracks_nothing = out["B1"]["state"] == EARNED and not has_banner

    def banner_dependent(r: str, fn: Any) -> None:
        if beyond(r):
            return
        b1 = out["B1"]
        if b1["state"] == NOT_ASSESSABLE or cons is None:
            put(r, NOT_ASSESSABLE, NOT_TESTED, reason="Consent behaviour could not be tested on this scan." if b1["state"] != NOT_ASSESSABLE else b1["reason"])
        elif not has_banner and tracks_nothing:
            put(r, NA, NOT_APPLICABLE, reason="There is no consent mechanism to interact with, and none is needed because no analytics or advertising activity was observed.")
        elif not has_banner:
            put(r, PREREQ, BLOCKED, found="No consent mechanism was found, so this could not be met.", blocked_by="B1", ev_refs=b1["evidence"])
        else:
            fn()

    if not beyond("B2"):
        if cons is None or not detection_ran:
            put("B2", NOT_ASSESSABLE, NOT_TESTED, reason="The state before consent was not observed on this scan.")
        else:
            pre = tcount("pre_consent", True)
            known = 0 if trackers else cons["known"][0]
            n_pre = max(pre, known)
            e = [ref(f"Before any consent choice: {n_pre} analytics or advertising cookies and requests observed" + (f" ({names})" if n_pre and names else ""), home)]
            if pre == 0 and known == 0:
                put("B2", EARNED, OBSERVED, found="No analytics or advertising request was issued and no such cookie was stored before a choice.", ev_refs=e)
            elif pre > 0 and known == 0 and denied_only("pre_consent"):
                put("B2", PART, POTENTIAL_ISSUE, found="Google tags sent requests before a choice in consent-denied mode and no analytics or advertising cookie was stored. The requests still reach Google.", ev_refs=e)
            else:
                put("B2", NOT_MET, POTENTIAL_ISSUE, found=f"{max(pre, known)} analytics or advertising request{_plural(max(pre, known), '', 's')} or cookie{_plural(max(pre, known), '', 's')} observed before any choice.", ev_refs=e)

    def b3() -> None:
        rej = (sig or {}).get("has_reject_all")
        e = [ref("Consent mechanism examined on the home page" + (f" ({vendor})" if vendor else ""), home)]
        if rej is True:
            put("B3", EARNED, OBSERVED, found="A reject control was found at the first level of the banner.", ev_refs=e, limitation="Whether it is at the same level as Accept is judged from the banner as detected.")
        elif rej is False and ((sig or {}).get("evidence") or {}).get("reject_interaction") == "click_failed":
            # The scan FOUND a reject control and could not click it (a slow page, a busy server): that is not the same as the control being absent, and must not cost the site its marks.
            put("B3", NOT_ASSESSABLE, INCONCLUSIVE, reason="A reject control was found but could not be clicked in this scan, so whether it is offered on the first level of the banner was not established.", ev_refs=e)
        elif rej is False:
            put("B3", NOT_MET, NOT_DETECTED, found="No reject control was found on the first level of the banner.", ev_refs=[*e, ref(searched("a reject control on the consent mechanism"), home)])
        else:
            put("B3", NOT_ASSESSABLE, INCONCLUSIVE, reason="The scan could not tell whether a reject control is present.", ev_refs=e)
    banner_dependent("B3", b3)

    def b4() -> None:
        if not cons["reject_tested"]:
            put("B4", NOT_ASSESSABLE, NOT_TESTED, reason="Reject could not be clicked and confirmed, so what follows a refusal was not tested.")
            return
        post = tcount("post_reject", True)
        known_r = 0 if trackers else cons["known"][2]
        e = [ref(f"After Reject, in a fresh browser session: {max(post, known_r)} analytics or advertising cookies and requests observed", home)]
        if post == 0 and known_r == 0:
            put("B4", EARNED, OBSERVED, found="No analytics or advertising activity was observed after Reject.", ev_refs=e, limitation="Reject was tested once, on the home page.")
        elif post > 0 and denied_only("post_reject"):
            put("B4", PART, POTENTIAL_ISSUE, found="After Reject, Google tags sent requests in consent-denied mode and no analytics or advertising cookie was stored.", ev_refs=e)
        else:
            put("B4", NOT_MET, POTENTIAL_ISSUE, found="Analytics or advertising activity was still observed after Reject.", ev_refs=e)
    banner_dependent("B4", b4)
    withdrawal = ((sig or {}).get("evidence") or {}).get("withdrawal") if isinstance((sig or {}).get("evidence"), dict) else None

    def untested(r: str) -> bool:
        """The withdrawal test did not run to the withdraw click: say why in the site owner's words. Returns True when the parameter was settled here."""
        if not withdrawal:
            put(r, NOT_ASSESSABLE, NOT_TESTED, reason="Withdrawal was not tested in this scan.")
            return True
        if not withdrawal.get("tested"):
            put(r, NOT_ASSESSABLE, NOT_TESTED, reason=WITHDRAWAL_REASONS.get(withdrawal.get("reason") or "error", WITHDRAWAL_REASONS["error"]))
            return True
        return False

    def b5() -> None:
        if withdrawal and withdrawal.get("reason") == "no_reopen_control":
            put("B5", NOT_MET, NOT_DETECTED, found="No option to reopen or manage the cookie choice was found, so a visitor cannot take consent back.",
                ev_refs=[ref("Searched the home page, after Accept, for a cookie-settings control or a consent tool's reopen button", home)], limitation="A control that is only reached by scrolling a long page may be missed.")
            return
        if untested("B5"):
            return
        after = withdrawal.get("after_withdrawal") or []
        started = withdrawal.get("accepted_non_essential") or []
        e = [ref(f"After Accept, “{withdrawal.get('reopen_control')}” reopened the choice and “{withdrawal.get('withdraw_control')}” withdrew it; {len(after)} analytics or advertising service{_plural(len(after), '', 's')} contacted afterwards ({len(started)} had been active after Accept)", home)]
        if not after:
            put("B5", EARNED, OBSERVED, found="The choice can be reopened and withdrawn, and no analytics or advertising request followed.", ev_refs=e, limitation="Requests were observed; cookies were not read.")
        else:
            names = ", ".join(sorted({x.get("vendor") or x.get("host", "") for x in after})[:3])
            put("B5", NOT_MET, POTENTIAL_ISSUE, found=f"After consent was withdrawn, {len(after)} analytics or advertising service{_plural(len(after), ' was', 's were')} still contacted ({names}).", ev_refs=e)
    banner_dependent("B5", b5)

    def b6() -> None:
        if untested("B6"):
            return
        pages_seen = [p for p in withdrawal.get("further_pages") or [] if p.get("loaded")]
        if len(pages_seen) < 2:
            put("B6", NOT_ASSESSABLE, NOT_TESTED, reason=f"Only {len(pages_seen)} further page{_plural(len(pages_seen), '', 's')} could be opened in the same session, and persistence needs two.")
            return
        bad = [p for p in pages_seen if p.get("non_essential")]
        e = [ref(f"{len(pages_seen)} further pages opened in the same session after withdrawal; {sum(len(p['non_essential']) for p in pages_seen)} analytics or advertising service contacts", pages_seen[0]["url"])]
        if not bad:
            put("B6", EARNED, OBSERVED, found="The withdrawn choice was still honoured on two further pages opened in the same session.", ev_refs=e)
        else:
            put("B6", NOT_MET, POTENTIAL_ISSUE, found=f"Analytics or advertising was contacted again on {len(bad)} of {len(pages_seen)} further pages after consent was withdrawn.", ev_refs=[ref(f"{bad[0]['url']}: {len(bad[0]['non_essential'])} analytics or advertising service contact(s)", bad[0]["url"])])
    banner_dependent("B6", b6)

    # ── rights, grievance and contact (Domain C) ──────────────────────────────────────────────────────────────────────────────────
    def c1() -> None:
        """Act s.8(9) and Rules r.9 ask for the *business contact information* of a Data Protection Officer, if applicable, or of a person who can answer the Data Principal's questions. The analysis
        verifies `dpo_contact` only when the quoted words name such a contact with a contact detail beside it, so a verified one is that and earns the points. A name or a title is not what the
        provision asks for. A grievance contact alone is the grievance mechanism (C2), not a contact for privacy questions, so it earns half."""
        d, g = req("dpo_contact"), req("grievance_contact")
        if _is_verified(d):
            put("C1", EARNED, OBSERVED, found="Business contact information of a Data Protection Officer or of a person who can answer privacy questions is published.", ev_refs=notice_ref(d, "Contact wording"))
        elif _is_verified(g) or (d and d.get("status") == "partial"):
            put("C1", PART, OBSERVED, found="Only part of a contact for privacy questions is published: " + ("a grievance contact, but no contact for privacy questions." if _is_verified(g) else "a contact without a contact detail beside it."),
                ev_refs=notice_ref(g if _is_verified(g) else d, "Contact wording"))
        else:
            put("C1", NOT_MET, NOT_DETECTED, found="No privacy contact was found.", ev_refs=[ref(f"Searched the notice text read from {notice_url} for a Data Protection Officer or privacy contact.", notice_url)])
    dependent("C1", c1)

    def c2() -> None:
        g, t = req("grievance_contact"), req("grievance_timeline")
        if _is_verified(g):
            put("C2", EARNED, OBSERVED, found="A grievance route is published" + (" with a response period." if _is_verified(t) else "."), ev_refs=notice_ref(g, "Grievance wording"))
        elif g and g.get("status") == "partial":
            put("C2", PART, OBSERVED, found="Part of a grievance route is published.", ev_refs=notice_ref(g, "Grievance wording"))
        else:
            put("C2", NOT_MET, NOT_DETECTED, found="No grievance route was found.", ev_refs=[ref(f"Searched the notice text read from {notice_url} for a grievance route.", notice_url)])
    dependent("C2", c2)

    def c3() -> None:
        means = req("rights_means")
        rights = [req(i) for i in ("rights_access", "rights_correction_erasure", "rights_nominate")]
        n = sum(1 for r in rights if _is_verified(r))
        shown = next((r for r in rights if _is_verified(r)), means if _is_verified(means) else None)
        if _is_verified(means) and n >= 2:
            put("C3", EARNED, OBSERVED, found=f"The notice names {n} of 3 rights and the means of making a request.", ev_refs=notice_ref(shown, "Rights wording"))
        elif _is_verified(means) or n > 0:
            put("C3", PART, OBSERVED, found="Only part of the rights and the means of exercising them is published: " + ("the means and " + str(n) + " right" + _plural(n, "", "s") if _is_verified(means) else f"{n} right{_plural(n, '', 's')} but no means of making a request") + ".", ev_refs=notice_ref(shown, "Rights wording"))
        else:
            put("C3", NOT_MET, NOT_DETECTED, found="No statement of rights or of how to exercise them was found.", ev_refs=[ref(f"Searched the notice text read from {notice_url} for rights and how to exercise them.", notice_url)])
    dependent("C3", c3)

    if not beyond("C4"):
        def c4() -> None:
            r = req("rights_nominate")
            if _is_verified(r):
                put("C4", EARNED, OBSERVED, found="The right to nominate another individual is disclosed.", ev_refs=notice_ref(r, "Notice wording"))
            else:
                put("C4", NOT_MET, NOT_DETECTED, found="The right to nominate another individual was not found.", ev_refs=[ref(f"Searched the notice text read from {notice_url} for the right of nomination.", notice_url)])
        dependent("C4", c4)

    # ── children (Domain D). The objective test: a page that declares itself to be for children (page_signals.child_directed_signal). With none, the domain does not apply and its points are
    #    redistributed; the test never looks at who visits, and it is never the absence of a notice. A scan made before the test existed has no record, and is not assessable. ──
    surfaces = (scan_signals or {}).get("child_surfaces")
    child_urls = {x["url"] for x in surfaces or []}
    for r in ("D1", "D2"):
        if beyond(r):
            continue
        if surfaces is None:
            put(r, NOT_ASSESSABLE, NOT_TESTED, reason="Whether any page is aimed at children was not recorded in this scan.")
        elif not surfaces:
            put(r, NA, NOT_APPLICABLE, reason="No page read declares itself to be for children (by its address, title or text), so the children's provisions do not apply here.")
    if not beyond("D1") and surfaces:
        hits = [t for t in trackers if _TRACK.search(t.get("category") or "") and (t.get("page_url") or "") in child_urls]
        e = [ref(f"{len(child_urls)} page{_plural(len(child_urls), '', 's')} aimed at children: {surfaces[0]['url']} ({surfaces[0]['signal']})", surfaces[0]["url"])]
        if not detection_ran:
            put("D1", NOT_ASSESSABLE, INCONCLUSIVE, reason="Requests were not observed on this scan.")
        elif not hits:
            put("D1", EARNED, OBSERVED, found="No analytics, advertising or behavioural-tracking service was observed on the pages aimed at children.", ev_refs=e)
        else:
            put("D1", NOT_MET, POTENTIAL_ISSUE, found=f"{len(hits)} analytics or advertising request{_plural(len(hits), '', 's')} observed on a page aimed at children ({', '.join(sorted({t.get('vendor') or 'unrecognised' for t in hits})[:3])}).", ev_refs=e)
    if not beyond("D2") and surfaces:
        def d2() -> None:
            r = req("children_data")
            e = notice_ref(r, "Notice wording") or [ref(f"Searched the notice text read from {notice_url} for how parental consent is obtained", notice_url)]
            if _is_verified(r):
                put("D2", EARNED, OBSERVED, found="The notice describes how verifiable parental consent is obtained for children's data.", ev_refs=e)
            elif r and r.get("status") == "partial":
                put("D2", PART, OBSERVED, found="The notice mentions children's data but not a route to verifiable parental consent.", ev_refs=e)
            else:
                put("D2", NOT_MET, NOT_DETECTED, found="A page is aimed at children and the notice gives no route to age assurance or verifiable parental consent.", ev_refs=e, limitation="A route that is on the page itself and not in the notice is not read.")
        dependent("D2", d2)

    # ── third parties (Domain E) ──────────────────────────────────────────────────────────────────────────────────────────────────
    seen = [t for t in trackers if _TRACK.search(t.get("category") or "") and t.get("vendor")]
    vendors = list(dict.fromkeys(t["vendor"] for t in seen))
    cats = {("analytics" if re.search("analytic", t["category"], _F) else "advertising") for t in seen}
    sq = ((req("data_sharing") or {}).get("quote")) or ""

    def covered(v: str) -> bool:
        key = re.sub(r"\b(google|analytics|tag manager|ads|pixel)\b", "", v, flags=re.IGNORECASE).strip().lower()
        return v.lower() in sq.lower() or (len(key) > 2 and key in sq.lower())

    def category_named() -> bool:
        words = [r"analytic|measure" if c == "analytics" else r"advertis|marketing" for c in cats]
        return bool(sq) and sharing_names_recipients(sq) and all(re.search(w, sq, _F) for w in words)

    if not beyond("E1"):
        if not detection_ran:
            not_yet("E1", "The services the site contacts were not observed on this scan.")
        elif not vendors:
            put("E1", NA, NOT_APPLICABLE, reason="No third-party service that collects analytics or advertising data was identified.")
        else:
            def e1() -> None:
                named = [v for v in vendors if covered(v)]
                e = [ref(f"The scan contacted {', '.join(vendors[:5])}", home), *notice_ref(req("data_sharing"), "Sharing wording")]
                if len(named) == len(vendors) or category_named():
                    put("E1", EARNED, OBSERVED, found="The notice names or describes by category the services the site contacts.", ev_refs=e)
                elif named or (sq and sharing_names_recipients(sq)):
                    put("E1", PART, OBSERVED, found="The notice names some recipients but not every service the site contacts.", ev_refs=e)
                else:
                    put("E1", NOT_MET, NOT_DETECTED, found="The notice does not name or describe the services the site contacts.", ev_refs=e)
            dependent("E1", e1)
    if not beyond("E2"):
        known = [(v, org_of(v)) for v in dict.fromkeys(t["vendor"] for t in trackers if t.get("vendor"))]
        abroad = [(v, o) for v, o in known if o is not None and o.outside_india]
        unknown = [v for v, o in known if o is None]
        if not detection_ran:
            put("E2", NOT_ASSESSABLE, INCONCLUSIVE, reason="The services the site contacts were not observed on this scan.")
        elif not known:
            put("E2", NA, NOT_APPLICABLE, reason="No third-party service the scan recognises was contacted.")
        elif not abroad and unknown and len(unknown) == len(known):
            put("E2", NOT_ASSESSABLE, INCONCLUSIVE, reason=f"The organisation behind {', '.join(unknown[:3])} is not in the scan's table, so where it is established was not identified.")
        elif not abroad:
            put("E2", NA, NOT_APPLICABLE, reason="Every service the scan could place is established in India, so no transfer outside India is indicated.")
        else:
            def e2() -> None:
                r = req("cross_border")
                names = "; ".join(f"{o.organisation} ({o.country}) for {v}" for v, o in abroad[:4])
                e = [ref(f"Services established outside India: {names}. Established means where the organisation is registered, from its public details (provenance: research); where data is stored is not visible", home), *notice_ref(r, "Notice wording")]
                lim = f"{len(unknown)} recognised service{_plural(len(unknown), ' was', 's were')} not placed (not in the table)." if unknown else None
                if _is_verified(r):
                    put("E2", EARNED, OBSERVED, found="The notice says personal data may be processed outside India.", ev_refs=e, limitation=lim)
                elif r and r.get("status") == "partial":
                    put("E2", PART, OBSERVED, found="The notice touches on transfers outside India but does not clearly say that data may be processed there.", ev_refs=e, limitation=lim)
                else:
                    put("E2", NOT_MET, NOT_DETECTED, found="Services established outside India are contacted and the notice does not say that data may be processed outside India.", ev_refs=e, limitation=lim)
            dependent("E2", e2)
    if not beyond("E3"):
        if not detection_ran:
            not_yet("E3", "The services the site contacts were not observed on this scan.")
        elif not vendors:
            put("E3", NA, NOT_APPLICABLE, reason="No third-party service that collects analytics or advertising data was identified.")
        else:
            def e3() -> None:
                miss = [v for v in vendors if not covered(v)]
                e = [ref(f"The scan contacted {', '.join(vendors[:5])}", home)]
                if (not miss) or category_named():
                    put("E3", EARNED, OBSERVED, found="Every observed service that collects data is accounted for in the notice.", ev_refs=e)
                else:
                    put("E3", NOT_MET, POTENTIAL_ISSUE, found=f"The notice does not account for {', '.join(miss[:3])}.", ev_refs=[*e, *notice_ref(req("data_sharing"), "Sharing wording")])
            dependent("E3", e3)

    # ── security of the public surface (Domain F) ─────────────────────────────────────────────────────────────────────────────────
    if not beyond("F1"):
        if scanned == 0 or not detection_ran:
            put("F1", NOT_ASSESSABLE, RESTRICTED if restricted else INCONCLUSIVE, reason="No page could be read, so how pages are served was not assessed.")
        else:
            insecure_pages = [u for u in read_urls if u.lower().startswith("http://")]
            insecure_src = [t["script_src"] for t in trackers if (t.get("script_src") or "").lower().startswith("http://")]
            if not insecure_pages and not insecure_src:
                put("F1", EARNED, OBSERVED, found="Every page read was served over a secure connection and no insecure request was observed.", ev_refs=[ref(f"{scanned} page{_plural(scanned, '', 's')} read over HTTPS", home)])
            else:
                put("F1", NOT_MET, POTENTIAL_ISSUE, found=f"{len(insecure_pages)} page{_plural(len(insecure_pages), '', 's')} served without a secure connection and {len(insecure_src)} insecure request{_plural(len(insecure_src), '', 's')} observed.",
                    ev_refs=[ref(f"Not secure: {(insecure_pages + insecure_src)[0]}", (insecure_pages or [home])[0])])

    # ── collection practice (Domain G) ────────────────────────────────────────────────────────────────────────────────────────────
    personal = [f for f in forms if any(x.get("field_type") in ("email", "tel", "text", "password", "textarea") or re.search(r"name|mail|phone|mobile", x.get("name") or "", _F) for x in f.get("fields", []))]

    def submit_of(f: dict) -> str:
        """Where the form posts to: `submit_url` as the evidence API serves it ("action_url" on a raw scanner record)."""
        return f.get("submit_url") or f.get("action_url") or ""

    def has_control(f: dict) -> bool:
        return any(x.get("field_type") in ("checkbox", "radio") for x in f.get("fields", []))

    form_signals = (scan_signals or {}).get("form_signals")
    notice_by_form = {(x.get("page_url"), x.get("selector")): bool(x.get("notice_link")) for x in form_signals or []}

    def notice_link_of(f: dict) -> bool | None:
        """Whether this form has a link to the notice at the point of collection: True or False as the crawler saw it, None when it did not record the form."""
        if form_signals is None:
            return None
        return notice_by_form.get((f.get("page_url"), f.get("selector")))

    if not beyond("G1"):
        if not detection_ran:
            put("G1", NOT_ASSESSABLE, INCONCLUSIVE, reason="Forms were not observed on this scan.")
        elif not personal:
            put("G1", NA, NOT_APPLICABLE, reason="No form that collects personal details was found.")
        else:
            with_c = [f for f in personal if has_control(f) or notice_link_of(f) is True]
            seen_links = all(notice_link_of(f) is not None for f in personal)
            e = [ref(f"{len(personal)} form{_plural(len(personal), '', 's')} that collect personal details; {len(with_c)} with a consent control or a link to the notice beside the form", personal[0].get("page_url") or home)]
            if len(with_c) == len(personal):
                put("G1", EARNED, OBSERVED, found="Every form that collects personal details presents a consent control or links to the notice.", ev_refs=e)
            elif with_c:
                put("G1", PART, OBSERVED, awarded=round(POINTS["G1"] * len(with_c) / len(personal)), found=f"{len(with_c)} of {len(personal)} forms present a consent control or a link to the notice; the rest present neither.", ev_refs=e)
            elif seen_links:
                put("G1", NOT_MET, NOT_DETECTED, found="No form that collects personal details presents a consent control or links to the notice beside the form.", ev_refs=e)
            else:
                put("G1", NOT_ASSESSABLE, INCONCLUSIVE, reason="No consent control was found beside any form, and a link to the notice beside a form is not yet captured by this scanner version, so this was not scored against the site.", ev_refs=e)
    if not beyond("F2"):
        if not detection_ran:
            not_yet("F2", "Forms were not observed on this scan.")
        elif not personal:
            put("F2", NA, NOT_APPLICABLE, reason="No form that collects personal details was found.")
        else:
            bad = [f for f in personal if submit_of(f).lower().startswith("http://")]
            far = [f for f in personal if _host(submit_of(f)) and _host(submit_of(f)) != _host(home)]
            e = [ref(f"{len(personal)} form{_plural(len(personal), '', 's')} that collect personal details inspected, none submitted", home)]
            if bad:
                put("F2", NOT_MET, POTENTIAL_ISSUE, found=f"{len(bad)} form{_plural(len(bad), '', 's')} submit{'s' if len(bad) == 1 else ''} over an insecure connection.", ev_refs=e)
            elif far:
                put("F2", PART, OBSERVED, found="Forms submit securely, but at least one submits to another organisation's address that this scan cannot show the notice accounts for.", ev_refs=e)
            else:
                put("F2", EARNED, OBSERVED, found="Every form submits over a secure connection to the site's own address.", ev_refs=e)
    if not beyond("G2"):
        if not personal:
            put("G2", NA, NOT_APPLICABLE, reason="No form that collects personal details was found.")
        else:
            controls = [(f, x) for f in personal for x in f.get("fields", []) if x.get("field_type") in ("checkbox", "radio") and _CONSENT_LIKE.search(f"{x.get('label') or ''} {x.get('name') or ''}")]
            if any(x.get("checked") is None for _f, x in controls):
                put("G2", NOT_ASSESSABLE, NOT_TESTED, reason="Whether a form's consent control starts ticked was not recorded in this scan.")
            else:
                ticked = [(f, x) for f, x in controls if x.get("checked")]
                e = [ref(f"{len(controls)} consent control{_plural(len(controls), '', 's')} on {len(personal)} form{_plural(len(personal), '', 's')} that collect personal details, read as each page loaded; {len(ticked)} already ticked", (ticked or controls or [(personal[0], {})])[0][0].get("page_url") or home)]
                if ticked:
                    put("G2", NOT_MET, POTENTIAL_ISSUE, found=f"{len(ticked)} consent control{_plural(len(ticked), ' is', 's are')} already ticked when the page loads (“{(ticked[0][1].get('label') or ticked[0][1].get('name') or '')[:80]}”).", ev_refs=e)
                else:
                    put("G2", EARNED, OBSERVED, found="No consent control on any form is pre-selected.", ev_refs=e)

    # ── readiness indicators: not scored ─────────────────────────────────────────────────────────────────────────────────────────
    indicators: list[dict[str, Any]] = []
    a1s = out["A1"]
    for iid, iname, rid in INDICATORS:
        row: dict[str, Any] = {"id": iid, "name": iname, "scored": False, "outcome": INCONCLUSIVE, "found": "", "evidence": [], "statutory": legal_map.statutory_of(iid, scan_day)}
        if iid == "I9":
            pu = ((sig or {}).get("evidence") or {}).get("purposes") if isinstance((sig or {}).get("evidence"), dict) else None
            if mode == "quick":
                row["outcome"], row["found"] = NOT_TESTED, "Choices by purpose are tested in a Deep Scan."
            elif not has_banner and tracks_nothing:
                row["outcome"], row["found"] = NOT_APPLICABLE, "There is no consent mechanism, and none is needed because no analytics or advertising activity was observed."
            elif not pu:
                row["outcome"], row["found"] = NOT_TESTED, "Choices by purpose were not tested in this scan."
            elif not pu.get("tested"):
                row["outcome"], row["found"] = NOT_TESTED, PURPOSE_REASONS.get(pu.get("reason") or "error", PURPOSE_REASONS["error"])
            else:
                tested = pu.get("purposes") or []
                bad = [x for x in tested if x.get("verdict") == "unexpected"]
                lines = "; ".join(f"{x['label']}: " + (", ".join(sorted({o.get('vendor') or o.get('host', '') for o in x.get('observed', [])})) or "nothing contacted") for x in tested[:4])
                row["evidence"] = [ref(f"Each purpose switched on alone in a fresh session, then saved. {lines}", home)]
                if bad:
                    row["outcome"] = POTENTIAL_ISSUE
                    row["found"] = f"With only “{bad[0]['label']}” switched on, analytics or advertising outside that purpose was still contacted ({', '.join(sorted({o.get('vendor') or o.get('host', '') for o in bad[0]['observed']})[:3])})."
                else:
                    row["outcome"] = OBSERVED
                    row["found"] = f"{len(tested)} purpose{_plural(len(tested), '', 's')} tested independently; each contacted only what its name says. Requests were observed; cookies were not read."
        elif iid == "I8":
            hdrs = ((scan_signals or {}).get("security_signals") or {}).get("homepage_headers")
            if hdrs is None:
                row["found"] = "The home page's response headers were not recorded in this scan."
            else:
                sent = [_HEADER_NAMES[k] for k in _HEADER_NAMES if hdrs.get(k)]
                row["evidence"] = [ref("Home page response headers: " + (", ".join(sent) if sent else "none of the six headers browsers use as safeguards"), home)]
                row["outcome"] = OBSERVED if sent else NOT_DETECTED
                row["found"] = (f"The home page sends {len(sent)} of 6 safeguard headers ({', '.join(sent)})." if sent else "The home page sends none of the six safeguard headers.") + " Headers are a signal, not the safeguards themselves."
        elif a1s["state"] == NOT_MET:
            row["outcome"], row["found"] = BLOCKED, "The notice this depends on was not found."
        elif a1s["state"] == NOT_ASSESSABLE or not policy_ran or not notice_readable:
            row["found"] = "The notice could not be read, so this was not assessed."
        else:
            r = req(rid)
            if _is_verified(r):
                row["outcome"], row["found"], row["evidence"] = OBSERVED, f"The notice states it: {iname.lower()}.", notice_ref(r, "Notice wording")
            elif r and r.get("status") == "partial":
                row["outcome"], row["found"], row["evidence"] = OBSERVED, f"The notice states part of it: {iname.lower()}.", notice_ref(r, "Notice wording")
            else:
                row["outcome"], row["found"] = NOT_DETECTED, f"Not found in the notice text read: {iname.lower()}."
                row["evidence"] = [ref(f"Searched the notice text read from {notice_url} for it.", notice_url)]
        indicators.append(row)

    # ── the calculation (section 4, SC-14) ────────────────────────────────────────────────────────────────────────────────────────
    params = [out[p[0]] for p in PARAMETERS]
    reach_pts = sum(p["points"] for p in params if mode == "deep" or p["reach"] == "both")
    in_play = sum(p["points"] for p in params if p["state"] in IN_PLAY)
    awarded = sum(p["awarded"] or 0 for p in params if p["state"] in IN_PLAY)
    beyond_pts = sum(p["points"] for p in params if p["reach"] == "deep" and mode == "quick")
    unassessed_pts = sum(p["points"] for p in params if p["state"] == NOT_ASSESSABLE and not (mode == "quick" and p["reach"] == "deep"))
    na_pts = sum(p["points"] for p in params if p["state"] == NA)
    evaluated_pts = reach_pts - unassessed_pts
    coverage = round(100 * evaluated_pts / reach_pts) if reach_pts else 0
    share = None if in_play == 0 else (100 * awarded / in_play)
    raw = None if share is None else int(share + 0.5)

    # ── evidence confidence ───────────────────────────────────────────────────────────────────────────────────────────────────────
    reasons: list[str] = []
    level = "high"
    if restricted:
        level = "low"
        reasons.append("The site blocked the scanner or more than a fifth of the pages tried could not be read.")
    if scanned == 0:
        level = "low"
        reasons.append("No page could be read.")
    if has_banner and cons is not None and not (cons["accept_tested"] and cons["reject_tested"]):
        level = "low"
        reasons.append("A consent mechanism was found but Accept or Reject could not be tested.")
    if coverage < 60:
        level = "low"
        reasons.append(f"Only {coverage}% of the points this scan can reach could be assessed.")
    if level != "low":
        if sp_failed or (sp or {}).get("stop_reason") or not policy_ran or discovered and discovered > scanned + 5:
            level = "medium"
            if sp_failed:
                reasons.append(f"{len(sp_failed)} page{_plural(len(sp_failed), '', 's')} could not be read.")
            if (sp or {}).get("stop_reason"):
                reasons.append(sp["stop_reason"])
            if not policy_ran:
                reasons.append("The policy analysis has not run.")
            if discovered and discovered > scanned + 5:
                reasons.append(f"{scanned} of {discovered} pages found were read.")
        if mode == "quick" and level == "high":
            level = "medium"
            reasons.append("Consent was tested once, on the home page.")
    if not reasons:
        reasons.append("Every planned page was read and every state tested.")

    # ── the band (section 7) ──────────────────────────────────────────────────────────────────────────────────────────────────────
    def band_of(p: float) -> str:
        return "Evidence-led" if p >= 85 else "Substantially addressed" if p >= 70 else "Developing" if p >= 50 else "Needs attention" if p >= 25 else "Early stage"

    band_note: list[str] = []
    indicative = None
    if share is None:
        band, score = INSUFFICIENT, None
        band_note.append("No parameter could be assessed.")
    else:
        score = raw
        band = band_of(round(share))
        if mode == "quick":
            band_note.append("A Quick Scan assesses the parameters it can reach; the others are left out of the score and listed as not assessed, and a Deep Scan assesses them.")
        if coverage < 60:
            band = INSUFFICIENT
            band_note.append(f"Fewer than 60% of the points this scan can reach ({coverage}%) could be assessed, so no settled band and no score are given (L-09, L-12).")
            indicative, score = score, None      # a site we could barely see never receives an apparently definitive number
        elif level == "low" and band in BANDS:
            band = BANDS[max(0, BANDS.index(band) - 1)]
            band_note.append("Evidence confidence is low, so the band is held one level lower.")
        # SC-12 / SC-37: 100 only from a Deep Scan at full coverage with high confidence
        if score is not None and score >= 100 and not (mode == "deep" and coverage == 100 and level == "high"):
            score = QUICK_MAX
            band_note.append("A score of 100 needs a Deep Scan at full coverage with high evidence confidence, so it is held at 99.")

    # ── findings, remedies, itemisations ──────────────────────────────────────────────────────────────────────────────────────────
    gap_states = (NOT_MET, PREREQ, PART)
    gaps = [p for p in params if p["state"] in gap_states]
    primary = [p for p in gaps if p["state"] != PREREQ]
    consequences: dict[str, list[str]] = {}
    for p in gaps:
        if p["state"] == PREREQ and p["blocked_by"]:
            consequences.setdefault(p["blocked_by"], []).append(p["ref"])

    def sev(p: dict) -> str:
        lost = p["points"] - (p["awarded"] or 0)
        for c in consequences.get(p["ref"], []):
            lost += POINTS[c]
        return "High" if lost >= 8 else "Medium" if lost >= 4 else "Low"

    conf_of = {"high": "High", "medium": "Medium", "low": "Low"}
    findings: list[dict[str, Any]] = []
    for p in sorted(primary, key=lambda x: (-(x["points"] - (x["awarded"] or 0) + sum(POINTS[c] for c in consequences.get(x["ref"], []))), x["ref"])):
        miss, why, action, owner, effort = PHRASES[p["ref"]]
        severity, confidence = sev(p), conf_of[level]
        lead = "Worth investigating: " if severity == "High" and level == "low" else ""
        findings.append({
            "ref": p["ref"], "domain": p["domain"], "title": p["name"], "outcome": p["outcome"], "state": p["state"], "severity": severity, "confidence": confidence,
            "observed": lead + (p["found"] or miss), "evidence": p["evidence"], "why_it_matters": why, "action": action, "owner": owner, "effort": effort,
            "consequences": consequences.get(p["ref"], []), "points_at_stake": p["points"] - (p["awarded"] or 0) + sum(POINTS[c] for c in consequences.get(p["ref"], [])),
            "statutory": legal_map.statutory_of(p["ref"], scan_day),
        })
    # a remedy is offered only against a parameter that scored zero or partial with evidence held (SC-33, SC-36), and is worded as what would close the gap (SC-34)
    remedies = []
    for f in findings:
        d = f["domain"]
        remedies.append({"ref": f["ref"], "closes": f["action"], "capability": CAPABILITY[d] if CAPABILITY_LIVE.get(d) else None, "points_at_stake": f["points_at_stake"], "effort": f["effort"]})
    remedies.sort(key=lambda r: (-r["points_at_stake"], {"Low": 0, "Medium": 1, "High": 2}[r["effort"]], r["ref"]))
    if mode == "quick":
        remedies = remedies[:QUICK_REMEDY_CAP]

    removed = [{"ref": p["ref"], "name": p["name"], "points": p["points"], "state": p["state"], "reason": p["reason"]} for p in params if p["state"] in (NOT_ASSESSABLE, NA)]
    itemised_beyond = [{"ref": p["ref"], "name": p["name"], "points": p["points"]} for p in params if mode == "quick" and p["reach"] == "deep"]

    what_not = [
        "Records of processing, processing agreements, retention in practice, how rights requests are handled and breach readiness are not visible from the public site and were not assessed.",
        "This is not a security assessment.",
    ]
    n_na = sum(1 for p in params if p["state"] == NA)
    n_un = sum(1 for p in params if p["state"] == NOT_ASSESSABLE and not (mode == "quick" and p["reach"] == "deep"))
    n_applied = len(params) - n_na - (sum(1 for p in params if mode == "quick" and p["reach"] == "deep"))
    explanation = (
        f"{n_applied} of the {len(params) - sum(1 for p in params if mode == 'quick' and p['reach'] == 'deep')} parameters this scan type assesses applied to this website. "
        "The score is the share of the assessable points the website earned, expressed out of 100"
        + (f"; {n_na} parameter{_plural(n_na, ' was', 's were')} not applicable and {n_un} could not be assessed, and {_plural(n_na + n_un, 'it is', 'they are')} left out of the calculation" if (n_na or n_un) else "")
        + "."
    )
    result: dict[str, Any] = {
        "version": SCORE_VERSION, "rules": RULES, "mode": mode, "status": "restricted" if restricted else "completed",
        "score": score, "indicative_score": indicative, "score_text": "No meaningful score" if score is None else f"{score}/100", "scale": SCALE, "band": band, "band_notes": band_note,
        "coverage": coverage, "confidence": {"level": level, "label": conf_of[level], "reasons": reasons},
        "explanation": explanation, "points": {"awarded": awarded, "in_play": in_play, "reachable": reach_pts, "not_applicable": na_pts, "not_assessable": unassessed_pts, "beyond_scan_type": beyond_pts},
        "parameters": params, "indicators": indicators, "purposes": ((sig or {}).get("evidence") or {}).get("purposes") if isinstance((sig or {}).get("evidence"), dict) else None,
        "domains": _domain_summary(params), "findings": findings, "top_findings": findings[:3], "priority_actions": findings[:5], "more_actions": max(0, len(findings) - 5),
        "remedies": remedies, "removed": removed, "beyond_quick_scan": itemised_beyond, "what_was_not_assessed": what_not,
        "pages": {"discovered": discovered, "scanned": scanned, "failed": len(sp_failed), "skipped": sum(x["count"] for x in ((sp or {}).get("skipped") or []))},
        "scanned_at": scanned_at,
        "disclaimer": "An observation of a public website on the date shown. It is not a statement that the website does or does not comply with the DPDP Act, and it is not a security assessment.",
    }
    if score == 100:
        result["perfect_score_notice"] = _perfect_notice(len(trackers), scanned, discovered)
    validate_result(result)
    return result


def _domain_summary(params: list[dict]) -> list[dict[str, Any]]:
    rows = []
    for d, meta in DOMAINS.items():
        ps = [p for p in params if p["domain"] == d]
        rows.append({"domain": d, "name": meta["name"], "anchor": meta["anchor"], "relation": meta["relation"], "commences": COMMENCEMENT, "points": sum(p["points"] for p in ps),
                     "awarded": sum(p["awarded"] or 0 for p in ps if p["state"] in IN_PLAY), "in_play": sum(p["points"] for p in ps if p["state"] in IN_PLAY)})
    return rows


def _perfect_notice(third_parties: int, pages: int, found: int | None) -> dict[str, Any]:
    cadence = "weekly" if third_parties > 15 or (found or pages) > 200 else "monthly"
    return {
        "text": "This score describes the website as observed on this date and at this time. It will not stay true on its own.",
        "moves_it": ["A marketing tag added through a tag manager", "A plugin or theme update that adds a tracker or changes the banner", "A new embedded video, chat widget, font, map or feed",
                     "A change to the consent platform's configuration", "A campaign landing page outside the main template", "A vendor changing its own cookies", "A new form with no consent control", "A redesign"],
        "not_covered": "It assesses the public surface only and says nothing about records of processing, processing agreements, retention in practice, handling of rights requests or breach readiness.",
        "recommended_cadence": cadence,
        "cadence_reason": f"{third_parties} third-party requests and {found or pages} pages were observed.",
    }


def validate_result(r: dict[str, Any]) -> None:
    """The validated-result gate. Raises ResultRefused rather than issue a weaker or contradictory report (EV-02, EV-07, GR-08, GR-19, GR-20, L-02)."""
    for p in r["parameters"]:
        if p["outcome"] not in OUTCOMES:
            raise ResultRefused(f"{p['ref']}: outcome {p['outcome']!r} is not in the permitted list")
        if p["state"] in (EARNED, PART, NOT_MET) and not p["evidence"]:
            raise ResultRefused(f"{p['ref']}: an asserting finding has no evidence")
        if p["state"] in (NOT_ASSESSABLE, NA) and p["awarded"] is not None:
            raise ResultRefused(f"{p['ref']}: an outcome that asserts nothing carries a score")
        if p["state"] == EARNED and p["outcome"] not in ASSERTING:
            raise ResultRefused(f"{p['ref']}: earned points under a non-asserting outcome")
        if p["state"] == PREREQ and not p["blocked_by"]:
            raise ResultRefused(f"{p['ref']}: blocked by a prerequisite but no blocking finding is named")
        for t in (p["found"], p["reason"]):
            if prohibited_wording(t):
                raise ResultRefused(f"{p['ref']}: prohibited wording {prohibited_wording(t)}")
    for i in r.get("indicators", []):
        if i["outcome"] not in OUTCOMES or i["scored"]:
            raise ResultRefused(f"{i['id']}: an indicator carries an outcome outside the list or a score")
        if i["outcome"] in ASSERTING and not i["evidence"]:
            raise ResultRefused(f"{i['id']}: an asserting indicator has no evidence")
        if prohibited_wording(i["found"]):
            raise ResultRefused(f"{i['id']}: prohibited wording")
    # EV-07: no sentence may say activity was observed in a state the figures record as none
    for p in r["parameters"]:
        if p["ref"] in ("B2", "B4") and p["state"] in (NOT_MET, PART) and not re.search(r"\b[1-9]\d* (analytics|analytic)", " ".join(e["observed"] for e in p["evidence"])) and "denied" not in p["found"]:
            raise ResultRefused(f"{p['ref']}: wording asserts activity the evidence does not record")
    for f in r["findings"]:
        bad = prohibited_wording(f["observed"] + " " + f["why_it_matters"] + " " + f["action"])
        if bad:
            raise ResultRefused(f"finding {f['ref']}: prohibited wording {bad}")
        if f["ref"] in r["removed"]:
            raise ResultRefused("a removed parameter appears as a finding")
    if prohibited_wording(r["explanation"] + " " + r["disclaimer"]):
        raise ResultRefused("prohibited wording in the report text")
    if r["score"] is not None and (not isinstance(r["score"], int) or r["score"] < 0 or r["score"] > 100):
        raise ResultRefused("score is not a whole number from 0 to 100")
    if r["mode"] == "quick" and r["score"] is not None and r["score"] > QUICK_MAX:
        raise ResultRefused("a Quick Scan can never issue a score of 100")
