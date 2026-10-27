"""DPDP Compliance Score -- a PROPOSED methodology, not an officially prescribed legal formula.

    score % = (weighted points earned / weighted points assessed) x 100
    coverage % = (weight of assessed requirements / weight of applicable requirements) x 100

Points per requirement: Met = 100% of its weight, Partially Met = 50%, Not Met = 0%.
Unable to Verify, Not Applicable and duplicate-suppressed requirements are NOT in the score denominator. Unable to Verify
ones are reported separately and count against coverage, so missing or untested evidence can never raise a score.
When coverage or the number of assessed requirements is too low the score is withheld ("Insufficient Evidence").

Everything is a pure function of one scan's evidence dict (the shape scan_repository produces, the same input the consent
rules use), the assessment date and an optional applicability declaration. Nothing here reads a database or the clock
except `assess()`'s default `as_of`, so every number can be reproduced and unit-tested.

Version history: see METHODOLOGY_VERSION and docs/DPDP_COMPLIANCE_SCORE.md.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction
from typing import Literal

from app.rules.consent_observation import script_failure_note
from app.rules.consent_rules import ConsentContext, build_consent_context
from app.scanner.scan_profiles import MAX_WEB_PAGES
from app.scoring import deductions as deductions_module
from app.scoring import findings, review
from app.scoring import sector as sector_analysis

METHODOLOGY_VERSION = "1.6.1"

# The version of the legal mapping: which provisions each check cites, their effective dates and their applicability. Changes
# whenever a citation, an effective date or an applicability condition changes, independently of the scoring methodology.
LEGAL_MAPPING_VERSION = "2026.10.1"
LEGAL_SOURCES: tuple[tuple[str, str], ...] = (
    ("DPDP Rules, 2025", "G.S.R. 846(E) of 13 Nov 2025, as corrected by G.S.R. 892(E) of 10 Dec 2025."),
    ("Commencement", ("G.S.R. 843(E) of 13 Nov 2025: Act ss.1(2), 2, 18-26, 35, 38-43 and 44(1), (3) in force on publication; "
                      "s.6(9) and s.27(1)(d) after one year (13 Nov 2026); ss.3-5, 6(1)-(8) and (10), 7-17, 27 (other than (1)(d)), "
                      "28-34, 36, 37 and 44(2) after eighteen months (13 May 2027). Rules 1, 2 and 17-21 on publication, Rule 4 after "
                      "one year, Rules 3, 5-16, 22 and 23 after eighteen months.")),
    ("DPDP Act, 2023", "Act No. 22 of 2023. The Act's sub-section numbers cited here should be checked against the statute."),
)

NATURE_NOTE = (
    "This is an evidence-based assessment of website readiness for the DPDP Act and Rules, from the entered main domain only. It is "
    "not a guarantee, a certification or a statement of full legal compliance: organisational evidence and legal interpretation are "
    "needed for that."
)

DISCLAIMER = (
    "This report is for informational purposes only and does not constitute legal advice. It reflects an automated scan of the pages "
    "listed, measured against Consiva's own checks, and is not a certification or guarantee of compliance with any law. Consult a "
    "qualified legal professional before relying on it."
)

# ---- thresholds below which a score is withheld ---------------------------------------------------------------------
MIN_COVERAGE_PERCENT = 30.0
MIN_ASSESSED_REQUIREMENTS = 2
# A score that is shown but rests on little is flagged: below this coverage, or on fewer assessed requirements, it carries a caution.
CAUTION_COVERAGE_PERCENT = 50.0
CAUTION_MIN_REQUIREMENTS = 4

# ---- effective dates -------------------------------------------------------------------------------------------------
# G.S.R. 843(E) / the DPDP Rules, 2025 were published on 13 Nov 2025. Provisions that start "one year" / "eighteen months"
# after publication are taken to start on the same calendar day 12 / 18 months later.
PUBLICATION_DATE = date(2025, 11, 13)
EFFECTIVE_NOW = PUBLICATION_DATE
EFFECTIVE_12_MONTHS = date(2026, 11, 13)
EFFECTIVE_18_MONTHS = date(2027, 5, 13)

Status = Literal["met", "partially_met", "not_met", "unable_to_verify", "not_applicable"]
SCORED_FACTOR: dict[str, Fraction] = {"met": Fraction(1), "partially_met": Fraction(1, 2), "not_met": Fraction(0)}
ASSESSED_STATUSES = tuple(SCORED_FACTOR)
STATUSES: tuple[Status, ...] = ("met", "partially_met", "not_met", "unable_to_verify", "not_applicable")
NOT_APPLICABLE: Status = "not_applicable"

DeclaredFlag = Literal["children", "significant", "consent_manager", "erasure_class"]


@dataclass(frozen=True)
class Applicability:
    """What the organisation declares about itself. Entity-specific requirements are excluded unless declared True: they are
    never guessed from a website. None means "not declared"."""
    processes_children_data: bool | None = None
    significant_data_fiduciary: bool | None = None
    consent_manager: bool | None = None
    erasure_class: bool | None = None  # e-commerce / online gaming / social media above the Third Schedule thresholds

    def flag(self, key: str) -> bool | None:
        return {
            "children": self.processes_children_data, "significant": self.significant_data_fiduciary,
            "consent_manager": self.consent_manager, "erasure_class": self.erasure_class,
        }[key]


@dataclass(frozen=True)
class Requirement:
    id: str
    category: str
    title: str
    provisions: tuple[str, ...]
    weight: int
    effective: date
    weight_rationale: str
    # None = applies to every data fiduciary; otherwise the Applicability flag that must be declared True.
    applies_if: DeclaredFlag | None = None
    # Whether a public website scan can observe it at all.
    scan_assessable: bool = True
    # How the check is answered: "website_scan" (what the scan observes), "policy_text" (what the privacy policy says, read by the policy analysis, no extra
    # model call) or "organisation_records" (only the organisation's own records can settle it).
    method: str = "website_scan"
    # Requirements in one group describe the same underlying gap; see _suppress_duplicates.
    dedup_group: str | None = None
    rule_ids: tuple[str, ...] = ()
    check: Callable[[ConsentContext], Outcome] | None = field(default=None, compare=False, repr=False)


@dataclass
class Outcome:
    status: Status
    observations: list[str] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    # The pages the evidence was read from, when it is not an observed request or cookie (the policy page a quotation comes from).
    pages: list[str] = field(default_factory=list)


# =======================================================================================================================
# Checks. Each reads ONLY the ConsentContext (shared with the consent rules) and returns a status plus the observations and
# evidence ids that justify it. A check never returns Met on missing evidence: it returns Unable to Verify.
# =======================================================================================================================

def _unverifiable(ctx: ConsentContext) -> str | None:
    """Reasons that make ANY observation about the scanned site unreliable."""
    if not ctx.pages:
        return "No pages were scanned, so nothing was observed."
    return None


def _framed_limit(ctx: ConsentContext) -> str | None:
    """The limit a framed wrapper puts on what a CLEAN or ABSENT result may claim.

    The scanned address only displays another site in a frame (rule R-012). What the scan OBSERVED is still observed: a tracker that fired while the
    address loaded fired, and a consent banner or policy link found on the pages scanned is there. What a wrapper cannot support is a conclusion
    from NOTHING being seen (no banner, no policy, no tracker, a clean Reject): the framed site may have its own, and the scan did not read it. So a
    requirement is limited only where its answer rests on something not being seen; one that rests on something seen keeps its answer."""
    if not ctx.framed_sites:
        return None
    return (f"The scanned address only displays another site ({ctx.framed_sites[0]}) in a frame, so the evidence describes "
            "the wrapper page, not the site visitors see (rule R-012).")


def _seen(ctx: ConsentContext) -> str:
    """What to add to a positive observation made on a wrapper."""
    return " This was seen on the scanned wrapper page; the framed site's own pages were not scanned." if ctx.framed_sites else ""


def _pre_consent_processing(ctx: ConsentContext) -> Outcome:
    if (why := _unverifiable(ctx)):
        return Outcome("unable_to_verify", [why])
    if not ctx.three_pass:
        return Outcome("unable_to_verify", ["This scan predates three-state consent testing, so what fired before consent is unknown."])
    classified = [e for e in ctx.non_essential if "pre_consent" in e.get("consent_states", [])]
    if classified:
        note = f"{len(classified)} analytics/marketing cookie(s)/script(s) fired before any consent interaction."
        return Outcome("not_met", [note + _seen(ctx)], [e["id"] for e in classified])
    if (limit := _framed_limit(ctx)):
        return Outcome("unable_to_verify", [limit])
    unknown = [e for e in (*ctx.trackers, *ctx.cookies) if e.get("category") is None and "pre_consent" in e.get("consent_states", [])]
    if unknown:
        note = (
            f"No classified analytics/marketing fired before consent, but {len(unknown)} unclassified item(s) did; their purpose is unknown "
            "and a reviewer must decide whether they are non-essential (rule R-008)."
        )
        return Outcome("partially_met", [note], [e["id"] for e in unknown])
    return Outcome("met", ["Before any consent interaction, no analytics or marketing cookie or script was observed."])


def _no_tracking_observed(ctx: ConsentContext, not_applicable_note: str) -> Outcome:
    """What an absence of analytics or marketing tracking means for the two checks that exist only where tracking is used.

    Tracking is gated behind consent on a site that has a consent tool, so it shows up only after Accept. When the Accept choice could not be tested (the banner's control was not
    found, or the click failed), nothing after consent was ever seen: "no tracking observed" is then an untested claim, not a finding, and the check is Unable to Verify. Treating it
    as Not Applicable made two scans of one website differ in which checks applied, and so in coverage, only because one Accept click happened to fail (uat.consiva.ai, 27 Oct 2026).
    Without a consent tool, or with Accept tested, an absence is an observation and the check does not apply."""
    if ctx.three_pass and ctx.mechanism_type in ("banner", "cmp") and ctx.accept_outcome != "clicked":
        return Outcome("unable_to_verify", [
            "No analytics or marketing tracking was observed, but the Accept choice could not be tested, so it is not known whether tracking starts once a visitor consents."
        ])
    return Outcome("not_applicable", [not_applicable_note])


def _consent_mechanism_present(ctx: ConsentContext) -> Outcome:
    if not ctx.non_essential:
        return _no_tracking_observed(ctx, "No analytics or marketing tracking was observed, so no consent mechanism was needed for it.")
    if (why := _unverifiable(ctx)):
        return Outcome("unable_to_verify", [why])
    ids = [e["id"] for e in ctx.non_essential]
    if ctx.mechanism_type == "none" and (limit := _framed_limit(ctx)):
        return Outcome("unable_to_verify", [limit])        # no banner seen on a wrapper is not proof the framed site has none
    if ctx.mechanism_type == "none":
        return Outcome("not_met", [f"{len(ids)} analytics/marketing item(s) were observed but no consent banner or CMP was found."], ids)
    if ctx.mechanism_type in ("banner", "cmp"):
        if ctx.banner_not_displayed:
            note = (
                "A consent tool is loaded but its banner was not displayed to the scanner (often region-specific), so it is not known "
                "that visitors are asked."
            ) + script_failure_note(ctx.interaction_evidence)
            return Outcome("unable_to_verify", [note])
        return Outcome("met", [f"A consent mechanism ({ctx.mechanism_type}) was detected on the scanned pages." + _seen(ctx)])
    return Outcome("unable_to_verify", ["The consent mechanism type was not determined."])


def _reject_honoured(ctx: ConsentContext) -> Outcome:
    if (why := _unverifiable(ctx)):
        return Outcome("unable_to_verify", [why])
    if ctx.mechanism_type == "none":
        if (limit := _framed_limit(ctx)):
            return Outcome("unable_to_verify", [limit])
        return Outcome("not_applicable", ["No consent mechanism exists to reject through; see DPDP-C2."])
    if not ctx.three_pass:
        return Outcome("unable_to_verify", ["This scan predates three-state consent testing."])
    if ctx.banner_not_displayed:
        return Outcome("unable_to_verify", ["The consent banner was not displayed to the scanner, so Reject could not be tested (rule R-009)." + script_failure_note(ctx.interaction_evidence)])
    if ctx.reject_outcome != "clicked":
        return Outcome("unable_to_verify", [f"Reject could not be exercised (outcome: {ctx.reject_outcome or 'not recorded'})."])
    if not ctx.reject_confirmed:
        return Outcome("unable_to_verify", ["Reject was clicked but the banner did not observably record the choice (rule R-011)."])
    after = [e for e in ctx.non_essential if "post_reject" in e.get("consent_states", [])]
    if after:
        return Outcome("not_met", [f"{len(after)} analytics/marketing item(s) kept firing after Reject was clicked and confirmed."],
                       [e["id"] for e in after])
    if (limit := _framed_limit(ctx)):
        return Outcome("unable_to_verify", [limit])
    return Outcome("met", ["Reject was clicked and confirmed, and no analytics or marketing was observed afterwards."])


def _reject_as_easy_as_accept(ctx: ConsentContext) -> Outcome:
    if ctx.mechanism_type not in ("banner", "cmp"):
        if (limit := _framed_limit(ctx)):
            return Outcome("unable_to_verify", [limit])
        return Outcome("not_applicable", ["No consent banner was found, so there is no choice to compare; see DPDP-C2."])
    if (why := _unverifiable(ctx)):
        return Outcome("unable_to_verify", [why])
    if ctx.banner_not_displayed:
        return Outcome("unable_to_verify", ["The consent banner was not displayed to the scanner, so its options were not seen." + script_failure_note(ctx.interaction_evidence)])
    has_reject = (ctx.signals or {}).get("has_reject_all")
    if has_reject is True:
        return Outcome("met", ["A reject-all option was found alongside accept." + _seen(ctx)])
    if has_reject is False:
        return Outcome("not_met", ["A consent banner was shown but no equally prominent reject-all option was found."])
    return Outcome("unable_to_verify", ["Whether a reject-all option exists was not recorded."])


def _granular_choices(ctx: ConsentContext) -> Outcome:
    if ctx.mechanism_type not in ("banner", "cmp"):
        if (limit := _framed_limit(ctx)):
            return Outcome("unable_to_verify", [limit])
        return Outcome("not_applicable", ["No consent banner was found, so there are no choices to assess; see DPDP-C2."])
    if (why := _unverifiable(ctx)):
        return Outcome("unable_to_verify", [why])
    if ctx.banner_not_displayed:
        return Outcome("unable_to_verify", ["The consent banner was not displayed to the scanner, so its options were not seen." + script_failure_note(ctx.interaction_evidence)])
    sig = ctx.signals or {}
    granular, reject = sig.get("has_granular_choices"), sig.get("has_reject_all")
    if granular is True:
        return Outcome("met", ["The banner offers choices per purpose or category." + _seen(ctx)])
    if granular is False:
        if reject is True:
            return Outcome("partially_met", ["The banner offers a single accept/reject choice but no choice per purpose."])
        return Outcome("not_met", ["The banner offers neither per-purpose choices nor a reject option."])
    return Outcome("unable_to_verify", ["Whether per-purpose choices exist was not recorded."])


def _privacy_notice(ctx: ConsentContext) -> Outcome:
    if (why := _unverifiable(ctx)):
        return Outcome("unable_to_verify", [why])
    if "privacy_policy" in ctx.policy_types:
        return Outcome("met", ["A privacy policy page was found on the scanned pages." + _seen(ctx)])
    if (limit := _framed_limit(ctx)):
        return Outcome("unable_to_verify", [limit])      # a wrapper without a policy link says nothing about the framed site's notice
    return Outcome("not_met", ["No privacy policy page was found within the pages scanned."])


def _tracking_disclosure(ctx: ConsentContext) -> Outcome:
    if not ctx.non_essential:
        return _no_tracking_observed(ctx, "No analytics or marketing tracking was observed, so no tracking disclosure was needed.")
    if (why := _unverifiable(ctx)):
        return Outcome("unable_to_verify", [why])
    ids = [e["id"] for e in ctx.non_essential]
    if "cookie_policy" in ctx.policy_types:
        return Outcome("met", ["A dedicated cookie policy page was found." + _seen(ctx)], ids)
    if "privacy_policy" in ctx.policy_types:
        return Outcome("partially_met", ["Tracking was observed and a general privacy policy exists, but no dedicated cookie policy was found."], ids)
    if (limit := _framed_limit(ctx)):
        return Outcome("unable_to_verify", [limit])
    return Outcome("not_met", ["Tracking was observed and neither a cookie policy nor a privacy policy was found."], ids)


def _not_observable(what: str) -> Callable[[ConsentContext], Outcome]:
    def check(_ctx: ConsentContext) -> Outcome:
        return Outcome("unable_to_verify", [f"{what} cannot be observed through a public website scan; it needs organisational evidence."])
    return check


# ---- checks answered by what the privacy policy says --------------------------------------------------------------------------------------------------
#
# The policy analysis (app/policy/analysis.py) already reads the policy and cookie pages once per analysis. It returns, for each thing a policy should say, a
# QUOTATION that was checked to appear word for word in the page, and a status decided by fixed rules (a contact detail found beside the passage, a response
# period parsed as a number). These checks read that result: no further model call is made and no token is spent.
#
# What counts, and what never does:
#   * A verified quotation with the needed detail is evidence the thing is published: Met.
#   * A quotation that names the thing but lacks the concrete detail the Rules ask for (a role with no way to reach it) is Partially Met.
#   * A stated period above the 90 days the Rules allow is Partially Met: the policy itself says so.
#   * Anything NOT found in the policy text stays Unable to Verify. The site may publish it elsewhere, so absence from one document is never a failure.
#   * A policy's description of its security or breach practice is never treated as proof that the practice exists.


def _policy_rows(ctx: ConsentContext) -> tuple[dict[str, dict], str | None]:
    """The policy analysis rows by id, or (empty, why there are none)."""
    analysis = ctx.policy_analysis
    if not analysis:
        return {}, None
    if analysis.get("status") != "analysed":
        return {}, analysis.get("reason") or "The privacy policy text could not be read."
    return {r["id"]: r for r in analysis.get("requirements", [])}, None


def _quoted(row: dict) -> str:
    quote = (row.get("quote") or "").strip()
    contacts = row.get("contacts") or {}
    detail = "; ".join(f"{label} {', '.join(values)}" for label, values in (("email", contacts.get("emails") or []), ("phone", contacts.get("phones") or []),
                                                                            ("link", contacts.get("links") or [])) if values)
    return f'The privacy policy says: "{quote}"' + (f" ({detail})" if detail else "") + (f" [{row['source_url']}]" if row.get("source_url") else "")


def _unsettled(what: str, ctx: ConsentContext, why_not_read: str | None, absent: str) -> Outcome:
    """Unable to Verify, with the honest reason: no analysis, one that could not read the policy, or one that read it and did not find the statement."""
    if not ctx.policy_analysis:
        return Outcome("unable_to_verify", [f"{what} cannot be observed through a public website scan; it needs organisational evidence."])
    if why_not_read:
        return Outcome("unable_to_verify", [f"{what}: the privacy policy could not be read ({why_not_read.rstrip('.')}), so this is not established."])
    return Outcome("unable_to_verify", [absent])


def _policy_single(row_id: str, what: str, absent: str) -> Callable[[ConsentContext], Outcome]:
    """One statement the policy should make: verified -> Met, named without the concrete detail -> Partially Met, not found -> Unable to Verify."""
    def check(ctx: ConsentContext) -> Outcome:
        rows, why = _policy_rows(ctx)
        row = rows.get(row_id)
        if row is None or row.get("status") in (None, "not_found", "not_applicable"):
            return _unsettled(what, ctx, why, absent)
        pages = [row["source_url"]] if row.get("source_url") else []
        if row["status"] == "verified":
            return Outcome("met", [_quoted(row)], pages=pages)
        return Outcome("partially_met", [_quoted(row), f"{row.get('detail', '')} The Rules ask for a concrete way to reach it.".strip()], pages=pages)
    return check


# A response period only counts as the grievance period when the quoted sentence is about grievances, complaints, requests or concerns. The policy analysis
# parses any "within N days" in the passage it was given; without this a sentence about something else (deleting data within 30 days) could earn a Met.
_GRIEVANCE_TOPIC = re.compile(r"grievanc|complain|redress|appeal|concern|quer(?:y|ies)|request", re.IGNORECASE)


def _policy_grievance(ctx: ConsentContext) -> Outcome:
    """A grievance system published with a response period of at most 90 days. Met needs both a reachable contact and a period within the limit. A period the
    policy states above 90 days is Partially Met; a period that is simply not stated cannot be checked and stays Unable to Verify."""
    what = "Whether a grievance redressal system with a response period is published"
    rows, why = _policy_rows(ctx)
    contact, period = rows.get("grievance_contact"), rows.get("grievance_timeline")
    if contact is None or contact.get("status") in (None, "not_found", "not_applicable"):
        return _unsettled(what, ctx, why, "No grievance contact was found in the privacy policy text that was read. It may be published elsewhere on the site.")
    pages = list(dict.fromkeys(r["source_url"] for r in (contact, period) if r and r.get("source_url")))
    notes = [_quoted(contact)]
    if contact["status"] != "verified":
        return Outcome("partially_met", [*notes, f"{contact.get('detail', '')} The Rules ask for a concrete way to reach the grievance system.".strip()], pages=pages)
    if period is None or period.get("status") in (None, "not_found", "not_applicable"):
        return Outcome("unable_to_verify", [*notes, "No response period was found in the privacy policy text that was read, so the 90-day limit cannot be checked."], pages=pages)
    notes.append(_quoted(period))
    if not _GRIEVANCE_TOPIC.search(period.get("quote") or ""):
        return Outcome("unable_to_verify", [*notes, "The response period found is not about grievances, complaints or requests, so it is not taken as the grievance response period."], pages=pages)
    days = period.get("response_days")
    if period["status"] == "verified":
        return Outcome("met", notes, pages=pages)
    if days is not None and days > 90:
        return Outcome("partially_met", [*notes, f"The policy states a response period of about {days:g} days; the Rules allow at most 90."], pages=pages)
    return Outcome("unable_to_verify", [*notes, "The passage mentions grievances but states no response period, so the 90-day limit cannot be checked."], pages=pages)


def _policy_practice(row_id: str, what: str, absent: str, caveat: str) -> Callable[[ConsentContext], Outcome]:
    """A duty about what the organisation DOES. The policy can only describe it; a description is shown beside the result but never settles it."""
    def check(ctx: ConsentContext) -> Outcome:
        rows, why = _policy_rows(ctx)
        row = rows.get(row_id)
        if row is None or row.get("status") in (None, "not_found", "not_applicable"):
            return _unsettled(what, ctx, why, absent)
        return Outcome("unable_to_verify", [_quoted(row), caveat], pages=[row["source_url"]] if row.get("source_url") else [])
    return check


# =======================================================================================================================
# The registry. Weights: 5 = core consent duty whose breach a scan observes directly; 4 = duty a scan observes directly that
# supports it; 3 = statutory duty a scan cannot observe; 2 = supporting or entity-specific duty.
# =======================================================================================================================

ACT = "DPDP Act, 2023"
RULES = "DPDP Rules, 2025"

REQUIREMENTS: tuple[Requirement, ...] = (
    Requirement(
        "DPDP-C1", "Consent", "No analytics or marketing processing before consent",
        (f"{ACT} s.6(1)", f"{RULES} r.3"), 5, EFFECTIVE_18_MONTHS,
        "Core consent obligation; the scan observes it directly in the pre-consent state.",
        dedup_group="consent-before-tracking", rule_ids=("R-001", "R-008"), check=_pre_consent_processing,
    ),
    Requirement(
        "DPDP-C2", "Consent", "A consent mechanism is provided where tracking is used",
        (f"{ACT} s.5(1)", f"{ACT} s.6(1)"), 4, EFFECTIVE_18_MONTHS,
        "Prerequisite for valid consent; observed directly.",
        dedup_group="consent-before-tracking", rule_ids=("R-002", "R-009"), check=_consent_mechanism_present,
    ),
    Requirement(
        "DPDP-C3", "Consent", "Refusing consent is honoured",
        (f"{ACT} s.6(1)", f"{ACT} s.6(4)", f"{ACT} s.6(6)", f"{RULES} r.3(c)(i)"), 4, EFFECTIVE_18_MONTHS,
        "Consent that cannot be refused is not free; observed only when Reject was clicked and confirmed.",
        rule_ids=("R-003", "R-009", "R-011"), check=_reject_honoured,
    ),
    Requirement(
        "DPDP-C4", "Consent", "Refusing is as easy as accepting",
        (f"{ACT} s.6(4)", f"{RULES} r.3(c)(i)"), 4, EFFECTIVE_18_MONTHS,
        "The Act requires withdrawal to be comparable in ease to giving consent; a reject-all option is the observable proxy.",
        dedup_group="choice-quality", rule_ids=("R-005",), check=_reject_as_easy_as_accept,
    ),
    Requirement(
        "DPDP-C5", "Consent", "Choices are specific to each purpose",
        (f"{ACT} s.6(1)", f"{RULES} r.3(b)(ii)"), 2, EFFECTIVE_18_MONTHS,
        "Consent must be specific; per-purpose choices are a supporting signal and a banner design, not a standalone breach.",
        dedup_group="choice-quality", check=_granular_choices,
    ),
    Requirement(
        "DPDP-N1", "Notice", "A privacy notice is published",
        (f"{ACT} s.5(1)", f"{RULES} r.3(a)", f"{RULES} r.3(b)"), 4, EFFECTIVE_18_MONTHS,
        "Notice must precede consent; the scan observes whether a policy page exists, not whether its content is adequate.",
        dedup_group="notice", rule_ids=("R-006",), check=_privacy_notice,
    ),
    Requirement(
        "DPDP-N2", "Notice", "Cookies and trackers are disclosed",
        (f"{ACT} s.5(1)", f"{RULES} r.3(b)(i)"), 2, EFFECTIVE_18_MONTHS,
        "Notice must itemise the personal data processed; a dedicated cookie policy is the observable proxy.",
        dedup_group="notice", rule_ids=("R-007",), check=_tracking_disclosure,
    ),
    Requirement(
        "DPDP-R9", "Data principal rights", "Contact of the Data Protection Officer or a responsible person is published",
        (f"{ACT} s.8(9)", f"{RULES} r.9"), 2, EFFECTIVE_18_MONTHS,
        "Statutory duty; answered by what the privacy policy publishes.", scan_assessable=False, method="policy_text",
        check=_policy_single("dpo_contact", "Whether a DPO or contact person is published",
                             "No contact of a Data Protection Officer or answering person was found in the privacy policy text that was read. It may be published elsewhere on the site."),
    ),
    Requirement(
        "DPDP-R14A", "Data principal rights", "The means to exercise data principal rights are published",
        (f"{ACT} s.11", f"{ACT} s.12", f"{ACT} s.13", f"{RULES} r.14(1)"), 3, EFFECTIVE_18_MONTHS,
        "Statutory duty; answered by what the privacy policy publishes.", scan_assessable=False, method="policy_text",
        check=_policy_single("rights_means", "Whether the means to exercise rights are published",
                             "No means of making a rights request was found in the privacy policy text that was read. It may be published elsewhere on the site."),
    ),
    Requirement(
        "DPDP-R14B", "Data principal rights", "A grievance redressal system with a response period of at most 90 days is published",
        (f"{ACT} s.8(10)", f"{ACT} s.13", f"{RULES} r.14(3)"), 3, EFFECTIVE_18_MONTHS,
        "Statutory duty; answered by what the privacy policy publishes.", scan_assessable=False, method="policy_text",
        check=_policy_grievance,
    ),
    Requirement(
        "DPDP-S6", "Security and breach", "Reasonable security safeguards are in place",
        (f"{ACT} s.8(5)", f"{RULES} r.6"), 3, EFFECTIVE_18_MONTHS,
        "Statutory duty; the scan cannot see an organisation's internal controls.", scan_assessable=False, method="organisation_records",
        check=_policy_practice("security", "Security safeguards", "The privacy policy text that was read does not describe security safeguards.",
                               "A policy's description of its safeguards is not evidence that they are in place; that needs the organisation's own records."),
    ),
    Requirement(
        "DPDP-S7", "Security and breach", "Personal data breaches are intimated to the Board and affected individuals",
        (f"{ACT} s.8(6)", f"{RULES} r.7"), 3, EFFECTIVE_18_MONTHS,
        "Statutory duty; the scan cannot see an organisation's incident process.", scan_assessable=False, method="organisation_records",
        check=_policy_practice("breach_response", "Breach intimation processes", "The privacy policy text that was read does not describe a breach response.",
                               "A policy's description of its breach response is not evidence that it works or that breaches have been reported; that needs the organisation's own records."),
    ),
    Requirement(
        "DPDP-X4", "Entity-specific", "Consent Manager registration and obligations",
        (f"{ACT} s.6(9)", f"{RULES} r.4"), 2, EFFECTIVE_12_MONTHS,
        "Applies only to a registered Consent Manager.", applies_if="consent_manager", scan_assessable=False, method="organisation_records",
        check=_not_observable("Consent Manager obligations"),
    ),
    Requirement(
        "DPDP-X8", "Entity-specific", "Erasure after the Third Schedule period, with 48 hours' notice",
        (f"{ACT} s.8(7)", f"{RULES} r.8"), 2, EFFECTIVE_18_MONTHS,
        "Applies only to the classes in the Third Schedule (large e-commerce, online gaming, social media).",
        applies_if="erasure_class", scan_assessable=False, method="organisation_records", check=_not_observable("Erasure processes"),
    ),
    Requirement(
        "DPDP-X10", "Entity-specific", "Verifiable parental or guardian consent for children's data",
        (f"{ACT} s.9", f"{RULES} r.10", f"{RULES} r.11"), 3, EFFECTIVE_18_MONTHS,
        "Applies only where the organisation processes children's personal data.",
        applies_if="children", scan_assessable=False, method="organisation_records", check=_not_observable("Verifiable parental consent"),
    ),
    Requirement(
        "DPDP-X13", "Entity-specific", "Significant Data Fiduciary duties (annual DPIA and audit)",
        (f"{ACT} s.10", f"{RULES} r.13"), 3, EFFECTIVE_18_MONTHS,
        "Applies only to a notified Significant Data Fiduciary.", applies_if="significant", scan_assessable=False, method="organisation_records",
        check=_not_observable("DPIA and audit"),
    ),
)

@dataclass(frozen=True)
class CheckDetail:
    """The written definition of one check, shown with its result on the dashboard and in the PDF."""
    description: str
    criteria: str
    expected_evidence: str
    why_it_matters: str = ""
    remediation: str = ""
    verification: str = ""
    # What an organisation would need to supply to settle the check when a scan cannot (shown only while the check is unverified).
    needed: tuple[str, ...] = ()


DETAILS: dict[str, CheckDetail] = {
    "DPDP-C1": CheckDetail(
        "Analytics and marketing cookies and scripts do not run before the visitor has given consent.",
        "Met: no request or cookie matching a known analytics or marketing service was observed before any consent interaction. "
        "Partially Met: only unclassified third-party items fired before consent. Not Met: at least one known analytics or "
        "marketing request or cookie was observed before consent. Unable to Verify: no pages scanned, a framed wrapper, or no "
        "three-state consent record.",
        "Before-consent observations (requests and cookies, with the page) from a scan pass made before any banner interaction.",
        "Consent must be free, specific, informed, unconditional and unambiguous, given by a clear affirmative action (Act s.6(1)). "
        "Processing that starts before that choice has no consent behind it unless another ground in the Act applies.",
        "Load analytics and marketing tags only after the visitor accepts: configure the consent tool to block them by default, or "
        "start them from its accept callback. Identify each unclassified item and treat it as non-essential unless it is needed "
        "for the service the visitor asked for.",
        "Re-scan. The Before Consent state should show no analytics or marketing requests or cookies, and they should appear only "
        "after Accept.",
    ),
    "DPDP-C2": CheckDetail(
        "A consent mechanism is provided wherever analytics or marketing tracking is used.",
        "Met: a banner or consent tool was detected and displayed. Not Met: tracking was observed but no consent mechanism was "
        "found. Not Applicable: no analytics or marketing tracking was observed. Unable to Verify: a consent tool is loaded but "
        "its banner was not displayed to the scanner, no pages were scanned, or the page is a framed wrapper.",
        "Consent-mechanism detection (banner or consent tool) on the scanned pages, and whether its banner was displayed.",
        "Notice and a request for consent have to reach the visitor before personal data is processed on the basis of consent "
        "(Act s.5(1), s.6(1)).",
        "Add a consent banner, or a consent tool, to every page that loads analytics or marketing tags, and show it to the "
        "audience's region.",
        "Re-scan: a consent mechanism is detected and its banner is displayed.",
    ),
    "DPDP-C3": CheckDetail(
        "When the visitor refuses consent, the refusal is honoured.",
        "Met: Reject was clicked, the banner recorded it, and no known analytics or marketing activity was observed afterwards. "
        "Not Met: such activity was observed after a confirmed Reject. Unable to Verify: Reject could not be operated or "
        "confirmed, or the banner was not displayed. Not Applicable: no consent mechanism exists.",
        "The After Reject state, observed after a Reject click that the banner confirmed.",
        "Consent can be withdrawn, and processing must stop when it is (Act s.6(4), s.6(6)).",
        "Make the consent tool stop or never start analytics and marketing tags once Reject is chosen, and clear their cookies.",
        "Re-scan: after a confirmed Reject click the After Reject state shows no analytics or marketing requests or cookies.",
        needed=("A re-scan from a region or page where the consent banner is shown, or a test of the Reject control by hand, followed by a look at the requests and cookies that appear after refusing.",),
    ),
    "DPDP-C4": CheckDetail(
        "Refusing consent is as easy as giving it.",
        "Met: a reject-all option was found alongside accept. Not Met: a banner was shown but no equally prominent reject-all "
        "option was found. Unable to Verify: the banner was not displayed or the option was not recorded. Not Applicable: no "
        "banner was found.",
        "The consent banner's options, as detected on the scanned pages.",
        "The ease of withdrawing or refusing consent must be comparable to the ease of giving it (Act s.6(4); Rules r.3(c)(i)).",
        "Add a reject-all control of the same prominence as accept, on the first layer of the banner.",
        "Re-scan: the banner shows a reject-all option on its first layer.",
    ),
    "DPDP-C5": CheckDetail(
        "Choices are specific to each purpose.",
        "Met: the banner offers choices per purpose or category. Partially Met: only a single accept or reject choice. Not Met: "
        "neither per-purpose choices nor a reject option. Unable to Verify: the banner was not displayed or the options were not "
        "recorded. Not Applicable: no banner was found.",
        "The consent banner's options, as detected on the scanned pages.",
        "Consent must be specific, and the notice must state the purposes (Act s.6(1); Rules r.3(b)(ii)).",
        "Offer separate choices for each purpose, such as analytics and marketing, instead of one combined choice.",
        "Re-scan: the banner lists the purposes with a choice for each.",
    ),
    "DPDP-N1": CheckDetail(
        "A privacy notice is published.",
        "Met: a privacy policy page was found on the scanned pages. Not Met: none was found. Unable to Verify: no pages were "
        "scanned or the page is a framed wrapper. This checks that a notice exists, not that its content is adequate.",
        "Policy pages found through the scanned main-domain pages.",
        "Notice must accompany or precede a request for consent and state what is processed and why (Act s.5(1); Rules r.3).",
        "Publish a privacy notice and link it from every page that collects personal data.",
        "Re-scan: a privacy policy page is found from the scanned pages.",
    ),
    "DPDP-N2": CheckDetail(
        "Cookies and trackers are disclosed.",
        "Met: a dedicated cookie policy page was found. Partially Met: tracking was observed and a general privacy policy exists "
        "but no dedicated cookie policy. Not Met: tracking was observed and neither was found. Not Applicable: no analytics or "
        "marketing tracking was observed.",
        "Policy pages found through the scanned pages, and the analytics and marketing items observed.",
        "The notice must itemise the personal data processed (Act s.5(1); Rules r.3(b)(i)).",
        "Publish a cookie policy that lists each cookie and tracker, its purpose and its provider, and link it from the banner.",
        "Re-scan: a cookie policy page is found and lists the cookies observed.",
    ),
    "DPDP-R9": CheckDetail(
        'The contact of the Data Protection Officer, or of a person who can answer questions, is published.',
        'Met: the privacy policy gives the contact, with an email address, phone number or form beside it. Partially Met: a role is named but no way to reach it is given. Unable to Verify: the policy text read does not state it (it may be published elsewhere), or the policy could not be read.',
        'A quotation from the privacy policy naming the officer or answering person with a way to reach them.',
        needed=('The name or title of the Data Protection Officer or of the person who answers for data processing.', 'An email address, phone number or contact form where they can be reached (Act s.8(9), Rules r.9).', 'The page where it is published, if it is not in the privacy policy.'),
    ),
    "DPDP-R14A": CheckDetail(
        'The means to exercise data principal rights are published.',
        'Met: the privacy policy gives a concrete means of making a request (an email address, form or portal) beside its statement of rights. Partially Met: rights are mentioned without any means. Unable to Verify: the policy text read does not state it, or the policy could not be read.',
        'A quotation from the privacy policy giving the means of making a rights request.',
        needed=('The form, portal or email address through which a person can ask to access, correct, complete, update or erase their data, or nominate someone (Rules r.14(1)).', 'The page where it is published, if it is not in the privacy policy.'),
    ),
    "DPDP-R14B": CheckDetail(
        'A grievance redressal system with a response period of at most 90 days is published.',
        'Met: the privacy policy gives a grievance contact and a response period within 90 days. Partially Met: the contact has no way to reach it, or the policy states a period above 90 days. Unable to Verify: no grievance contact or no response period was found in the text read, or the policy could not be read.',
        'A quotation from the privacy policy giving the grievance contact and the response period.',
        needed=('The grievance redressal policy, and the page where it is published.', 'The response period it commits to; the Rules allow at most 90 days (Rules r.14(3)).'),
    ),
    "DPDP-S6": CheckDetail(
        'Reasonable security safeguards are in place.',
        'Cannot be settled from a website scan or from what a policy says. The description of its safeguards in a policy is shown beside the result but never counts as evidence that they exist. Unable to Verify until the records of the organisation are supplied.',
        'The security records of the organisation.',
        needed=('Encryption, obfuscation, masking or tokenisation of personal data (Rules r.6(1)(a)).', 'Access control for the systems that hold personal data (r.6(1)(b)).', 'Logs, monitoring and review that detect unauthorised access, kept for one year (r.6(1)(c), (e)).', 'Data backups and a way to keep processing if data is lost or compromised (r.6(1)(d)).', 'Contracts with processors that require reasonable security safeguards (r.6(1)(f)).', 'Technical and organisational measures that make sure the safeguards are followed (r.6(1)(g)).'),
    ),
    "DPDP-S7": CheckDetail(
        'Personal data breaches are intimated to the Board and to affected individuals.',
        'Cannot be settled from a website scan or from what a policy says. The description of its breach response in a policy is shown beside the result but never counts as evidence that it works. Unable to Verify until the records of the organisation are supplied.',
        'The incident records of the organisation.',
        needed=('A written breach response plan naming who decides and who notifies.', 'The template and the route for telling each affected person: what happened, likely consequences, measures taken, safety steps, a contact (Rules r.7(1)).', 'The process for telling the Board without delay and with the detailed report within seventy-two hours (Rules r.7(2)).', 'The record of any breaches and the notices given.'),
    ),
    "DPDP-X4": CheckDetail(
        "Consent Manager registration and obligations.",
        "Applies only to a registered Consent Manager; cannot be assessed from a website scan.",
        "Registration and platform evidence, from the organisation.",
        needed=('Proof of registration as a Consent Manager and the policies that govern the consent records it keeps.',),
    ),
    "DPDP-X8": CheckDetail(
        "Erasure after the Third Schedule period, with 48 hours' notice.",
        "Applies only to the Third Schedule classes; cannot be assessed from a website scan.",
        "Retention and erasure records, from the organisation.",
        needed=('The erasure schedule and the notice given to people 48 hours before erasure.',),
    ),
    "DPDP-X10": CheckDetail(
        "Verifiable parental or guardian consent for children's data.",
        "Applies only where children's data is processed; cannot be assessed from a website scan.",
        "The age and parental-consent flow, from the organisation.",
        needed=('How parental or guardian consent is verified and the records of it.',),
    ),
    "DPDP-X13": CheckDetail(
        "Significant Data Fiduciary duties (annual DPIA and audit).",
        "Applies only to a notified Significant Data Fiduciary; cannot be assessed from a website scan.",
        "DPIA and audit reports, from the organisation.",
        needed=('The notification as a Significant Data Fiduciary, the annual impact assessment and the audit report.',),
    ),
}


APPLICABILITY_TEXT: dict[str, str] = {
    'DPDP-C1': 'Every website. Assessed when pages were scanned and the scan recorded the three consent states.',
    'DPDP-C2': 'Applies when analytics or marketing tracking is observed; otherwise Not Applicable.',
    'DPDP-C3': 'Applies when a consent mechanism exists and Reject could be operated; otherwise Not Applicable or Unable to Verify.',
    'DPDP-C4': 'Applies when a consent banner is shown; otherwise Not Applicable.',
    'DPDP-C5': 'Applies when a consent banner is shown; otherwise Not Applicable.',
    'DPDP-N1': 'Every website.',
    'DPDP-N2': 'Applies when analytics or marketing tracking is observed; otherwise Not Applicable.',
    'DPDP-R9': 'Every data fiduciary. Tested from what the privacy policy publishes; where the policy does not settle it, the organisation must supply the evidence.',
    'DPDP-R14A': 'Every data fiduciary. Tested from what the privacy policy publishes; where the policy does not settle it, the organisation must supply the evidence.',
    'DPDP-R14B': 'Every data fiduciary. Tested from what the privacy policy publishes; where the policy does not settle it, the organisation must supply the evidence.',
    'DPDP-S6': 'Every data fiduciary. A business check: the organisation must supply the evidence.',
    'DPDP-S7': 'Every data fiduciary. A business check: the organisation must supply the evidence, including its breach response times.',
    'DPDP-X4': 'Only a registered Consent Manager. Included only when declared applicable.',
    'DPDP-X8': 'Only the Third Schedule classes (large e-commerce, online gaming, social media). Included only when declared applicable.',
    'DPDP-X10': "Only where the organisation processes children's personal data. Included only when declared applicable.", 'DPDP-X13': 'Only a notified Significant Data Fiduciary. Included only when declared applicable.',
}

# What kind of legal basis each check has, and how far a website scan can speak to it. "Statutory duty" means the cited provision
# imposes the duty. The scan can only observe a signal of that duty; where the signal is Consiva's own reading of the provision
# rather than something the text names, the note says so. Provision numbers are those in `provisions`; they were mapped from the
# DPDP Act, 2023 and the DPDP Rules, 2025 (see LEGAL_SOURCES) and should be confirmed by counsel before being relied on.
LEGAL_BASIS_NOTES: dict[str, str] = {
    "DPDP-C1": "Statutory duty: consent must be given by a clear affirmative action before processing that depends on it. The scan "
               "observes what runs before any choice. It cannot tell whether another lawful ground in the Act covers an item.",
    "DPDP-C2": "Statutory duty: notice and a request for consent must reach the visitor. The scan observes whether a consent tool "
               "exists and was displayed. It does not assess whether the notice it carries meets the Rules.",
    "DPDP-C3": "Statutory duty: consent can be withdrawn and processing must stop. The scan tests this only after a Reject click that "
               "the banner confirmed, on the homepage.",
    "DPDP-C4": "Statutory duty: refusing must be as easy as giving consent. The scan checks that a reject-all option is present. How "
               "prominent it is, and how many clicks it takes, are not measured.",
    "DPDP-C5": "Statutory duty: consent must be specific. The scan detects per-purpose choices from banner wording. It cannot judge "
               "whether the purposes listed are specific enough.",
    "DPDP-N1": "Statutory duty: a notice must be given with specified content. The scan checks that a privacy-policy page exists on the "
               "domain. It does not read the policy, so it cannot show the content the Rules require is there.",
    "DPDP-N2": "Consiva's reading of a statutory duty: the notice must itemise the personal data collected. The Act and Rules do not name "
               "a cookie policy. A dedicated cookie-policy page is the clearest evidence this scan looks for; a general privacy policy "
               "counts as partial evidence.",
    "DPDP-R9": "Statutory duty: contact details of the officer or person who answers for data processing must be published. Read from "
               "the privacy policy text (a quotation, no further model call); where the policy does not state it, only organisational evidence settles it.",
    "DPDP-R14A": "Statutory duty: the means for people to exercise their rights must be published. Read from the privacy policy text "
                 "(a quotation, no further model call); where the policy does not state it, only organisational evidence settles it.",
    "DPDP-R14B": "Statutory duty: a grievance process with a response period of at most 90 days must be published. Read from the privacy "
                 "policy text (a quotation, no further model call); where the policy does not state it, only organisational evidence settles it.",
    "DPDP-S6": "Statutory duty: reasonable security safeguards. Internal controls are not visible from a public website, so this is "
               "assessed only from organisational evidence.",
    "DPDP-S7": "Statutory duty: breaches must be reported to the Board and to affected people. An incident process is not visible from "
               "a public website, so this is assessed only from organisational evidence.",
    "DPDP-X4": "Statutory duties of a registered Consent Manager. Included only when the organisation declares it is one.",
    "DPDP-X8": "Statutory duty of the Third Schedule classes (erasure after the specified period, with 48 hours' notice). Included "
               "only when the organisation declares it is in such a class.",
    "DPDP-X10": "Statutory duty where children's data is processed (verifiable parental or guardian consent). Included only when "
                "declared.",
    "DPDP-X13": "Statutory duties of a notified Significant Data Fiduciary (annual impact assessment and audit). Included only when "
                "declared.",
}

# Checks that rest on something NOT being found within the pages scanned: their conclusions are weaker than a positive observation.
ABSENCE_BASED = ('DPDP-C2', 'DPDP-N1', 'DPDP-N2')

# Not requirements a website scan or this score can assess, listed so nobody assumes they were overlooked.
OUT_OF_SCOPE: tuple[tuple[str, str], ...] = (
    ("Rule 5, Second Schedule", "Processing by the State and its instrumentalities."),
    ("Rule 15", "Transfer outside India applies only to requirements the Central Government specifies by order."),
    ("Rule 16", "Research, archiving and statistical exemption."),
    ("Rules 17-23", "Constitution and procedure of the Data Protection Board and the Appellate Tribunal."),
)

# A category has no weight of its own to configure: its weight is the sum of the weights of its applicable requirements, and the
# overall score is exactly the weight-averaged score of the categories. What each category is, and why it is weighted as it is:
CATEGORY_INFO: dict[str, str] = {
    "Consent": "The core of the Act's consent duty and the part of it a website scan can observe directly, so it carries the most weight.",
    "Notice": "Notice must precede consent. A scan can see whether a notice exists but not whether its content is adequate, so it "
              "carries less weight than consent.",
    "Data principal rights": "Statutory duties about rights and grievances. A scan cannot observe them; they are business checks.",
    "Security and breach": "Statutory safeguards and breach duties. A scan cannot observe them; they are business checks.",
    "Entity-specific": "Duties that apply only to some organisations. Included only when declared applicable; business checks.",
}

MIN_WEIGHT = 1
MAX_WEIGHT = 10


def resolve_weights(overrides: dict[str, int] | None = None) -> dict[str, int]:
    """The weight of every requirement: the documented default, or the configured override. Validated, so a typo, an unknown
    requirement or a weight outside 1..10 raises instead of silently changing a score."""
    weights = {r.id: r.weight for r in REQUIREMENTS}
    for rid, value in (overrides or {}).items():
        if rid not in weights:
            raise ValueError(f"Unknown requirement {rid!r} in the score weights; known: {', '.join(weights)}")
        if isinstance(value, bool) or not isinstance(value, int) or not MIN_WEIGHT <= value <= MAX_WEIGHT:
            raise ValueError(f"The weight of {rid} must be a whole number from {MIN_WEIGHT} to {MAX_WEIGHT}, not {value!r}")
        weights[rid] = value
    return weights


DEDUP_GROUPS: dict[str, str] = {
    "consent-before-tracking": "Tracking without a consent mechanism is one gap, counted once.",
    "choice-quality": "A banner with no reject option and no per-purpose choice is one gap, counted once.",
    "notice": "A site with no notice at all is one gap, counted once.",
}


# =======================================================================================================================
# Results
# =======================================================================================================================

@dataclass
class RequirementResult:
    id: str
    category: str
    title: str
    provisions: list[str]
    weight: int
    effective: date
    in_force: bool
    status: Status
    points_earned: float
    counts_in_score: bool
    observations: list[str]
    evidence_ids: list[str]
    rule_ids: list[str]
    exclusion_reason: str | None = None
    # The written definition of the check (CheckDetail) ...
    description: str = ""
    criteria: str = ""
    # ... and the evidence record: what was expected, what was observed (`observations`), where, and whether it is enough.
    expected_evidence: str = ""
    pages: list[str] = field(default_factory=list)
    sufficient: bool | None = None
    sufficiency_reason: str = ""
    limitation: str = ""
    # "in_force" or "future": a gap in a future duty is a readiness gap, never a current violation.
    timing: str = "future"
    # Set when this check is not scored because another check already carries the same gap.
    duplicate_of: str | None = None
    # The documented default weight, when a configured weight replaced it (`weight` is the one used).
    default_weight: int = 0
    # What to do about a gap, and how to confirm it is fixed.
    why_it_matters: str = ""
    remediation: str = ""
    verification: str = ""
    # "website": a public scan can observe it. "business": it needs internal documents, business information, integrations or
    # legal review, so a scan can never show it is met.
    check_type: str = "website"
    applicability: str = ""
    # "high", "medium" or "none" (nothing was observed, or the check does not apply), and the arithmetic behind the points.
    confidence: str = "none"
    scoring_rationale: str = ""
    # What kind of legal basis the check has and how far a scan can speak to it.
    legal_note: str = ""
    # Weight not earned (None when the check is not scored), the percentage points it adds to the score, and the share of the
    # applicable weight it represents. Filled in from the score block, never recomputed elsewhere.
    penalty_points: float | None = None
    score_contribution_percent: float | None = None
    coverage_share_percent: float | None = None
    # How this check was (or could be) answered, and for a check left unverified, the records that would settle it.
    method: str = "website_scan"
    needed_evidence: list[str] = field(default_factory=list)
    # Where a quotation came from (policy pages); merged into `pages` with the pages of observed items.
    source_pages: list[str] = field(default_factory=list)


@dataclass
class ScoreBlock:
    """One scoring basis over a set of requirements."""
    basis: Literal["readiness", "in_force"]
    status: Literal["scored", "insufficient_evidence"]
    score_percent: float | None
    points_earned: float
    points_assessed: int
    applicable_weight: int
    assessed_weight: int
    unable_to_verify_weight: int
    coverage_percent: float | None
    insufficient_reason: str | None
    categories: list[dict]
    caution: str | None = None


@dataclass
class MethodWeight:
    id: str
    title: str
    category: str
    check_type: str
    weight: int
    default_weight: int
    rationale: str
    applicability: str
    effective: date
    #: True when it is part of this assessment (not Not Applicable).
    applies_now: bool


@dataclass
class MethodCategory:
    name: str
    rationale: str
    weight: int                        # the sum of the weights of its requirements that apply now
    share_percent: float | None        # of the applicable weight
    requirement_ids: list[str]


@dataclass
class MethodDedup:
    group: str
    requirement_ids: list[str]
    explanation: str


@dataclass
class Methodology:
    """How the score was calculated, in words and numbers, for the dashboard and the PDF. Not a second calculation."""
    version: str
    weights_source: str                # "default" or "custom"
    weights_note: str
    score_formula: str
    coverage_formula: str
    points_rules: list[str]
    reliability_rule: str
    duplicate_rule: str
    timing_rule: str
    scope_rule: str
    legal_mapping_version: str
    legal_sources: list[dict]
    weights: list[MethodWeight]
    categories: list[MethodCategory]
    dedup: list[MethodDedup]


@dataclass
class Assessment:
    """What was assessed and when: the scan's scope, so a score can be read (and reproduced) against it."""
    scan_url: str | None
    main_domain: str | None
    scanned_at: str | None             # ISO timestamp the scan finished
    assessed_on: date                  # the date effective dates were judged against
    pages_scanned: list[str]
    page_limit: int
    states_tested: list[str]
    states_not_tested: list[str]
    nature_note: str


@dataclass
class LedgerMetric:
    """One percentage the score card shows, with exactly what it divides by what."""
    key: str
    name: str
    numerator: float
    denominator: float
    percent: float | None
    formula: str
    meaning: str


@dataclass
class StatusRule:
    """How one status is treated by the score, and which checks of this scan carry it."""
    status: str
    label: str
    rule: str
    in_numerator: str
    in_denominator: str
    on_coverage: str
    count: int
    weight: int
    check_ids: list[str]


@dataclass
class Ledger:
    """Every number on the score card, defined once. Counts and weights are tallied from the same results the score is computed
    from, and every metric reads its numerator and denominator from the score block, so nothing here is a second formula."""
    total_checks: int
    applicable_checks: int
    assessed_checks: int
    passed_checks: int
    partial_checks: int
    failed_checks: int
    unable_checks: int
    not_applicable_checks: int
    total_weight: int
    applicable_weight: int
    assessed_weight: int
    earned_weight: float
    penalty_weight: float
    unable_weight: int
    not_applicable_weight: int
    metrics: list[LedgerMetric]
    category_metrics: list[LedgerMetric]
    status_rules: list[StatusRule]
    penalty_note: str
    security_score_note: str
    not_applicable: list[dict]


def _n(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else f"{x:.1f}"


_STATUS_RULE_TEXT: dict[str, tuple[str, str, str, str, str]] = {
    "met": ("Met", "Earns 100% of the check's weight (factor 1). No penalty.", "Weight added in full", "Counted", "Counted as assessed"),
    "partially_met": ("Partially Met", "Earns 50% of the check's weight (factor 0.5). The other half is its penalty.",
                      "Half the weight added", "Counted", "Counted as assessed"),
    "not_met": ("Not Met", "Earns nothing (factor 0). Its whole weight is the penalty.", "Nothing added", "Counted",
                "Counted as assessed"),
    "unable_to_verify": ("Unable to Verify", "Not scored: there was not enough evidence. It is never counted as met or as not met, "
                         "and carries no penalty.", "Not added", "Not counted", "Lowers coverage: its weight stays in the applicable total"),
    "not_applicable": ("Not Applicable", "Left out: the check does not apply here, for the reason shown on the check. It is never "
                       "counted as a pass.", "Not added", "Not counted", "Does not lower coverage: its weight is removed from the applicable total"),
}


def build_ledger(results: list[RequirementResult], block: ScoreBlock, in_force: ScoreBlock) -> Ledger:
    """Tally the checks and weights behind the readiness score, and label each ratio with what it actually measures."""
    def of(status: str) -> list[RequirementResult]:
        return [r for r in results if r.status == status]

    applicable = [r for r in results if r.status != NOT_APPLICABLE]
    assessed = [r for r in results if r.counts_in_score]
    unable = of("unable_to_verify")
    na = of(NOT_APPLICABLE)
    earned, lost = block.points_earned, sum(r.weight for r in assessed) - block.points_earned

    score_meaning = (
        "Of the weight we could test, the share the site earned. It says nothing about the weight we could not test."
        if block.status == "scored" else f"No score is shown: {block.insufficient_reason}"
    )
    metrics = [
        LedgerMetric("score", "Readiness score (weighted)", block.points_earned, block.points_assessed, block.score_percent,
                     f"{_n(block.points_earned)} ÷ {block.points_assessed} × 100", score_meaning),
        LedgerMetric("coverage", "Assessment coverage (by weight)", block.assessed_weight, block.applicable_weight, block.coverage_percent,
                     f"{block.assessed_weight} ÷ {block.applicable_weight} × 100",
                     "Of all the weight that applies to this site, the share a scan could test. It is by weight, not by number of checks."),
        LedgerMetric("untested", "Applicable weight not verified", block.unable_to_verify_weight, block.applicable_weight,
                     _pct(block.unable_to_verify_weight, block.applicable_weight) if block.applicable_weight else None,
                     f"{block.unable_to_verify_weight} ÷ {block.applicable_weight} × 100",
                     "The share of applicable weight left untested. Together with coverage it makes up the applicable weight when "
                     "every applicable check is either assessed or unable to verify."),
        LedgerMetric("in_force", "In-force score (duties already in force)", in_force.points_earned, in_force.points_assessed,
                     in_force.score_percent, f"{_n(in_force.points_earned)} ÷ {in_force.points_assessed} × 100"
                     if in_force.points_assessed else "no check is in force on the assessment date",
                     "The same formula over only the duties already in force on the assessment date."
                     if in_force.status == "scored" else (in_force.insufficient_reason or "Not scored.")),
    ]
    cat_metrics: list[LedgerMetric] = []
    for c in block.categories:
        cat_metrics.append(LedgerMetric(
            f"score:{c['category']}", f"{c['category']}: score", c["points_earned"], c["assessed_weight"], c["score_percent"],
            f"{_n(c['points_earned'])} ÷ {c['assessed_weight']} × 100" if c["assessed_weight"] else "nothing in this category was assessed",
            "Earned weight over assessed weight within this category."))
        cat_metrics.append(LedgerMetric(
            f"coverage:{c['category']}", f"{c['category']}: coverage", c["assessed_weight"], c["applicable_weight"], c["coverage_percent"],
            f"{c['assessed_weight']} ÷ {c['applicable_weight']} × 100",
            "Assessed weight over applicable weight within this category."))
    rules = []
    for status in STATUSES:
        label, rule, num, den, cov = _STATUS_RULE_TEXT[status]
        rs = of(status)
        rules.append(StatusRule(status, label, rule, num, den, cov, len(rs), sum(r.weight for r in rs), [r.id for r in rs]))
    return Ledger(
        total_checks=len(results), applicable_checks=len(applicable), assessed_checks=len(assessed),
        passed_checks=len(of("met")), partial_checks=len(of("partially_met")), failed_checks=len(of("not_met")),
        unable_checks=len(unable), not_applicable_checks=len(na),
        total_weight=sum(r.weight for r in results), applicable_weight=block.applicable_weight, assessed_weight=block.assessed_weight,
        earned_weight=earned, penalty_weight=lost, unable_weight=block.unable_to_verify_weight,
        not_applicable_weight=sum(r.weight for r in na),
        metrics=metrics, category_metrics=cat_metrics, status_rules=rules,
        penalty_note=(
            "This scoring has no separate penalty list. The penalty on a check is the weight it did not earn: 0 for Met, half the "
            "weight for Partially Met, the whole weight for Not Met. The multipliers in the rules (1, 0.5, 0) are the share of the "
            "weight a check EARNS, so 1 means met and no penalty, and 0 means not met and the full penalty. Unable to Verify and "
            "Not Applicable carry no penalty because they are not scored."),
        security_score_note=(
            "There is no separate security score. The \"Security and breach\" category holds two duties a website scan cannot "
            "observe, so it is shown as Not assessed and contributes nothing to any number here."),
        not_applicable=[{"id": r.id, "reason": r.exclusion_reason or "", "duplicate_of": r.duplicate_of} for r in na],
    )


def _attach_contributions(results: list[RequirementResult], block: ScoreBlock) -> None:
    """Per-check numbers for the check table: the penalty (weight not earned), the percentage points the check adds to the score,
    and the share of the applicable weight the check represents. Read from the score block, so they always sum to its figures."""
    for r in results:
        if r.counts_in_score:
            r.penalty_points = float(r.weight) - r.points_earned
            r.score_contribution_percent = _pct(r.points_earned, block.points_assessed) if block.points_assessed else None
        if r.status != NOT_APPLICABLE and block.applicable_weight:
            r.coverage_share_percent = _pct(r.weight, block.applicable_weight)


@dataclass
class DpdpScore:
    methodology_version: str
    as_of: date
    requirements: list[RequirementResult]
    readiness: ScoreBlock
    in_force: ScoreBlock
    disclaimer: str = DISCLAIMER
    status_counts: dict = field(default_factory=dict)
    evidence_summary: review.EvidenceSummary | None = None
    breakdown: review.Breakdown | None = None
    human_review: review.HumanReview | None = None
    finding_groups: findings.FindingGroups | None = None
    methodology: Methodology | None = None
    assessment: Assessment | None = None
    remediation_plan: list[findings.RemediationStep] = field(default_factory=list)
    ledger: Ledger | None = None
    # The readiness score taken apart: every check that lost points, how many, why, on what evidence (scoring/deductions.py).
    deductions: deductions_module.Deductions | None = None
    # Which kind of website this looks like and which provisions depend on it. Informational: it never changes a score.
    sector: sector_analysis.SectorAssessment | None = None
    thresholds: dict = field(default_factory=lambda: {
        "min_coverage_percent": MIN_COVERAGE_PERCENT, "min_assessed_requirements": MIN_ASSESSED_REQUIREMENTS,
    })
    out_of_scope: list[dict] = field(default_factory=lambda: [{"provision": p, "reason": r} for p, r in OUT_OF_SCOPE])


def today_in_india() -> date:
    """Today's date in India (UTC+5:30, no daylight saving). The Act and Rules start on India dates, so an obligation that starts on 13 May 2027 is in force from
    the start of that day in India, not from 05:30 India time (the start of that day in UTC)."""
    return (datetime.now(UTC) + timedelta(hours=5, minutes=30)).date()


def _pct(numerator: Fraction | int, denominator: Fraction | int) -> float:
    """numerator / denominator x 100, exact until the end, then rounded half-up to one decimal place."""
    q = Fraction(numerator) / Fraction(denominator) * 100
    return float((Decimal(q.numerator) / Decimal(q.denominator)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def _suppress_duplicates(results: list[RequirementResult]) -> None:
    """Within a dedup group, if more than one requirement is Not Met, only the heaviest keeps its penalty (ties: the one
    listed first). The others are suppressed: removed from numerator AND denominator, never silently passed as Met."""
    by_group: dict[str, list[RequirementResult]] = {}
    group_of = {r.id: r.dedup_group for r in REQUIREMENTS}
    for res in results:
        g = group_of[res.id]
        if g and res.status == "not_met":
            by_group.setdefault(g, []).append(res)
    for g, failing in by_group.items():
        if len(failing) < 2:
            continue
        keep = max(failing, key=lambda r: (r.weight, -[x.id for x in REQUIREMENTS].index(r.id)))
        for res in failing:
            if res is keep:
                continue
            res.status = NOT_APPLICABLE
            res.points_earned = 0.0
            res.counts_in_score = False
            res.duplicate_of = keep.id
            res.exclusion_reason = f"Counted under {keep.id}, which already carries this gap. {DEDUP_GROUPS[g]}"


def evaluate_requirements(
    evidence: dict, as_of: date, applicability: Applicability | None = None, ctx: ConsentContext | None = None,
    weights: dict[str, int] | None = None,
) -> list[RequirementResult]:
    applicability = applicability or Applicability()
    weights = weights or resolve_weights()
    ctx = ctx or build_consent_context(evidence)
    results: list[RequirementResult] = []
    for req in REQUIREMENTS:
        base = {
            "id": req.id, "category": req.category, "title": req.title, "provisions": list(req.provisions),
            "weight": weights[req.id], "default_weight": req.weight, "effective": req.effective, "in_force": req.effective <= as_of,
            "rule_ids": list(req.rule_ids),
        }
        if req.applies_if is not None and applicability.flag(req.applies_if) is not True:
            declared = applicability.flag(req.applies_if)
            reason = ("Declared as not applicable to this organisation." if declared is False
                      else "Applies only to some organisations; excluded unless declared applicable.")
            results.append(RequirementResult(**base, status="not_applicable", points_earned=0.0, counts_in_score=False,
                                             observations=[], evidence_ids=[], exclusion_reason=reason))
            continue
        assert req.check is not None
        out = req.check(ctx)
        counts = out.status in ASSESSED_STATUSES
        earned = float(SCORED_FACTOR[out.status] * weights[req.id]) if counts else 0.0
        results.append(RequirementResult(
            **base, status=out.status, points_earned=earned, counts_in_score=counts, observations=list(out.observations),
            evidence_ids=list(dict.fromkeys(out.evidence_ids)), method=req.method, source_pages=list(dict.fromkeys(out.pages)),
            exclusion_reason=out.observations[0] if out.status == "not_applicable" and out.observations else None,
        ))
    _suppress_duplicates(results)
    _attach_evidence_records(results, ctx)
    return results


_STATUS_WORDS = {"met": "Met", "partially_met": "Partially Met", "not_met": "Not Met", "unable_to_verify": "Unable to Verify",
                 "not_applicable": "Not Applicable"}


def _confidence(res: RequirementResult, ctx: ConsentContext) -> str:
    """How far to trust a result: only an assessed website check has any. A conclusion that rests on something NOT being found,
    or on a single page, or that is only partly met, is medium; a direct observation across several pages is high."""
    if res.status not in ASSESSED_STATUSES:
        return "none"
    if res.status == "partially_met" or res.id in ABSENCE_BASED or res.method == "policy_text" or len(ctx.pages) < 2:
        return "medium"
    return "high"


def _rationale(res: RequirementResult, req: Requirement) -> str:
    if res.status in ASSESSED_STATUSES:
        earned = int(res.points_earned) if float(res.points_earned).is_integer() else res.points_earned
        return (f"{_STATUS_WORDS[res.status]} earns {earned} of {res.weight} points. Weight {res.weight}"
                f"{'' if res.weight == req.weight else f' (configured; documented default {req.weight})'}: {req.weight_rationale}")
    if res.status == "unable_to_verify":
        return (f"Not scored: it is left out of the score and counted against coverage, never treated as met. "
                f"Weight {res.weight}: {req.weight_rationale}")
    return f"Not scored: {res.exclusion_reason or 'not applicable.'}"


def _page_lookup(ctx: ConsentContext) -> dict[str, list[str]]:
    """evidence id -> the main-domain pages it was observed on (trackers by page; cookies by their recorded sightings)."""
    out: dict[str, list[str]] = {}
    for t in ctx.trackers:
        if t.get("page_url"):
            out[t["id"]] = [t["page_url"]]
    for c in ctx.cookies:
        urls = [o.get("page_url") for o in (c.get("observations") or []) if o.get("page_url")]
        if urls:
            out[c["id"]] = list(dict.fromkeys(urls))
    return out


def _attach_evidence_records(results: list[RequirementResult], ctx: ConsentContext) -> None:
    """Fill in where each result's evidence came from and whether it is enough. Never changes a status: it only describes."""
    by_req = {r.id: r for r in REQUIREMENTS}
    lookup = _page_lookup(ctx)
    scanned = [p["url"] for p in ctx.pages if p.get("url")]
    for res in results:
        req = by_req[res.id]
        detail = DETAILS[res.id]
        res.description, res.criteria, res.expected_evidence = detail.description, detail.criteria, detail.expected_evidence
        res.timing = "in_force" if res.in_force else "future"
        res.check_type = ("policy" if req.method == "policy_text" and res.status in ASSESSED_STATUSES else "website" if req.scan_assessable else "business")
        res.needed_evidence = list(detail.needed) if res.status == "unable_to_verify" else []
        res.applicability = APPLICABILITY_TEXT[res.id]
        res.legal_note = LEGAL_BASIS_NOTES[res.id]
        res.confidence = _confidence(res, ctx)
        res.scoring_rationale = _rationale(res, req)
        if res.status in ("not_met", "partially_met"):
            res.why_it_matters, res.remediation, res.verification = detail.why_it_matters, detail.remediation, detail.verification
        pages = list(dict.fromkeys([*res.source_pages, *(u for i in res.evidence_ids for u in lookup.get(i, []))]))
        if not pages and req.scan_assessable and res.status != "not_applicable":
            banner = res.id in ("DPDP-C2", "DPDP-C3", "DPDP-C4", "DPDP-C5") and ctx.interaction_page
            pages = [ctx.interaction_page] if banner else scanned
        res.pages = pages
        n = len(scanned)
        if res.status in ASSESSED_STATUSES and req.method == "policy_text":
            res.sufficient = True
            res.sufficiency_reason = ("The privacy policy names it without the concrete detail the Rules ask for." if res.status == "partially_met"
                                      else "Quoted from the privacy policy, word for word.")
            res.limitation = "Covers only the privacy and cookie policy pages that were read; it shows what is published, not how it is carried out."
        elif res.status in ASSESSED_STATUSES:
            res.sufficient = True
            res.sufficiency_reason = ("The evidence supports only part of the requirement." if res.status == "partially_met"
                                      else "Direct observation on the scanned pages.")
            res.limitation = f"Covers only the {n} main-domain page(s) scanned."
        elif res.status == "unable_to_verify":
            res.sufficient = False
            res.sufficiency_reason = res.observations[0] if res.observations else "The scan evidence is insufficient."
            res.limitation = (("The privacy policy text that was read does not settle this." if res.source_pages or (ctx.policy_analysis or {}).get("status") == "analysed"
                               else "A website scan cannot observe this.") if not req.scan_assessable
                              else f"Covers only the {n} main-domain page(s) scanned.")
        else:
            res.sufficient = None
            res.sufficiency_reason = res.exclusion_reason or ""
            res.limitation = ""



def _block(basis: Literal["readiness", "in_force"], results: list[RequirementResult]) -> ScoreBlock:
    pool = [r for r in results if r.status != "not_applicable" and (basis == "readiness" or r.in_force)]
    assessed = [r for r in pool if r.counts_in_score]
    unable = [r for r in pool if r.status == "unable_to_verify"]
    applicable_w = sum(r.weight for r in pool)
    assessed_w = sum(r.weight for r in assessed)
    earned = sum(Fraction(r.points_earned).limit_denominator(2) for r in assessed)
    coverage = _pct(assessed_w, applicable_w) if applicable_w else None

    reason = None
    if not pool:
        reason = ("No requirement is in force on the assessment date." if basis == "in_force"
                  else "No requirement applies.")
    elif len(assessed) < MIN_ASSESSED_REQUIREMENTS:
        reason = (f"Only {len(assessed)} requirement(s) could be assessed; at least {MIN_ASSESSED_REQUIREMENTS} are needed.")
    elif coverage is not None and coverage < MIN_COVERAGE_PERCENT:
        reason = f"Assessment coverage is {coverage}%, below the {MIN_COVERAGE_PERCENT:g}% needed for a score."
    scored = reason is None
    caution = None
    if scored and coverage is not None and (coverage < CAUTION_COVERAGE_PERCENT or len(assessed) < CAUTION_MIN_REQUIREMENTS):
        caution = (f"Treat this score with caution: it rests on {len(assessed)} assessed requirement{'s' if len(assessed) != 1 else ''} "
                   f"and {coverage}% coverage. Read it together with the coverage and the checks that could not be verified.")

    categories = []
    for cat in dict.fromkeys(r.category for r in pool):
        cp = [r for r in pool if r.category == cat]
        ca = [r for r in cp if r.counts_in_score]
        cw, caw = sum(r.weight for r in cp), sum(r.weight for r in ca)
        ce = sum(Fraction(r.points_earned).limit_denominator(2) for r in ca)
        categories.append({
            "category": cat, "applicable_weight": cw, "assessed_weight": caw,
            "points_earned": float(ce),
            "score_percent": _pct(ce, caw) if caw else None,
            "coverage_percent": _pct(caw, cw) if cw else None,
            "status": "scored" if caw else "not_assessed",
        })
    return ScoreBlock(
        basis=basis, status="scored" if scored else "insufficient_evidence",
        score_percent=_pct(earned, assessed_w) if scored and assessed_w else None,
        points_earned=float(earned), points_assessed=assessed_w, applicable_weight=applicable_w, assessed_weight=assessed_w,
        unable_to_verify_weight=sum(r.weight for r in unable), coverage_percent=coverage, insufficient_reason=reason,
        categories=categories, caution=caution,
    )


def build_methodology(results: list[RequirementResult], weights: dict[str, int]) -> Methodology:
    custom = {rid: w for rid, w in weights.items() if w != next(r.weight for r in REQUIREMENTS if r.id == rid)}
    by_res = {r.id: r for r in results}
    live = [r for r in results if r.status != "not_applicable"]
    total = sum(r.weight for r in live)
    cats: dict[str, list[RequirementResult]] = {}
    for r in live:
        cats.setdefault(r.category, []).append(r)
    groups: dict[str, list[str]] = {}
    for req in REQUIREMENTS:
        if req.dedup_group:
            groups.setdefault(req.dedup_group, []).append(req.id)
    return Methodology(
        version=METHODOLOGY_VERSION,
        weights_source="custom" if custom else "default",
        weights_note=("Documented default weights." if not custom else
                      "CUSTOM weights are in use for: " + ", ".join(f"{k} = {v} (default {next(r.weight for r in REQUIREMENTS if r.id == k)})"
                                                                    for k, v in sorted(custom.items()))
                      + ". Scores made with different weights are not comparable."),
        score_formula="DPDP Compliance Score (%) = (weighted points earned ÷ weighted points assessed) × 100",
        coverage_formula="Assessment coverage (%) = (weight of assessed requirements ÷ weight of applicable requirements) × 100. "
                         "It is shown separately from the score: a high score with low coverage is not a reliable result.",
        points_rules=[
            "Met earns 100% of the requirement's weight, only when the evidence meets its written criteria.",
            "Partially Met earns 50%, only when the evidence supports part of the requirement.",
            "Not Met earns 0%, only when sufficient evidence supports an identified gap.",
            ("Unable to Verify and Not Applicable earn nothing and are left out of the denominator; Unable to Verify lowers coverage "
             "and is never counted as met or as not met."),
        ],
        reliability_rule=(f"A score is shown only when at least {MIN_ASSESSED_REQUIREMENTS} requirements were assessed and coverage is at "
                          f"least {MIN_COVERAGE_PERCENT:g}%. Otherwise the result is \"Insufficient Evidence\"."),
        duplicate_rule=("Each requirement is scored once, however many requests, cookies or pages relate to it. Where two "
                        "requirements describe the same underlying gap, only the heavier one is penalised and the other is Not "
                        "Applicable with the reason."),
        timing_rule=("Requirements not yet in force are scored as readiness and shown as Future Readiness, never as current "
                     "violations. A separate in-force score covers only duties already in force."),
        scope_rule=f"Only the entered main domain, up to {MAX_WEB_PAGES} pages. Other websites are not assessed.",
        legal_mapping_version=LEGAL_MAPPING_VERSION,
        legal_sources=[{"name": n, "reference": r} for n, r in LEGAL_SOURCES],
        weights=[MethodWeight(
            r.id, r.title, r.category, "website" if r.scan_assessable else "business", weights[r.id], r.weight, r.weight_rationale,
            APPLICABILITY_TEXT[r.id], r.effective, by_res[r.id].status != "not_applicable") for r in REQUIREMENTS],
        categories=[MethodCategory(
            name, CATEGORY_INFO.get(name, ""), sum(r.weight for r in rs),
            _pct(sum(r.weight for r in rs), total) if total else None, [r.id for r in rs]) for name, rs in cats.items()],
        dedup=[MethodDedup(g, ids, DEDUP_GROUPS[g]) for g, ids in groups.items()],
    )


def _main_domain(url: str | None) -> str | None:
    if not url:
        return None
    from urllib.parse import urlparse

    host = (urlparse(url if "//" in url else f"//{url}").hostname or "").lower()
    return host[4:] if host.startswith("www.") else (host or None)


def build_assessment(ctx: ConsentContext, as_of: date, scan_url: str | None, scanned_at: str | None) -> Assessment:
    """The consent states the scan actually exercised. Before Consent is tested whenever pages were scanned with the three-state
    scanner; After Accept and After Reject only when the banner was clicked (Reject also had to be confirmed)."""
    tested, not_tested = [], []
    (tested if ctx.pages and ctx.three_pass else not_tested).append("Before Consent")
    (tested if ctx.pages and ctx.accept_outcome == "clicked" else not_tested).append("After Accept")
    (tested if ctx.pages and ctx.reject_outcome == "clicked" and ctx.reject_confirmed else not_tested).append("After Reject")
    return Assessment(
        scan_url=scan_url, main_domain=_main_domain(scan_url), scanned_at=scanned_at, assessed_on=as_of,
        pages_scanned=[p["url"] for p in ctx.pages if p.get("url")], page_limit=MAX_WEB_PAGES,
        states_tested=tested, states_not_tested=not_tested, nature_note=NATURE_NOTE,
    )


def canonical_evidence(evidence: dict) -> dict:
    """The same evidence in one fixed order. The evidence queries do not order their rows, so the same stored scan can come back in a different row order on
    each read; the score must never depend on that. Every list is sorted by its own content (not by an id or a position), so two copies of the same evidence
    always reach the rules as identical input. The input is not changed."""
    return {
        key: sorted(value, key=lambda item: json.dumps(item, sort_keys=True, default=str)) if isinstance(value, list) else value
        for key, value in evidence.items()
    }


def assess(
    evidence: dict, as_of: date | None = None, applicability: Applicability | None = None,
    weights: dict[str, int] | None = None, scan_url: str | None = None, scanned_at: str | None = None,
    business_sector: str | None = None,
) -> DpdpScore:
    """`weights` replaces the documented default weight of the requirements it names (validated; see resolve_weights).
    `scan_url` and `scanned_at` only describe the scan in the result; they never change a score."""
    as_of = as_of or today_in_india()
    resolved = resolve_weights(weights)
    evidence = canonical_evidence(evidence)
    ctx = build_consent_context(evidence)
    results = evaluate_requirements(evidence, as_of, applicability, ctx, resolved)
    human_review = review.build_human_review(ctx, results, as_of, MAX_WEB_PAGES)
    readiness, in_force = _block("readiness", results), _block("in_force", results)
    _attach_contributions(results, readiness)
    return DpdpScore(
        methodology_version=METHODOLOGY_VERSION, as_of=as_of, requirements=results,
        readiness=readiness, in_force=in_force,
        status_counts={st: sum(1 for r in results if r.status == st) for st in STATUSES},
        evidence_summary=review.summarise_evidence(ctx),
        breakdown=review.build_breakdown(results, human_review.items),
        human_review=human_review,
        finding_groups=findings.build_finding_groups(results, human_review.items, ctx, as_of),
        methodology=build_methodology(results, resolved),
        assessment=build_assessment(ctx, as_of, scan_url, scanned_at),
        remediation_plan=findings.build_remediation_plan(results, readiness),
        ledger=build_ledger(results, readiness, in_force),
        deductions=deductions_module.build_deductions(results, readiness, [*ctx.trackers, *ctx.cookies]),
        sector=sector_analysis.assess_sector(evidence, scan_url, applicability, business_sector),
    )
