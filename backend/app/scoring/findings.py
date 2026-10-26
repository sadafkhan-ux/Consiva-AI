"""The four finding groups: Confirmed Findings, Needs Human Review, Unable to Verify, Future Readiness.

Derived from the SAME requirement results, evidence context and review items as the DPDP score, so the dashboard and the PDF
(which render this one structure) cannot classify the same result differently. Nothing here re-scores anything.

Classification (each result lands in exactly one group, except that a review item may point at a requirement that is also listed
as a gap, because "what was observed" and "what a person must decide" are different questions):

  Confirmed Findings   a check in a duty that is IN FORCE failed (Not Met, or Partially Met) with sufficient evidence.
  Future Readiness     the same, but the duty is NOT YET in force: shown with its effective date, never as a current violation.
  Needs Human Review   the evidence is ambiguous, or business context or legal interpretation is needed to conclude anything.
  Unable to Verify     the scan did not have the evidence. Counted neither as passing nor as failing.

A network request or a cookie is evidence that something was observed. It is never, by itself, proof of a legal violation.

Severity is shown only where it is justified: a Confirmed Finding on a duty in force, resting on a direct observation (high
confidence). Its level follows the requirement's documented weight (5 high, 4 medium, otherwise low). A partly met requirement is
never rated, because its confidence is at most medium. Everywhere else the finding is "not rated" and says why. It is not derived from the old risk levels.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction

from app.rules.consent_observation import cmp_script_failure
from app.rules.consent_rules import ConsentContext
from app.scanner.scan_profiles import MAX_WEB_PAGES
from app.scoring.review import ReviewItem

GROUPS = ("confirmed", "needs_review", "unable_to_verify", "future_readiness")

STATE_LABEL = {"pre_consent": "Before Consent", "post_accept": "After Accept", "post_reject": "After Reject"}
# Where a check looks, when no individual item pins the consent state down.
_DEFAULT_STATES = {"DPDP-C1": ["Before Consent"], "DPDP-C3": ["After Reject"]}

SIGNAL_CAVEAT = (
    "A network request or a cookie is evidence that something was observed. By itself it is not proof of a legal violation."
)

# What a failed check can mean for the organisation, in operational terms. Never a legal conclusion and never a penalty.
BUSINESS_IMPACT: dict[str, str] = {
    "DPDP-C1": "Visitors' browsing is shared with analytics and advertising providers before they have chosen. The organisation may "
               "not be able to rely on consent for that processing, and the consent set-up may need rework before the duty applies.",
    "DPDP-C2": "Visitors are not asked for a choice at all while tracking runs, so there is no consent record for it.",
    "DPDP-C3": "A visitor's refusal is not respected, which undermines the consent record and the trust the banner is meant to create.",
    "DPDP-C4": "Refusing is harder than accepting, so consent given through the banner may not hold up as freely given.",
    "DPDP-C5": "Visitors cannot choose by purpose, so consent may be treated as bundled rather than specific.",
    "DPDP-N1": "Visitors have no notice to read before consent is requested, and the organisation cannot point to one.",
    "DPDP-N2": "Visitors are not told which cookies and trackers run, so any consent they give is less informed.",
}
_BUSINESS_UNVERIFIED = "Without this evidence the organisation cannot show that it meets this duty; the scan can neither confirm nor refute it."


@dataclass
class FindingEntry:
    id: str
    group: str
    title: str
    requirement_ids: list[str]
    legal_references: list[str]
    outcome: str                           # "Not Met", "Partially Met", "Needs review", "Unable to Verify"
    severity: str | None                   # "High" / "Medium" / "Low", or None when it is not justified
    severity_note: str
    pages: list[str]
    consent_states: list[str]
    observed: list[str]
    evidence_ids: list[str]
    rule_ids: list[str]
    effective_date: str | None
    timing_note: str
    business_impact: str
    explanation: str
    what_to_review: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    how_to_verify: str = ""
    remediation: str = ""
    signal_caveat: str = ""
    confidence: str = "none"
    related_ids: list[str] = field(default_factory=list)


@dataclass
class UpcomingDate:
    date: str
    requirement_ids: list[str]


@dataclass
class FindingGroups:
    confirmed: list[FindingEntry]
    needs_review: list[FindingEntry]
    unable_to_verify: list[FindingEntry]
    future_readiness: list[FindingEntry]
    #: Every applicable duty that is not yet in force, by the date it starts.
    upcoming: list[UpcomingDate]
    #: Per-group notes: what a person should keep in mind reading the group.
    notes: dict[str, str]
    counts: dict[str, int] = field(default_factory=dict)


@dataclass
class RemediationStep:
    rank: int
    requirement_id: str
    title: str
    outcome: str                      # "Not Met" or "Partially Met"
    timing: str                       # "Current" or "Future readiness"
    priority: str                     # "Fix now", "Prepare before 2027-05-13", "Improve"
    priority_reason: str
    action: str
    how_to_verify: str
    legal_references: list[str]
    current_percent: float | None = None
    percent_if_fixed: float | None = None
    gain_percent: float | None = None
    impact: str = ""


def _pct(numerator: float, denominator: float) -> float:
    """numerator / denominator x 100 rounded half-up to one decimal, exactly as the score does (scoring/dpdp_score.py `_pct`). Python's round() rounds half
    to even and works on a binary float, so 5 / 16 came out 31.2 here while the score showed 31.3 for the same fraction."""
    q = Fraction(numerator).limit_denominator(2) / Fraction(denominator) * 100
    return float((Decimal(q.numerator) / Decimal(q.denominator)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def build_remediation_plan(results: list, readiness=None) -> list[RemediationStep]:
    """The remediation actions of every failed check, in the order to do them, by a fixed rule that can be read off the result:
    duties already in force first; within that, Not Met before Partially Met; then the heavier documented weight; then by id.
    The same rule gives the same order for the same evidence."""
    gaps = [r for r in results if r.status in ("not_met", "partially_met") and r.sufficient]
    gaps.sort(key=lambda r: (0 if r.in_force else 1, 0 if r.status == "not_met" else 1, -r.default_weight, r.id))
    plan: list[RemediationStep] = []
    # The effect of fixing each gap ON ITS OWN, from the weights the score itself uses: a Not Met check regains its whole weight, a Partially Met
    # one the half it had not earned. No estimate and no invented metric: the same arithmetic as the score, run once more with one check fixed.
    scored = readiness is not None and getattr(readiness, "status", None) == "scored" and getattr(readiness, "assessed_weight", 0)
    for rank, r in enumerate(gaps, start=1):
        current = _pct(readiness.points_earned, readiness.assessed_weight) if scored else None
        regained = r.weight * (1.0 if r.status == "not_met" else 0.5)
        after = min(100.0, _pct(readiness.points_earned + regained, readiness.assessed_weight)) if scored else None
        if r.in_force:
            priority = "Fix now" if r.status == "not_met" else "Improve"
            timing = "Current"
        else:
            priority = f"Prepare before {r.effective.isoformat()}"
            timing = "Future readiness"
        plan.append(RemediationStep(
            rank=rank, requirement_id=r.id, title=r.title, outcome="Not Met" if r.status == "not_met" else "Partially Met",
            timing=timing, priority=priority,
            priority_reason=(f"{'The duty is in force' if r.in_force else f'The duty applies from {r.effective.isoformat()}'}; "
                             f"{'a gap' if r.status == 'not_met' else 'partly met'}; documented weight {r.default_weight}; "
                             f"{r.confidence} confidence."),
            action=r.remediation, how_to_verify=r.verification, legal_references=list(r.provisions),
            current_percent=current, percent_if_fixed=after, gain_percent=round(after - current, 1) if scored else None,
            impact=BUSINESS_IMPACT.get(r.id, ""),
        ))
    return plan


def severity_for(res) -> tuple[str | None, str]:
    """(severity, why). Only a Confirmed Finding on an in-force duty with a direct observation is rated."""
    if res.status not in ("not_met", "partially_met"):
        return None, "Not rated: this is not a failed check."
    if not res.in_force:
        return None, "Not rated: the duty is not yet in force, so this is a readiness gap, not a violation."
    if res.confidence != "high":
        return None, ("Not rated: the evidence is partial, or rests on something not being found, so a severity is not justified."
                       if res.status == "not_met" else
                       "Not rated: the evidence supports only part of the requirement, so a severity is not justified.")
    level = 2 if res.default_weight >= 5 else 1 if res.default_weight == 4 else 0
    return ("Low", "Medium", "High")[level], (
        f"Rated from the requirement's documented weight ({res.default_weight}) and a direct observation.")


def _how_to_verify_missing(res, ctx: ConsentContext) -> str:
    if res.check_type == "business":
        return f"Provide the evidence: {res.expected_evidence} Record it as reviewed outside this scan."
    if not ctx.pages:
        return "Scan again once the site can be reached."
    if ctx.framed_sites:
        return "Enter the address of the site that is shown inside the frame and scan that directly."
    if ctx.banner_not_displayed and res.id in ("DPDP-C2", "DPDP-C3", "DPDP-C4", "DPDP-C5"):
        if cmp_script_failure(ctx.interaction_evidence):
            return "Fix the consent tool's script (it failed to load: see the evidence), confirm the banner now appears in a browser, then re-scan."
        return "Re-scan from the audience's region or on a page where the banner appears, or check the banner by hand."
    if res.id == "DPDP-C3":
        return "Test Reject by hand, or scan a page where the banner can be operated automatically, then re-scan."
    if not ctx.three_pass:
        return "Re-scan: this scan predates three-state consent testing."
    return "Re-scan, and compare the result."


def _states_for(res, rows_by_id: dict[str, dict]) -> list[str]:
    seen: list[str] = []
    for i in res.evidence_ids:
        for s in rows_by_id.get(i, {}).get("consent_states", []):
            if STATE_LABEL.get(s) and STATE_LABEL[s] not in seen:
                seen.append(STATE_LABEL[s])
    return seen or list(_DEFAULT_STATES.get(res.id, []))


def _signal_based(res, rows_by_id: dict[str, dict]) -> bool:
    return any(i in rows_by_id for i in res.evidence_ids)


def build_finding_groups(results: list, review_items: list[ReviewItem], ctx: ConsentContext, as_of: date) -> FindingGroups:
    rows_by_id = {r["id"]: r for r in (*ctx.trackers, *ctx.cookies)}
    by_id = {r.id: r for r in results}

    def page_lookup(ids: list[str]) -> list[str]:
        out: list[str] = []
        for i in ids:
            row = rows_by_id.get(i, {})
            urls = [row["page_url"]] if row.get("page_url") else [
                o["page_url"] for o in (row.get("observations") or []) if o.get("page_url")]
            for u in urls:   # one at a time: a cookie seen in three consent states names its page three times
                if u not in out:
                    out.append(u)
        return out

    confirmed: list[FindingEntry] = []
    future: list[FindingEntry] = []
    unable: list[FindingEntry] = []
    review: list[FindingEntry] = []

    for res in results:
        if res.status in ("not_met", "partially_met") and res.sufficient:
            sev, sev_note = severity_for(res)
            entry = FindingEntry(
                id=res.id, group="confirmed" if res.in_force else "future_readiness", title=res.title,
                requirement_ids=[res.id], legal_references=list(res.provisions),
                outcome="Not Met" if res.status == "not_met" else "Partially Met",
                severity=sev, severity_note=sev_note, pages=list(res.pages), consent_states=_states_for(res, rows_by_id),
                observed=list(res.observations), evidence_ids=list(res.evidence_ids), rule_ids=list(res.rule_ids),
                effective_date=res.effective.isoformat(),
                timing_note=("Current: the duty is in force." if res.in_force
                             else f"Future readiness: applies from {res.effective.isoformat()}. Not a current violation."),
                business_impact=BUSINESS_IMPACT.get(res.id, ""), explanation=res.sufficiency_reason,
                remediation=res.remediation, how_to_verify=res.verification,
                signal_caveat=SIGNAL_CAVEAT if _signal_based(res, rows_by_id) else "", confidence=res.confidence,
                related_ids=[i.id for i in review_items if res.id in i.requirement_ids and i.category == "uncertain"],
            )
            (confirmed if res.in_force else future).append(entry)
        elif res.status == "unable_to_verify":
            unable.append(FindingEntry(
                id=res.id, group="unable_to_verify", title=res.title, requirement_ids=[res.id],
                legal_references=list(res.provisions), outcome="Unable to Verify", severity=None,
                severity_note="Not rated: it is neither passing nor failing, the evidence is missing.",
                pages=list(res.pages), consent_states=[], observed=list(res.observations), evidence_ids=[], rule_ids=list(res.rule_ids),
                effective_date=res.effective.isoformat(),
                timing_note=("In force." if res.in_force else f"Applies from {res.effective.isoformat()}."),
                business_impact=BUSINESS_IMPACT.get(res.id) or _BUSINESS_UNVERIFIED,
                explanation=res.sufficiency_reason, missing=[res.sufficiency_reason, f"Evidence expected: {res.expected_evidence}", *(f"Needed: {n}" for n in res.needed_evidence)],
                how_to_verify=_how_to_verify_missing(res, ctx), confidence="none",
            ))

    if len(ctx.pages) >= MAX_WEB_PAGES:
        unable.append(FindingEntry(
            id="page-limit", group="unable_to_verify", title=f"Pages beyond the first {MAX_WEB_PAGES} of the main domain were not assessed",
            requirement_ids=[], legal_references=[], outcome="Unable to Verify", severity=None,
            severity_note="Not rated: it is neither passing nor failing, the evidence is missing.", pages=[], consent_states=[],
            observed=[f"{len(ctx.pages)} pages were scanned, which is the limit."], evidence_ids=[], rule_ids=[],
            effective_date=None, timing_note="", business_impact="Pages that were not scanned, such as sign-up or checkout, may behave differently.",
            explanation=f"The scan covers at most {MAX_WEB_PAGES} pages of the main domain.",
            missing=["Behaviour on pages that were not scanned."],
            how_to_verify="Scan the specific page directly, for example the one that collects personal data.",
        ))

    for item in review_items:
        if item.category != "uncertain":
            continue
        reqs = [by_id[r] for r in item.requirement_ids if r in by_id]
        first = reqs[0] if reqs else None
        impact = BUSINESS_IMPACT.get(first.id, "") if first else ""
        review.append(FindingEntry(
            id=item.id, group="needs_review", title=item.topic, requirement_ids=list(item.requirement_ids),
            legal_references=list(item.provisions) or (list(first.provisions) if first else []), outcome="Needs review",
            severity=None, severity_note="Not rated: a person has to decide whether this is a problem at all.",
            pages=page_lookup(item.evidence_ids) or (list(first.pages) if first and first.pages else []),
            consent_states=_review_states(item, rows_by_id), observed=list(item.known), evidence_ids=list(item.evidence_ids),
            rule_ids=sorted({r for q in reqs for r in q.rule_ids}), effective_date=first.effective.isoformat() if first else None,
            timing_note=("" if not first else "In force." if first.in_force else f"Applies from {first.effective.isoformat()}."),
            business_impact=impact,
            explanation="Needs " + " and ".join(n.replace("_", " ") for n in item.needs) + " before any conclusion is drawn.",
            what_to_review=[item.decision], missing=list(item.missing),
            how_to_verify="Record the decision and its reasoning, then re-scan to confirm the change.",
            signal_caveat=SIGNAL_CAVEAT if any(i in rows_by_id for i in item.evidence_ids) else "",
            related_ids=[r for r in item.requirement_ids if by_id.get(r) is not None and by_id[r].status in ("not_met", "partially_met")],
        ))

    upcoming: dict[str, list[str]] = {}
    for res in results:
        if res.status != "not_applicable" and not res.in_force:
            upcoming.setdefault(res.effective.isoformat(), []).append(res.id)

    groups = FindingGroups(
        confirmed=confirmed, needs_review=review, unable_to_verify=unable, future_readiness=future,
        upcoming=[UpcomingDate(d, ids) for d, ids in sorted(upcoming.items())],
        notes={
            "confirmed": ("Only duties that are in force today can be confirmed findings. A severity is shown only when the evidence "
                          "justifies it."),
            "needs_review": "These need business context or legal interpretation. The scan does not conclude that any is a violation.",
            "unable_to_verify": ("The scan did not have the evidence. These are neither passes nor failures, and they lower the "
                                 "assessment coverage. This scan makes no statement about whether retention rules, a DPIA, grievance "
                                 "handling, breach response times or a ROPA exist or are complete."),
            "future_readiness": ("These duties are not yet in force. The gaps are for preparation and are not current violations."),
        },
    )
    groups.counts = {g: len(getattr(groups, g)) for g in GROUPS}
    return groups


def _review_states(item: ReviewItem, rows_by_id: dict[str, dict]) -> list[str]:
    seen: list[str] = []
    for i in item.evidence_ids:
        for s in rows_by_id.get(i, {}).get("consent_states", []):
            if STATE_LABEL.get(s) and STATE_LABEL[s] not in seen:
                seen.append(STATE_LABEL[s])
    return seen
