"""Why were points deducted: the readiness score taken apart, check by check.

The score is `points earned / points assessed x 100`. A check that earns less than its weight (Not Met earns none of it, Partially Met half) leaves that
many points unearned, and each unearned point lowers the score by `100 / points assessed` percentage points. So the score is exactly 100 minus the
sum of every check's deduction. Nothing here estimates or re-scores: every figure is read from the same results the score was computed from
(`RequirementResult.weight`, `.points_earned`, `.status`) and the arithmetic is exact (fractions) until it is rounded for display.

Checks that cost nothing are listed too, with the reason: Unable to Verify and Not Applicable checks are left out of the score (never deducted, never
passed), and a duplicate gap is counted once under the check that carries it.
"""

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction
from urllib.parse import urlsplit

STATE_LABEL = {"pre_consent": "Before Consent", "post_accept": "After Accept", "post_reject": "After Reject"}
_OUTCOME = {"not_met": "Not Met", "partially_met": "Partially Met"}
_STATUS_LABEL = {"unable_to_verify": "Unable to Verify", "not_applicable": "Not Applicable"}
# What the status earns, in words, from the rule the score applies (scoring/dpdp_score.py ASSESSED_STATUSES).
_EARNS = {"not_met": "Not Met earns none of the check's weight", "partially_met": "Partially Met earns half of the check's weight"}


@dataclass
class DeductionEvidence:
    kind: str                 # "observation" (the scan's own sentence), "request" (a tracker or network request) or "cookie"
    text: str
    url: str | None = None    # the page it was seen on
    vendor: str | None = None
    category: str | None = None
    consent_states: list[str] = field(default_factory=list)


@dataclass
class DeductionStep:
    requirement_id: str
    title: str
    category: str
    outcome: str                      # "Not Met" or "Partially Met"
    weight: int
    points_earned: float
    points_lost: float
    percent_lost: float               # percentage points this check took off the score
    score_before: float               # the running score, in the order the steps are listed
    score_after: float
    calculation: str                  # "Weight 5, earns 0, loses 5. 5 ÷ 25 × 100 = 20.0 points off the score."
    why: str                          # the rule that gave this outcome, then what was observed
    in_force: bool
    effective: str
    evidence: list[DeductionEvidence] = field(default_factory=list)
    pages: list[str] = field(default_factory=list)
    consent_states: list[str] = field(default_factory=list)


@dataclass
class NotDeducted:
    requirement_id: str
    title: str
    status: str                       # "Unable to Verify" or "Not Applicable"
    weight: int
    reason: str


@dataclass
class Deductions:
    status: str                       # "scored" or "insufficient_evidence"
    score_percent: float | None
    points_earned: float
    points_assessed: int
    points_lost: float
    percent_lost: float | None        # 100 - score
    steps: list[DeductionStep]
    not_deducted: list[NotDeducted]
    summary: str
    formula: str
    #: True when the deductions as shown (each rounded to one decimal) add up to the whole drop; otherwise `rounding_note` says by how much.
    rounded_steps_add_up: bool = True
    rounding_note: str | None = None
    note: str = ""


def _n(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else f"{x:.1f}"


def _round1(q: Fraction) -> float:
    return float((Decimal(q.numerator) / Decimal(q.denominator)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def _host(url: str | None) -> str:
    return (urlsplit(url).hostname or url or "") if url else ""


def _evidence_for(res, rows_by_id: dict[str, dict]) -> tuple[list[DeductionEvidence], list[str], list[str]]:
    """The scan's own sentences first, then every tracker, request or cookie the check cites, with the page it was seen on."""
    out: list[DeductionEvidence] = [DeductionEvidence(kind="observation", text=t) for t in res.observations]
    pages: list[str] = []
    states: list[str] = []

    def add_state(codes) -> list[str]:
        labels = [STATE_LABEL[s] for s in codes if s in STATE_LABEL]
        for label in labels:
            if label not in states:
                states.append(label)
        return labels

    for i in dict.fromkeys(res.evidence_ids):
        row = rows_by_id.get(i)
        if not row:
            continue
        if row.get("name"):   # a cookie
            seen = [o.get("page_url") for o in (row.get("observations") or []) if o.get("page_url")]
            url = seen[0] if seen else row.get("page_url")
            text = f"Cookie {row['name']} set by {row.get('domain') or 'an unrecorded domain'}"
            kind = "cookie"
        else:                 # a tracker or request
            url = row.get("page_url")
            target = row.get("script_src") or "an unrecorded address"
            text = f"Request to {target}"
            kind = "request"
        labels = add_state(row.get("consent_states") or [])
        out.append(DeductionEvidence(kind=kind, text=text, url=url, vendor=row.get("vendor"), category=row.get("category"), consent_states=labels))
        for u in ([*seen] if kind == "cookie" else [url]):
            if u and u not in pages:
                pages.append(u)
    for u in res.pages:
        if u not in pages:
            pages.append(u)
    return out, pages, states


def build_deductions(results: list, block, rows: list[dict]) -> Deductions:
    """`results` are the RequirementResults the score was computed from, `block` the readiness ScoreBlock, `rows` the scan's tracker and cookie records."""
    rows_by_id = {r["id"]: r for r in rows if r.get("id")}
    assessed = [r for r in results if r.counts_in_score]
    lost_total = sum((Fraction(r.weight) - Fraction(r.points_earned).limit_denominator(2) for r in assessed), Fraction(0))
    not_deducted = [
        NotDeducted(
            requirement_id=r.id, title=r.title, status=_STATUS_LABEL[r.status], weight=r.weight,
            reason=(f"Left out of the score and counted against coverage. It is not treated as met and no points are taken off. "
                    f"{r.sufficiency_reason or r.limitation}".strip()
                    if r.status == "unable_to_verify" else
                    (r.exclusion_reason or "Does not apply to this site.") + " No points are taken off."),
        )
        for r in results if r.status in _STATUS_LABEL
    ]

    if block.status != "scored" or not block.points_assessed:
        return Deductions(
            status=block.status, score_percent=None, points_earned=block.points_earned, points_assessed=block.points_assessed,
            points_lost=float(lost_total), percent_lost=None, steps=[], not_deducted=not_deducted,
            summary=f"No score is shown, so no points are deducted. {block.insufficient_reason or ''}".strip(),
            formula="A score needs enough assessed checks and enough coverage; this scan did not reach that.",
            note="A score that is not shown is not a score of zero.",
        )

    den = Fraction(block.points_assessed)
    losing = [r for r in assessed if Fraction(r.points_earned).limit_denominator(2) < r.weight]
    losing.sort(key=lambda r: (-(Fraction(r.weight) - Fraction(r.points_earned).limit_denominator(2)), r.id))

    steps: list[DeductionStep] = []
    running = Fraction(100)
    for r in losing:
        loss = Fraction(r.weight) - Fraction(r.points_earned).limit_denominator(2)
        pct = loss / den * 100
        before, running = running, running - pct
        evidence, pages, states = _evidence_for(r, rows_by_id)
        steps.append(DeductionStep(
            requirement_id=r.id, title=r.title, category=r.category, outcome=_OUTCOME[r.status], weight=r.weight,
            points_earned=float(r.points_earned), points_lost=float(loss), percent_lost=_round1(pct),
            score_before=_round1(before), score_after=_round1(running),
            calculation=(f"Weight {r.weight}, earned {_n(r.points_earned)}, lost {_n(float(loss))}. "
                         f"{_n(float(loss))} ÷ {block.points_assessed} × 100 = {_round1(pct)} points off the score."),
            why=f"{_EARNS[r.status]} ({r.weight}). {r.criteria}".strip(),
            in_force=r.in_force, effective=r.effective.isoformat(), evidence=evidence, pages=pages, consent_states=states,
        ))

    score = _round1(Fraction(block.points_earned).limit_denominator(2) / den * 100)
    drop = _round1(Fraction(100) - Fraction(block.points_earned).limit_denominator(2) / den * 100)
    shown = round(sum(s.percent_lost for s in steps), 1)
    adds_up = abs(shown - drop) < 0.05
    if steps:
        summary = (f"The score is {score}%, so {drop} points were deducted from 100 across {len(steps)} "
                   f"check{'s' if len(steps) != 1 else ''}: {', '.join(f'{s.requirement_id} (−{s.percent_lost})' for s in steps)}.")
    else:
        summary = f"No points were deducted: every assessed check earned its full weight, so the score is {score}%."
    return Deductions(
        status="scored", score_percent=score, points_earned=block.points_earned, points_assessed=block.points_assessed,
        points_lost=float(lost_total), percent_lost=drop, steps=steps, not_deducted=not_deducted, summary=summary,
        formula=(f"Score = points earned ÷ points assessed × 100 = {_n(block.points_earned)} ÷ {block.points_assessed} × 100 = {score}%. "
                 f"Points assessed {block.points_assessed} − points lost {_n(float(lost_total))} = points earned {_n(block.points_earned)}. "
                 f"Each point lost takes {_round1(Fraction(100) / den) if block.points_assessed else 0} points off the score."),
        rounded_steps_add_up=adds_up,
        rounding_note=None if adds_up else (f"Each figure is rounded to one decimal, so the steps shown add up to {shown} while the exact total is {drop}. "
                                            f"The score and the total are exact."),
        note="Only checks the scan could assess are scored. Checks left out are listed separately and cost nothing.",
    )
