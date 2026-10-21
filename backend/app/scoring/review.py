"""What a scan confirms, what it cannot, and what a person must decide -- the "Logic Required / Human Review" content.

Derived from the SAME evidence context and requirement results as the DPDP score, so the backend, the dashboard and the PDF
cannot tell different stories. Nothing here decides legal outcomes: a technical signal (a third-party request, a cookie) is
reported as an observation, and anything that turns on business context or legal interpretation is listed for a person.

Vocabulary (kept deliberately separate):
  network request  a third-party request the scanned page made.
  known tracker    a request that matches the catalogue of analytics/marketing services. That the tracker actually ran and what
                   data it sent is NOT established by the request.
  cookie           a cookie the browser stored.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from app.rules.consent_rules import ConsentContext

STATEMENT = (
    "A website scan cannot establish complete DPDP compliance. It shows what the scanned pages did in this scan. A network "
    "request is not proof that a tracker ran, and a cookie or third-party request is not by itself a legal violation: the items "
    "below need business context or legal interpretation before they are treated as violations."
)

NEEDS = ("business_context", "legal_interpretation", "more_evidence")


@dataclass
class StateCounts:
    pre_consent: int = 0
    post_accept: int = 0
    post_reject: int = 0


@dataclass
class EvidenceSummary:
    """The three kinds of technical signal, counted apart. Counts are of rows observed on the scanned main-domain pages."""
    network_requests: int
    known_tracker_requests: int
    unclassified_requests: int
    cookies: int
    known_tracker_cookies: int
    unclassified_cookies: int
    network_requests_by_state: StateCounts
    known_tracker_requests_by_state: StateCounts
    cookies_by_state: StateCounts
    pages_scanned: int
    note: str = (
        "Network requests, known tracker requests and cookies are different things. A request shows the browser contacted a "
        "service; it does not show that a tracker ran or what it sent. None of these counts is a finding on its own."
    )


@dataclass
class ReviewItem:
    id: str
    category: str                      # "uncertain": a finding that needs interpretation; "missing_evidence": the scan could not see it
    topic: str
    needs: list[str]
    known: list[str]                   # what the scan confirms
    missing: list[str]                 # the evidence or context that is not available
    decision: str                      # what a person has to decide
    provisions: list[str] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    requirement_ids: list[str] = field(default_factory=list)


@dataclass
class GapItem:
    """A confirmed gap (Not Met) or a partly met requirement, with what to do about it."""
    requirement_id: str
    title: str
    status: str                        # "not_met" or "partially_met"
    timing: str                        # "in_force" (a current gap) or "future" (a readiness gap: the duty is not yet in force)
    applies_from: str                  # ISO date the duty starts
    provisions: list[str]
    weight: int
    points: float
    what_the_scan_observed: list[str]
    pages: list[str]
    evidence_ids: list[str]
    rule_ids: list[str]
    why_it_matters: str
    recommended_action: str
    how_to_verify: str


@dataclass
class MissingEvidenceItem:
    requirement_id: str
    title: str
    provisions: list[str]
    weight: int
    reason: str
    kind: str                          # "scan_limit" (a scan could observe it but did not) or "organisational" (it never can)


@dataclass
class Breakdown:
    verified_gaps: list[GapItem]       # Not Met, and the duty is in force now
    readiness_gaps: list[GapItem]      # Not Met, but the duty is not yet in force: never a current violation
    partially_satisfied: list[GapItem]
    missing_evidence: list[MissingEvidenceItem]
    uncertain_count: int


@dataclass
class HumanReview:
    statement: str
    items: list[ReviewItem]


def _states(rows: list[dict]) -> StateCounts:
    c = StateCounts()
    for r in rows:
        s = r.get("consent_states", [])
        c.pre_consent += "pre_consent" in s
        c.post_accept += "post_accept" in s
        c.post_reject += "post_reject" in s
    return c


def summarise_evidence(ctx: ConsentContext) -> EvidenceSummary:
    known_t = [t for t in ctx.trackers if t.get("category") in ("analytics", "marketing")]
    known_c = [c for c in ctx.cookies if c.get("category") in ("analytics", "marketing")]
    return EvidenceSummary(
        network_requests=len(ctx.trackers),
        known_tracker_requests=len(known_t),
        unclassified_requests=sum(1 for t in ctx.trackers if t.get("category") is None),
        cookies=len(ctx.cookies),
        known_tracker_cookies=len(known_c),
        unclassified_cookies=sum(1 for c in ctx.cookies if c.get("category") is None),
        network_requests_by_state=_states(ctx.trackers),
        known_tracker_requests_by_state=_states(known_t),
        cookies_by_state=_states(ctx.cookies),
        pages_scanned=len(ctx.pages),
    )


def build_breakdown(results: list, review_items: list[ReviewItem]) -> Breakdown:
    def gap(r) -> GapItem:
        return GapItem(
            r.id, r.title, r.status, r.timing, r.effective.isoformat(), list(r.provisions), r.weight, r.points_earned,
            list(r.observations), list(r.pages), list(r.evidence_ids), list(r.rule_ids), r.why_it_matters, r.remediation,
            r.verification,
        )

    missing = []
    for r in results:
        if r.status == "unable_to_verify":
            organisational = r.id in _ORGANISATIONAL
            missing.append(MissingEvidenceItem(
                r.id, r.title, list(r.provisions), r.weight,
                (r.observations or ["Not observed."])[0], "organisational" if organisational else "scan_limit"))
    return Breakdown(
        verified_gaps=[gap(r) for r in results if r.status == "not_met" and r.in_force],
        readiness_gaps=[gap(r) for r in results if r.status == "not_met" and not r.in_force],
        partially_satisfied=[gap(r) for r in results if r.status == "partially_met"],
        missing_evidence=missing,
        uncertain_count=sum(1 for i in review_items if i.category == "uncertain"),
    )


# Requirements a public website scan can never observe (the score registry's scan_assessable=False ones).
_ORGANISATIONAL = frozenset({"DPDP-R9", "DPDP-R14A", "DPDP-R14B", "DPDP-S6", "DPDP-S7", "DPDP-X4", "DPDP-X8", "DPDP-X10", "DPDP-X13"})


def build_human_review(ctx: ConsentContext, results: list, as_of: date, max_pages: int) -> HumanReview:
    by_id = {r.id: r for r in results}
    items: list[ReviewItem] = []

    def status(rid: str) -> str:
        return by_id[rid].status if rid in by_id else ""

    # --- known tracker requests before consent: the scan observed requests; whether that is a violation is not its call ------
    c1 = by_id.get("DPDP-C1")
    if c1 is not None and c1.status == "not_met":
        items.append(ReviewItem(
            id="known-tracker-before-consent", category="uncertain",
            topic="Requests to known analytics or advertising services before consent",
            needs=["business_context", "legal_interpretation"],
            known=[
                (f"{len(c1.evidence_ids)} request(s) or cookie(s) matching known analytics/marketing services were observed on "
                 "the scanned pages before any consent interaction."),
                "The browser contacted those services. This is evidence of a request, not proof that a tracker ran or of what it sent.",
            ],
            missing=[
                "Whether personal data was actually sent, and which.",
                "The purpose each service serves for the business, and whether any of them is strictly necessary for the service the visitor asked for.",
                "Whether the organisation relies on a ground other than consent (a legitimate use under the Act) for any of this processing.",
            ],
            decision="Decide, with legal advice, whether this processing needed consent and whether it was lawful without it. "
                     "If it did, move the services behind consent and re-scan.",
            provisions=list(c1.provisions), evidence_ids=list(c1.evidence_ids), requirement_ids=["DPDP-C1"],
        ))
    elif c1 is not None and c1.status == "partially_met":
        items.append(ReviewItem(
            id="unclassified-before-consent", category="uncertain",
            topic="Third-party requests whose purpose is unknown, seen before consent",
            needs=["business_context"],
            known=[f"{len(c1.evidence_ids)} request(s) or cookie(s) did not match any known service and fired before any consent interaction."],
            missing=["What each one is for, and whether it is strictly necessary for the service the visitor asked for."],
            decision="Identify each one with the engineering team. Treat it as non-essential, and put it behind consent, unless it is necessary.",
            provisions=list(c1.provisions), evidence_ids=list(c1.evidence_ids), requirement_ids=["DPDP-C1"],
        ))

    unclassified_after = [r for r in (*ctx.trackers, *ctx.cookies) if r.get("category") is None]
    if unclassified_after and not (c1 is not None and c1.status == "partially_met"):
        items.append(ReviewItem(
            id="unclassified-signals", category="uncertain",
            topic="Requests or cookies that could not be classified",
            needs=["business_context"],
            known=[f"{len(unclassified_after)} request(s) or cookie(s) matched no known vendor signature, so the scan did not classify them (rule R-008)."],
            missing=["Their purpose and owner."],
            decision="Identify them. The scan does not treat them as violations.",
            evidence_ids=[r["id"] for r in unclassified_after], requirement_ids=["DPDP-C1"],
        ))

    # --- refusing consent --------------------------------------------------------------------------------------------------
    c3 = by_id.get("DPDP-C3")
    if c3 is not None and c3.status == "not_met":
        items.append(ReviewItem(
            id="activity-after-reject", category="uncertain",
            topic="Activity observed after Reject was clicked",
            needs=["business_context", "legal_interpretation"],
            known=[f"Reject was clicked and the banner recorded it; {len(c3.evidence_ids)} request(s) or cookie(s) matching known analytics/marketing services were still observed afterwards."],
            missing=["Whether these are leftovers from before the choice or new activity, and whether data was sent."],
            decision="Confirm on a clean browser profile; if it repeats, the consent tool is not honouring refusal.",
            provisions=list(c3.provisions), evidence_ids=list(c3.evidence_ids), requirement_ids=["DPDP-C3"],
        ))

    if ctx.banner_not_displayed:
        items.append(ReviewItem(
            id="banner-not-displayed", category="missing_evidence",
            topic="The consent banner was not displayed to the scanner",
            needs=["more_evidence", "business_context"],
            known=["A consent tool is loaded on the page, but no banner appeared, so Accept and Reject could not be tested."],
            missing=["What visitors in the audience region see, and how Accept and Reject behave."],
            decision="Say which regions the site is meant for, then re-scan from there or on a page where the banner appears.",
            requirement_ids=["DPDP-C2", "DPDP-C3", "DPDP-C4", "DPDP-C5"],
        ))
    elif status("DPDP-C3") == "unable_to_verify" and ctx.mechanism_type in ("banner", "cmp") and not ctx.framed_sites:
        items.append(ReviewItem(
            id="reject-untested", category="missing_evidence",
            topic="Refusing consent could not be tested",
            needs=["more_evidence"],
            known=[f"A consent mechanism was detected, but its Reject control could not be operated or confirmed ({ctx.reject_outcome or 'not recorded'})."],
            missing=["What happens after a visitor refuses."],
            decision="Test Reject by hand, or scan a page where the banner can be operated automatically.",
            requirement_ids=["DPDP-C3"],
        ))

    if ctx.framed_sites:
        items.append(ReviewItem(
            id="framed-wrapper", category="missing_evidence",
            topic="The scanned address only displays another site in a frame",
            needs=["more_evidence"],
            known=["The scanned page is a wrapper, so the evidence describes the wrapper and not the site visitors see (rule R-012)."],
            missing=["Evidence about the site that is actually shown."],
            decision="Enter the address of the site that is shown and scan it directly.",
        ))

    n1 = by_id.get("DPDP-N1")
    if n1 is not None and n1.status == "not_met":
        items.append(ReviewItem(
            id="privacy-notice-not-found", category="uncertain",
            topic="No privacy notice was found",
            needs=["business_context", "more_evidence"],
            known=[f"No privacy policy page was found on the {len(ctx.pages)} main-domain page(s) scanned."],
            missing=["Whether a notice exists elsewhere (another path, a pop-up, an app), and whether its content meets the notice requirements."],
            decision="Confirm where the notice is published and whether it is linked from every page that collects personal data.",
            provisions=list(n1.provisions), requirement_ids=["DPDP-N1"],
        ))

    if len(ctx.pages) >= max_pages:
        items.append(ReviewItem(
            id="page-limit", category="missing_evidence",
            topic=f"Only the first {max_pages} pages of the main domain are assessed",
            needs=["more_evidence"],
            known=[f"{len(ctx.pages)} pages were scanned, which is the limit. Other pages of the main domain may behave differently."],
            missing=["Behaviour on pages that were not scanned, such as sign-up, checkout or account pages."],
            decision="Scan a specific page directly if it matters (for example the one that collects personal data).",
        ))

    # --- what a website can never show -------------------------------------------------------------------------------------
    organisational = [r for r in results if r.status == "unable_to_verify" and r.id in _ORGANISATIONAL]
    if organisational:
        items.append(ReviewItem(
            id="organisational-requirements", category="missing_evidence",
            topic="Requirements that need organisational evidence",
            needs=["business_context", "more_evidence"],
            known=["None of these can be observed through a public website scan: " + "; ".join(r.title for r in organisational) + ".",
                   ("This scan makes no statement about whether retention rules, a DPIA, grievance handling, breach response times, "
                   "or a record of processing activities (ROPA) exist or are complete. A ROPA is not required by name under the "
                   "DPDP Act or Rules; if the organisation keeps one, it is supporting evidence, not a scored check.")],
             missing=["Policies, procedures, logs, records and contact details held by the organisation."],
            decision="Provide the evidence for each, or assess it outside this scan.",
            provisions=sorted({p for r in organisational for p in r.provisions}),
            requirement_ids=[r.id for r in organisational],
        ))

    excluded = [r for r in results if r.status == "not_applicable" and r.id.startswith("DPDP-X")]
    if excluded:
        items.append(ReviewItem(
            id="entity-applicability", category="uncertain",
            topic="Which entity-specific duties apply to the organisation",
            needs=["business_context", "legal_interpretation"],
            known=["These were excluded from the score because a website does not show them: " + "; ".join(r.title for r in excluded) + "."],
            missing=["Whether the organisation processes children's data, is a notified Significant Data Fiduciary, is a Consent Manager, or falls in a Third Schedule class."],
            decision="Declare which apply, so they are included in the assessment.",
            provisions=sorted({p for r in excluded for p in r.provisions}),
            requirement_ids=[r.id for r in excluded],
        ))

    pending = sorted({r.effective for r in results if r.counts_in_score and r.effective > as_of})
    if pending:
        items.append(ReviewItem(
            id="not-yet-in-force", category="uncertain",
            topic="Most assessed duties are not yet in force",
            needs=["legal_interpretation"],
            known=[(f"The consent and notice duties start on {pending[-1].isoformat()} (18 months after the Rules were published). "
                    "This assessment is a readiness view, not a statement that a duty is currently breached.")],
            missing=["Whether any transitional or sector-specific rule applies to this organisation."],
            decision="Use the findings to prepare, and confirm timing and applicability with legal advice.",
        ))

    return HumanReview(statement=STATEMENT, items=items)
