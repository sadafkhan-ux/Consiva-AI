"""Severity and risk assessment from evidence (prompt §10, §19).

Deterministic and additive. Every factor that moves the score is recorded with its
contribution, so a reviewer can disagree with one input rather than with an opaque
number. No model produces a score here; a model may later explain one.

THE TWO RULES THAT SHAPE THIS ENGINE
------------------------------------
1. UNCERTAINTY IS NOT ZERO. An unknown subject count is not "nobody affected", and an
   unconfirmed exfiltration is not "no exfiltration". Where evidence is missing the
   engine says so in its confidence and its reason, and it does NOT quietly score the
   incident as low. Scoring absence of evidence as absence of harm is how a serious
   breach gets triaged as routine.

2. CONFIDENCE IS REPORTED, NOT FOLDED IN. A high score held with low confidence is a
   different thing from a high score held with high confidence, and a single number
   cannot carry both. They are separate outputs, and the caller is expected to show
   both.

`assess_severity` answers "how bad is this?" and runs at intake on thin evidence.
`assess_risk` answers "what is the risk to the people involved?" and runs once impact
is known. They are separate because they are asked at different times, of different
evidence, by different people.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from app.agents.breach.schemas import incident

# Score bands. Deliberately coarse -- an incident is not meaningfully "a 63".
BAND_LOW = 25
BAND_MEDIUM = 50
BAND_HIGH = 75

# Category weights. These are ORGANISATIONAL DEFAULTS, not legal determinations:
# credentials and government identifiers score highest because they enable further
# harm, not because any statute says so.
_CATEGORY_WEIGHT: dict[str, int] = {
    "Credential / Secret": 30,
    "Government Identifier / High-Risk": 30,
    "Health Data": 28,
    "Financial Data": 25,
    "Identity Data": 18,
    "Location Data": 15,
    "Contact Data": 12,
    "Employment Data": 12,
    "Professional / Social Profile": 8,
    "Online Identifier": 8,
    "Behavioural Data": 6,
    "Potential Personal Data (free text)": 10,
}
# A category the engine does not recognise still counts for something -- an unknown
# category is not a harmless one.
_UNKNOWN_CATEGORY_WEIGHT = 10

_TYPE_WEIGHT: dict[str, int] = {
    incident.TYPE_DATA_LEAKAGE: 25,
    incident.TYPE_MALWARE: 22,
    incident.TYPE_INSIDER: 20,
    incident.TYPE_CREDENTIAL_COMPROMISE: 20,
    incident.TYPE_UNAUTHORIZED_ACCESS: 16,
    incident.TYPE_DATA_EXPOSURE: 15,
    incident.TYPE_THIRD_PARTY: 14,
    incident.TYPE_MISCONFIGURATION: 12,
    incident.TYPE_ACCIDENTAL_DISCLOSURE: 10,
    incident.TYPE_LOST_DEVICE: 10,
    incident.TYPE_OTHER: 8,
    incident.TYPE_UNCLASSIFIED: 8,
}


@dataclass(frozen=True)
class Factor:
    """One thing that moved the score, and by how much."""

    code: str
    label: str
    contribution: int
    detail: str
    # False where the factor rests on an assumption rather than on evidence in the
    # incident. Surfaced so a reviewer can see which parts of a score are soft.
    evidenced: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code, "label": self.label, "contribution": self.contribution,
            "detail": self.detail, "evidenced": self.evidenced,
        }


@dataclass(frozen=True)
class Assessment:
    """A level, a score, the factors behind it, and how much any of it is trusted."""

    level: str
    score: int
    confidence: str
    factors: tuple[Factor, ...]
    reason: str
    # Things the assessment could not establish. Never empty just because the score
    # came out low -- that is the distinction between "we checked" and "we don't know".
    gaps: tuple[str, ...] = field(default=())

    def as_dict(self) -> dict[str, Any]:
        return {
            "level": self.level, "score": self.score, "confidence": self.confidence,
            "reason": self.reason, "factors": [f.as_dict() for f in self.factors],
            "gaps": list(self.gaps),
        }


def _band(score: int) -> str:
    if score >= BAND_HIGH:
        return incident.SEV_CRITICAL
    if score >= BAND_MEDIUM:
        return incident.SEV_HIGH
    if score >= BAND_LOW:
        return incident.SEV_MEDIUM
    return incident.SEV_LOW


def _confidence_from(factors: tuple[Factor, ...], gaps: tuple[str, ...]) -> str:
    """How much the assessment itself should be trusted.

    Driven by how much of the score rests on evidence rather than assumption, and by
    how many questions are still open. Never returns CONFIRMED: an engine does not get
    to be certain, only a human does (§12).
    """
    if not factors:
        return incident.UNKNOWN
    evidenced = sum(f.contribution for f in factors if f.evidenced)
    total = sum(f.contribution for f in factors) or 1
    share = evidenced / total
    if gaps and share < 0.5:
        return incident.UNKNOWN
    if share >= 0.8 and not gaps:
        return incident.PROBABLE
    if share >= 0.5:
        return incident.POSSIBLE
    return incident.UNKNOWN


# ── Severity: "how bad is this?", asked at intake ───────────────────────────────

def assess_severity(
    *,
    incident_type: str,
    reported_severity: str | None = None,
    evidence_count: int = 0,
    has_external_evidence: bool = False,
    affected_system_count: int = 0,
    personal_data_involved: str = incident.UNKNOWN,
) -> Assessment:
    """A first read, from what is known at intake.

    Deliberately conservative about the unknown: an incident with no evidence yet is
    not low severity, it is an incident nobody has looked at. That distinction is in
    the confidence and the gaps, not hidden inside the score.
    """
    factors: list[Factor] = []
    gaps: list[str] = []

    weight = _TYPE_WEIGHT.get(incident_type, 8)
    factors.append(Factor(
        code="TYPE", label=f"Incident type: {incident_type}", contribution=weight,
        detail=f"{incident_type} carries a base weight of {weight}",
        evidenced=incident_type != incident.TYPE_UNCLASSIFIED,
    ))
    if incident_type == incident.TYPE_UNCLASSIFIED:
        gaps.append("the incident has not been classified yet")

    if incident.at_least(personal_data_involved, incident.PROBABLE):
        factors.append(Factor(
            code="PERSONAL_DATA", label="Personal data involvement", contribution=20,
            detail=f"personal data involvement is {personal_data_involved}",
        ))
    elif personal_data_involved == incident.POSSIBLE:
        factors.append(Factor(
            code="PERSONAL_DATA_POSSIBLE", label="Possible personal data", contribution=10,
            detail="personal data involvement is possible but unconfirmed",
            evidenced=False,
        ))
    else:
        gaps.append("whether personal data was involved is not yet established")

    if affected_system_count > 1:
        contribution = min(5 * affected_system_count, 20)
        factors.append(Factor(
            code="MULTI_SYSTEM", label="Multiple systems affected", contribution=contribution,
            detail=f"{affected_system_count} systems identified as affected",
        ))
    elif affected_system_count == 0:
        gaps.append("no affected system has been identified yet")

    if has_external_evidence:
        factors.append(Factor(
            code="EXTERNAL_EVIDENCE", label="Corroborated by external evidence",
            contribution=8,
            detail="at least one piece of evidence describes something that happened, "
                   "rather than being derived from Consiva's own metadata",
        ))
    elif evidence_count:
        factors.append(Factor(
            code="DERIVED_ONLY", label="Only derived evidence", contribution=0,
            detail="every piece of evidence so far is Consiva's own metadata; nothing "
                   "external corroborates the incident",
            evidenced=False,
        ))
        gaps.append("no external evidence has been attached")
    else:
        gaps.append("no evidence has been attached")

    # A reporter's own severity is a signal, never the answer -- but if they said
    # critical and the engine did not, that disagreement must be visible rather than
    # silently overridden.
    score = sum(f.contribution for f in factors)
    level = _band(score)
    if reported_severity and incident.SEVERITY_ORDER.get(reported_severity, 0) > incident.SEVERITY_ORDER[level]:
        gaps.append(
            f"the reporter assessed this as {reported_severity}, higher than the "
            f"engine's {level}; a reviewer should reconcile the difference"
        )

    return Assessment(
        level=level, score=score,
        confidence=_confidence_from(tuple(factors), tuple(gaps)),
        factors=tuple(factors),
        reason=_severity_reason(level, score, gaps),
        gaps=tuple(gaps),
    )


def _severity_reason(level: str, score: int, gaps: list[str]) -> str:
    base = f"Assessed {level} ({score}) from the factors listed."
    if gaps:
        return base + (
            f" {len(gaps)} question(s) remain open, so this is a provisional reading "
            "rather than a settled one."
        )
    return base


# ── Risk: "what is the risk to the people involved?", asked after impact ────────

def assess_risk(
    *,
    incident_type: str,
    data_categories: tuple[str, ...] = (),
    subject_total: int | None = None,
    count_basis: str = "unknown",
    exfiltration: str = incident.UNKNOWN,
    unauthorized_access: str = incident.UNKNOWN,
    exposure_window: timedelta | None = None,
    third_party_involved: bool = False,
    personal_data_involved: str = incident.UNKNOWN,
    system_criticality: str | None = None,
) -> Assessment:
    """Risk to the individuals, once impact is understood.

    Takes only facts the incident has actually established. Anything it is not given
    becomes a recorded gap rather than a zero.
    """
    factors: list[Factor] = []
    gaps: list[str] = []

    factors.append(Factor(
        code="TYPE", label=f"Incident type: {incident_type}",
        contribution=_TYPE_WEIGHT.get(incident_type, 8),
        detail=f"base weight for {incident_type}",
        evidenced=incident_type != incident.TYPE_UNCLASSIFIED,
    ))

    # Data sensitivity: the single most important input. Only the highest-weighted
    # category counts fully, with a smaller allowance for breadth -- ten contact fields
    # are not more harmful than one set of credentials.
    if data_categories:
        weights = sorted(
            (_CATEGORY_WEIGHT.get(c, _UNKNOWN_CATEGORY_WEIGHT) for c in data_categories),
            reverse=True,
        )
        top = weights[0]
        breadth = min(len(weights) - 1, 4) * 3
        factors.append(Factor(
            code="DATA_SENSITIVITY", label="Most sensitive category involved",
            contribution=top,
            detail=f"highest-weighted category of {len(data_categories)}: "
                   f"{max(data_categories, key=lambda c: _CATEGORY_WEIGHT.get(c, _UNKNOWN_CATEGORY_WEIGHT))}",
        ))
        if breadth:
            factors.append(Factor(
                code="DATA_BREADTH", label="Multiple data categories", contribution=breadth,
                detail=f"{len(data_categories)} distinct categories may be involved",
            ))
    else:
        gaps.append("no affected data category has been established")

    # Scale. An unknown count contributes nothing to the score but is recorded as a
    # gap, because a large unknown is not a small known.
    if subject_total is None:
        gaps.append("the number of affected individuals is not known")
    else:
        if subject_total >= 100_000:
            contribution = 25
        elif subject_total >= 10_000:
            contribution = 20
        elif subject_total >= 1_000:
            contribution = 14
        elif subject_total >= 100:
            contribution = 8
        else:
            contribution = 4
        factors.append(Factor(
            code="SCALE", label="Number of individuals", contribution=contribution,
            detail=f"{subject_total:,} individuals ({count_basis})",
            # An estimate is a softer input than a count.
            evidenced=count_basis == "counted",
        ))
        if count_basis == "estimated":
            gaps.append("the affected-individual figure is an estimate, not a count")
        elif count_basis == "unknown":
            gaps.append("how the affected-individual figure was arrived at is unrecorded")

    if incident.at_least(exfiltration, incident.PROBABLE):
        factors.append(Factor(
            code="EXFILTRATION", label="Data left the organisation", contribution=25,
            detail=f"exfiltration is {exfiltration}",
        ))
    elif exfiltration == incident.POSSIBLE:
        factors.append(Factor(
            code="EXFILTRATION_POSSIBLE", label="Possible exfiltration", contribution=12,
            detail="exfiltration is possible but unconfirmed", evidenced=False,
        ))
    else:
        gaps.append("whether data left the organisation is not established")

    if incident.at_least(unauthorized_access, incident.PROBABLE):
        factors.append(Factor(
            code="UNAUTHORIZED_ACCESS", label="Unauthorised access", contribution=12,
            detail=f"unauthorised access is {unauthorized_access}",
        ))

    if exposure_window is not None:
        days = max(exposure_window.total_seconds() / 86400, 0)
        contribution = 4 if days < 1 else 8 if days < 7 else 14 if days < 30 else 18
        factors.append(Factor(
            code="EXPOSURE_WINDOW", label="Exposure duration", contribution=contribution,
            detail=f"approximately {days:.1f} day(s) of exposure",
        ))
    else:
        gaps.append("the exposure window is not established")

    if third_party_involved:
        factors.append(Factor(
            code="THIRD_PARTY", label="Third party involved", contribution=8,
            detail="a vendor or processor is implicated, so remediation is not "
                   "entirely within the organisation's control",
        ))

    if system_criticality in ("high", "critical"):
        factors.append(Factor(
            code="SYSTEM_CRITICALITY", label="Business-critical system",
            contribution=10, detail=f"affected system criticality: {system_criticality}",
        ))

    if personal_data_involved == incident.UNKNOWN and not data_categories:
        gaps.append("whether personal data was involved at all is still unknown")

    score = sum(f.contribution for f in factors)
    level = _band(score)
    confidence = _confidence_from(tuple(factors), tuple(gaps))

    return Assessment(
        level=level, score=score, confidence=confidence, factors=tuple(factors),
        reason=_risk_reason(level, score, confidence, gaps),
        gaps=tuple(gaps),
    )


def _risk_reason(level: str, score: int, confidence: str, gaps: list[str]) -> str:
    reason = (
        f"Risk assessed {level} (score {score}) from {'evidence' if not gaps else 'partial evidence'}, "
        f"with {confidence} confidence."
    )
    if gaps:
        reason += (
            f" {len(gaps)} input(s) could not be established; the score reflects what is "
            "known and should not be read as a complete picture."
        )
    return reason


def is_assessable(data_categories: tuple[str, ...], subject_total: int | None,
                  exfiltration: str) -> bool:
    """Whether enough is known for a risk assessment to mean anything.

    Not a hard gate -- a provisional assessment on thin evidence is useful, and the
    gaps make its thinness visible. This exists so a caller can tell a reviewer "there
    is not enough here yet" rather than presenting a confident-looking low score.
    """
    return bool(data_categories) or subject_total is not None or exfiltration != incident.UNKNOWN
