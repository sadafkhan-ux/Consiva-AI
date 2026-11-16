"""The Enterprise DPDP scoring engine: parameter assessments in, a reproducible score out.

A pure function of its inputs. It does not read a database, a model or a website, and it imports nothing from the 5-page agent's scoring (app.scoring). What decided each parameter's
state (the evidence, the rules, a reviewer) is recorded beside the state; this module only turns states into points, and says how.

The method, as approved by the owner on 13 November 2026:
  * 100 points are spread over 33 scored parameters (catalogue.py). Six more parameters are shown and earn or lose nothing.
  * Met earns the parameter's points, Partly met half of them (only where the parameter allows a partial), Not met none.
  * A scored parameter that was not assessed, does not apply, awaits a person's review, or has no evidence earns NOTHING: its points are lost, never left out of the calculation and never given in full
    (owner's decision, 16 Nov 2026). The six parameters a website cannot show (Needs records, statement-only, human decision) are worth no points and change nothing.
  * score = the points earned, out of 100, rounded half up. Coverage is the points assessed out of 100; it is reported beside the score and never withholds it (owner's decision, 16 Nov 2026). No score only when nothing is in play.
  * The supporting parameters are worth 8 of the 100 points (under the 10% limit), so no scaling is needed.
  * A result that rests on evidence needs at least one evidence reference. A model's low-confidence reading goes to a person; it never earns or loses points.
  * Before 13 May 2027 most duties are not in force: the score is then labelled readiness, never a finding of breach.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from app.enterprise.scoring.catalogue import (
    BY_ID, CATALOGUE_VERSION, GROUP_LABELS, PARAMETERS, STATUTORY, SUPPORTING, TOTAL_POINTS, Parameter,
)

SCORE_VERSION = f"ent-dpdp-{CATALOGUE_VERSION}"
SCORE_NAME = "Website-observable DPDP readiness score"
SUPPORTING_SHARE_CAP = Decimal("0.10")

MET, PARTLY, NOT_MET = "met", "partly_met", "not_met"
NOT_ASSESSED, NOT_APPLICABLE, NEEDS_RECORDS, HUMAN_REVIEW = "not_assessed", "not_applicable", "needs_records", "human_review"
STATES = (MET, PARTLY, NOT_MET, NOT_ASSESSED, NOT_APPLICABLE, NEEDS_RECORDS, HUMAN_REVIEW)
IN_PLAY = (MET, PARTLY, NOT_MET)
CREDIT = {MET: Decimal("1"), PARTLY: Decimal("0.5"), NOT_MET: Decimal("0")}


class ScoringError(ValueError):
    """The input breaks a rule of the method. It is refused, never repaired quietly."""


@dataclass(frozen=True)
class Assessment:
    """One parameter's result. `evidence_refs` names the stored evidence (or, for an absence, the pages that were searched). `confidence` is the model's own, where a model judged it."""
    parameter_id: str
    state: str
    evidence_refs: tuple[str, ...] = ()
    confidence: str | None = None            # "high" | "medium" | "low"
    note: str = ""
    reviewed_by: str | None = None
    review_reason: str | None = None


def apply_review(a: Assessment, *, reviewer: str, state: str, reason: str) -> Assessment:
    """A person's decision replaces the engine's: recorded with who and why, and not downgraded for the model's confidence."""
    if not reviewer.strip() or not reason.strip():
        raise ScoringError("a review names the reviewer and the reason")
    return replace(a, state=state, reviewed_by=reviewer.strip(), review_reason=reason.strip(), confidence=None)


@dataclass
class Line:
    parameter_id: str
    name: str
    group: str
    kind: str
    state: str
    points: int
    credit: Decimal
    earned: Decimal
    in_play: Decimal
    evidence_refs: tuple[str, ...]
    in_force: date | None
    in_force_on_date: bool | None
    adjustment: str | None = None

    def calculation(self) -> str:
        if self.in_play == 0:
            return f"{self.parameter_id}: {self.state.replace('_', ' ')}; not in the calculation"
        return f"{self.parameter_id}: {self.state.replace('_', ' ')}, credit {self.credit} x {self.points} points = {self.earned}"


@dataclass
class ScoreResult:
    name: str
    version: str
    catalogue_fingerprint: str
    as_of: date
    lines: list[Line]
    points_earned: Decimal
    points_in_play: Decimal
    coverage: int
    score: int | None
    reason_no_score: str | None
    basis: str                                   # "readiness" while any parameter in play is not yet in force, else "assessment"
    not_yet_in_force: list[str]
    supporting_scale: Decimal | None
    by_group: dict[str, dict] = field(default_factory=dict)
    needs_attention: dict[str, list[str]] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "name": self.name, "version": self.version, "catalogue_fingerprint": self.catalogue_fingerprint, "as_of": self.as_of.isoformat(), "score": self.score,
            "reason_no_score": self.reason_no_score, "basis": self.basis, "points_earned": float(self.points_earned), "points_in_play": float(self.points_in_play),
            "points_available": TOTAL_POINTS, "coverage": self.coverage, "not_yet_in_force": self.not_yet_in_force,
            "supporting_scale": None if self.supporting_scale is None else float(self.supporting_scale), "by_group": self.by_group, "needs_attention": self.needs_attention,
            "lines": [{"parameter_id": x.parameter_id, "state": x.state, "points": x.points, "earned": float(x.earned), "in_play": float(x.in_play),
                       "evidence_refs": list(x.evidence_refs), "adjustment": x.adjustment, "calculation": x.calculation()} for x in self.lines],
        }


def catalogue_fingerprint() -> str:
    """A fingerprint of the catalogue the score was worked out under, so a stored score can be tied to the exact parameters and points."""
    text = "|".join(f"{p.id}:{p.kind}:{p.points}:{int(p.partial_allowed)}:{int(p.conditional)}:{p.in_force}" for p in PARAMETERS)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _half_up(x: Decimal) -> int:
    return int(x.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _check(a: Assessment, p: Parameter) -> None:
    if a.state not in STATES:
        raise ScoringError(f"{a.parameter_id}: unknown state {a.state!r}")
    if a.state == PARTLY and not p.partial_allowed:
        raise ScoringError(f"{a.parameter_id}: this parameter has no partial rule, so 'partly met' is not a valid result")
    if a.state in IN_PLAY and not a.evidence_refs and not a.reviewed_by:
        raise ScoringError(f"{a.parameter_id}: a result that earns or loses points needs evidence (or a reviewer's decision); none was given")


def score(assessments: list[Assessment], *, as_of: date) -> ScoreResult:
    seen: set[str] = set()
    for a in assessments:
        if a.parameter_id not in BY_ID:
            raise ScoringError(f"unknown parameter {a.parameter_id!r}")
        if a.parameter_id in seen:
            raise ScoringError(f"{a.parameter_id} was assessed twice; one parameter has one result, so the same evidence cannot count twice")
        seen.add(a.parameter_id)
    given = {a.parameter_id: a for a in assessments}

    lines: list[Line] = []
    for p in PARAMETERS:
        a = given.get(p.id) or Assessment(p.id, NOT_ASSESSED, note="no assessment was supplied")
        _check(a, p)
        state, adjustment = a.state, None
        if state in IN_PLAY and a.confidence == "low" and not a.reviewed_by:
            state, adjustment = HUMAN_REVIEW, "a low-confidence reading goes to a person and earns or loses nothing"
        if state in IN_PLAY and not p.scored:
            adjustment = "shown as found, but a website cannot show this fully: it earns or loses no points"
        credit = CREDIT.get(state, Decimal("0"))
        in_play = Decimal(p.points) if (state in IN_PLAY and p.scored) else Decimal("0")
        lines.append(Line(
            parameter_id=p.id, name=p.name, group=p.group, kind=p.kind, state=state, points=p.points, credit=credit, earned=credit * in_play, in_play=in_play,
            evidence_refs=tuple(dict.fromkeys(a.evidence_refs)), in_force=p.in_force, in_force_on_date=None if p.in_force is None else p.in_force <= as_of, adjustment=adjustment))

    scale: Decimal | None = None          # no scaling: the 100 points are fixed, and the supporting parameters are worth 8 of them

    earned = sum((x.earned for x in lines), Decimal("0"))
    in_play = sum((x.in_play for x in lines), Decimal("0"))
    coverage = _half_up(in_play * 100 / TOTAL_POINTS)
    value, why = None, None
    if in_play == 0:
        why = "no scored parameter could be assessed"
    else:
        value = _half_up(earned * 100 / TOTAL_POINTS)          # the points earned out of the 100 the parameters are worth

    pending = sorted(x.parameter_id for x in lines if x.in_play > 0 and x.in_force_on_date is False)
    by_group: dict[str, dict] = {}
    for g, label in GROUP_LABELS.items():
        gl = [x for x in lines if x.group == g]
        by_group[g] = {"label": label, "points_available": sum(x.points for x in gl), "points_in_play": float(sum(x.in_play for x in gl)), "points_earned": float(sum(x.earned for x in gl))}
    attention = {
        "not_met": [x.parameter_id for x in lines if x.state == NOT_MET], "partly_met": [x.parameter_id for x in lines if x.state == PARTLY],
        "not_assessed": [x.parameter_id for x in lines if x.state == NOT_ASSESSED], "human_review": [x.parameter_id for x in lines if x.state == HUMAN_REVIEW],
        "needs_records": [x.parameter_id for x in lines if x.state == NEEDS_RECORDS],
    }
    return ScoreResult(
        name=SCORE_NAME, version=SCORE_VERSION, catalogue_fingerprint=catalogue_fingerprint(), as_of=as_of, lines=lines, points_earned=earned, points_in_play=in_play,
        coverage=coverage, score=value, reason_no_score=why, basis="readiness" if pending else "assessment", not_yet_in_force=pending,
        supporting_scale=scale, by_group=by_group, needs_attention=attention)


def legal_exposure(result: ScoreResult) -> list[dict]:
    """For each statutory parameter the site did not fully meet, the most the Act's Schedule lets the Board impose for that kind of breach. A ceiling set by law, kept apart from the
    score, and never a prediction. Supporting signals are technical observations and have no entry."""
    out = []
    for x in result.lines:
        p = BY_ID[x.parameter_id]
        if x.state in (NOT_MET, PARTLY) and p.kind == STATUTORY and p.penalty_ceiling_cr is not None:
            out.append({"parameter_id": p.id, "state": x.state, "schedule_item": p.schedule_item, "maximum_penalty_crore": p.penalty_ceiling_cr, "legal_refs": list(p.legal_refs)})
    return sorted(out, key=lambda e: (-e["maximum_penalty_crore"], e["parameter_id"]))
