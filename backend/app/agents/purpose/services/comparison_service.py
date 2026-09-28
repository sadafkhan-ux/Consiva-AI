"""Declared purpose versus observed purpose. This is the agent's actual work.

Everything else in this package reads existing evidence. This module is the only part
that produces a new claim, so it carries the whole accuracy burden.

THE DISCIPLINE IT INHERITS

Three rules, each of which exists because breaking it has already cost this codebase a
real incident or a real correction:

  1. A mismatch requires BOTH purposes to map into the same vocabulary with confidence.
     Otherwise the result is `undetermined`. A vocabulary artefact reported as a
     compliance mismatch is a confident, specific, false statement about a customer's
     data -- the worst output this platform can produce.

  2. Nothing is invented. Where there is no declared purpose there is no mismatch, only
     an absence, and the absence is reported as such.

  3. A high-severity finding always requires human review, forced here rather than left
     to a caller. Whether a compliance finding reaches a customer unread must not depend
     on someone remembering a flag.

WHAT IT CAN FIND WITHOUT ANY DECLARED PURPOSE

One finding needs only observed evidence, and it is the most valuable one available
today: a tracker or cookie whose purpose requires consent, observed firing in a state
where no consent had been granted. Both halves already sit in `trackers`/`cookies`;
nothing in the platform previously compared them.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.agents.purpose.rules import reconciliation
from app.agents.purpose.services.declared_service import DeclaredPurpose
from app.agents.purpose.services.observed_service import ObservedPurpose

# Findings this agent can raise.
MISMATCH = "purpose_mismatch"
WITHOUT_CONSENT = "processing_without_consent"
UNDECLARED = "purpose_undeclared"
RETENTION = "retention_review"


@dataclass
class Assessment:
    """One comparison, plus any finding it justifies."""

    subject_type: str
    subject_ref: str
    subject_label: str
    declared_purpose: str | None
    declared_source: str
    observed_purpose: str | None
    observed_source: str
    alignment: str
    confidence: float
    reason: str
    evidence_refs: list[str] = field(default_factory=list)
    retention_status: str = "not_evaluated"
    retention_note: str | None = None
    finding: Finding | None = None


@dataclass
class Finding:
    finding_type: str
    severity: str
    title: str
    description: str

    @property
    def review_required(self) -> bool:
        """Always true. Every finding this agent produces is for a person to read.

        This USED to read `True if self.severity == "high" else True` -- a tautology
        dressed as a severity rule, with a docstring claiming high severity was forced
        and everything else "honoured upward". Nothing was honoured: there was no
        stored flag to honour, so the branch could not have done anything. Found by
        ruff, not by a test, because the behaviour was right and only the code lied.

        The behaviour is right. Phase 1 produces findings from deterministic rules
        against evidence the agent did not gather itself, and none of them -- not even
        a low-severity `purpose_undeclared` -- is something the platform should mark
        settled on a customer's behalf. Measured on live data: 69 findings, 14 high and
        55 low, all requiring review.

        Deliberately a property rather than a stored field, so a caller cannot set it
        to False. Making it severity-dependent later means adding a real field for the
        caller's request and forcing it true for high severity here -- which is what
        the old docstring described and the old code did not do.
        """
        return True


def assess(
    observed: ObservedPurpose,
    declared_index: dict[str, DeclaredPurpose],
    consent_obtained: bool | None,
) -> Assessment:
    """Compare one observed use against whatever was declared for it."""
    declared = _lookup(observed, declared_index)

    if declared is None:
        alignment, confidence, reason = reconciliation.UNDETERMINED, 0.0, (
            "no declared purpose could be matched to this item"
        )
        declared_purpose, declared_source = None, "none"
    else:
        alignment, confidence, reason = reconciliation.compare(declared.purpose, observed.purpose)
        declared_purpose, declared_source = declared.purpose, declared.source

    assessment = Assessment(
        subject_type=observed.subject_type,
        subject_ref=observed.subject_ref,
        subject_label=observed.subject_label,
        declared_purpose=declared_purpose,
        declared_source=declared_source,
        observed_purpose=observed.purpose,
        observed_source="consent_scan",
        alignment=alignment,
        confidence=confidence,
        reason=reason,
        evidence_refs=list(observed.evidence_refs),
    )
    assessment.finding = _finding_for(assessment, observed, consent_obtained)
    return assessment


def _lookup(
    observed: ObservedPurpose, declared_index: dict[str, DeclaredPurpose]
) -> DeclaredPurpose | None:
    """Find the declaration covering this item.

    Exact key match only. Fuzzy matching a vendor host against a processing-activity
    name would manufacture the very declaration the comparison then judges, and a
    finding built on a guessed pairing is indistinguishable from a real one once it
    reaches a report.
    """
    if not declared_index:
        return None
    for key in [observed.subject_label, *observed.lookup_aliases]:
        hit = declared_index.get(str(key).strip().lower())
        if hit is not None:
            return hit
    return None


def _finding_for(
    assessment: Assessment,
    observed: ObservedPurpose,
    consent_obtained: bool | None,
) -> Finding | None:
    """The finding this assessment justifies, if any.

    Ordered by severity of the underlying fact, and it returns at the first match: an
    item that both fired without consent AND contradicts its declaration is reported as
    the former, because that is the stronger, more actionable statement.
    """
    # 1. Processing without a lawful basis. Needs no declared purpose at all.
    if observed.requires_consent and observed.fired_without_consent:
        states = ", ".join(observed.consent_states) or "unknown"
        basis = (
            "No consent mechanism was confirmed on this site."
            if consent_obtained is None
            else "The consent control was found but could not be confirmed as working."
            if consent_obtained is False
            else "A consent control was confirmed, but this item fired outside the "
                 "state where consent applies."
        )
        return Finding(
            finding_type=WITHOUT_CONSENT,
            severity="high",
            title=f"{observed.purpose.title()} {observed.subject_type.replace('_', ' ')} "
                  f"active without consent: {observed.subject_label}",
            description=(
                f"{observed.subject_label} is classified as {observed.purpose!r}, a purpose "
                f"that requires consent, and was observed in these consent states: {states}"
                f"{f' across {observed.occurrences} requests' if observed.occurrences > 1 else ''}. "
                f"{basis} Processing for a consent-requiring purpose before consent is "
                f"granted, or after it is refused, has no lawful basis."
            ),
        )

    # 2. A genuine contradiction between declaration and behaviour.
    if assessment.alignment == "mismatch":
        return Finding(
            finding_type=MISMATCH,
            severity="medium",
            title=f"Declared and observed purpose disagree: {observed.subject_label}",
            description=(
                f"{observed.subject_label} is declared as {assessment.declared_purpose!r} "
                f"but was observed being used for {observed.purpose!r}. {assessment.reason}. "
                f"Either the declaration is out of date or the data is being used beyond "
                f"the purpose it was collected for."
            ),
        )

    # 3. Observed processing with nothing declared anywhere. Low severity because an
    #    absent record is a documentation gap, not evidence of misuse.
    if assessment.declared_purpose is None and observed.purpose:
        return Finding(
            finding_type=UNDECLARED,
            severity="low",
            title=f"No declared purpose for {observed.subject_label}",
            description=(
                f"{observed.subject_label} was observed being used for "
                f"{observed.purpose!r}, but no declared purpose was found for it. This "
                f"is a gap in the record of processing, not evidence that the use is "
                f"improper."
            ),
        )

    return None


def summarise(assessments: list[Assessment]) -> dict[str, int]:
    """Counts for the run record and the console."""
    summary = {
        "total": len(assessments),
        "aligned": 0,
        "mismatch": 0,
        "undetermined": 0,
        "findings": 0,
        "high": 0,
        "medium": 0,
        "low": 0,
    }
    for item in assessments:
        summary[item.alignment] = summary.get(item.alignment, 0) + 1
        if item.finding:
            summary["findings"] += 1
            summary[item.finding.severity] += 1
    return summary
