"""Method 3.0.0 scoring: one result per parameter in, a reproducible score out. A pure function of its inputs.

The rules (approved by the owner on 20 November 2026):
  * Pass earns 100% of a parameter's marks, Partial 50% (only where the catalogue writes a Partial criterion), Fail 0.
  * Not applicable (a duty the organisation does not have, or a check with nothing on the site to apply to) is REMOVED FROM THE MAXIMUM, with the reason. It is never a Pass or a Fail.
  * Information (E01) earns and loses nothing.
  * There is no "unable to verify": every applicable parameter ends Pass, Partial or Fail. A duty internal to the organisation is judged from what the published policy says, and
    says so ("internal records not inspected").
  * score = marks earned / applicable maximum x 100, rounded to one decimal place (half up). Categories use the same formula within the category.
  * Before 13 May 2027 most duties are not in force: the score is then labelled readiness, never a finding of breach.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from app.enterprise.method3.catalogue import BY_ID, CATALOGUE_VERSION, CATS, CONDITIONAL, INFO, LEGAL_REVIEW_STATUS, METHOD_NAME, PARAMS

PASS, PARTIAL, FAIL, NA, INFORMATION = "pass", "partial", "fail", "not_applicable", "information"
STATUSES = (PASS, PARTIAL, FAIL, NA, INFORMATION)
LABEL = {PASS: "Pass", PARTIAL: "Partial", FAIL: "Fail", NA: "Not applicable", INFORMATION: "Information"}
CREDIT = {PASS: Decimal("1"), PARTIAL: Decimal("0.5"), FAIL: Decimal("0")}
VERSION = f"ent-dpdp-{CATALOGUE_VERSION}"


class MethodError(ValueError):
    """The input breaks a rule of the method. It is refused, never repaired quietly."""


@dataclass
class UrlCheck:
    url: str
    status: str            # pass | partial | fail
    note: str = ""


@dataclass
class Result:
    """One parameter's result, with everything a reader needs to check it: the evidence quoted or observed, the stored evidence ids, the URLs it was assessed on, and why."""
    parameter_id: str
    status: str
    explanation: str
    evidence: list[str] = field(default_factory=list)          # quoted passages and observations, each naming its URL
    evidence_refs: list[str] = field(default_factory=list)     # "ev:<id>" of the stored evidence rows
    basis: str = ""                                            # how it was decided: observed URL by URL, read from the policy, consent test, ...
    urls: list[UrlCheck] = field(default_factory=list)
    review: bool = False                                       # a person should confirm it
    limitation: str | None = None                              # what the evidence cannot show (records not inspected, a page off the scanned host, ...)
    differs: str | None = None                                 # where it differs from the 51-parameter result for the same scan, and why

    def to_dict(self) -> dict:
        d = asdict(self)
        d["urls"] = [asdict(u) for u in self.urls]
        return d


def _round1(x: Decimal) -> float:
    return float(x.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def fingerprint() -> str:
    text = "|".join(f"{p.id}:{p.kind}:{p.weight}:{int(p.partial_allowed)}" for p in PARAMS)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def check(results: list[Result]) -> dict[str, Result]:
    by: dict[str, Result] = {}
    for r in results:
        p = BY_ID.get(r.parameter_id)
        if p is None:
            raise MethodError(f"unknown parameter {r.parameter_id!r}")
        if r.parameter_id in by:
            raise MethodError(f"{r.parameter_id} has two results; one parameter has one result")
        if r.status not in STATUSES:
            raise MethodError(f"{r.parameter_id}: unknown status {r.status!r}")
        if p.kind == INFO and r.status != INFORMATION:
            raise MethodError(f"{r.parameter_id} is shown for information only")
        if p.kind != INFO and r.status == INFORMATION:
            raise MethodError(f"{r.parameter_id}: only E01 is information")
        if r.status == PARTIAL and not p.partial_allowed:
            raise MethodError(f"{r.parameter_id}: the catalogue writes no Partial criterion, so Partial is not a valid result")
        if r.status == NA and not r.explanation.strip():
            raise MethodError(f"{r.parameter_id}: Not applicable needs the reason")
        if r.status in CREDIT and not (r.evidence or r.urls or r.explanation.strip()):
            raise MethodError(f"{r.parameter_id}: a result that earns or loses marks needs its evidence or reason")
        by[r.parameter_id] = r
    missing = [p.id for p in PARAMS if p.id not in by]
    if missing:
        raise MethodError(f"no result for {', '.join(missing)}")
    return by


def score(results: list[Result], *, as_of: date) -> dict:
    """The score and everything behind it, as a plain dict (stored as it is and returned by the API)."""
    by = check(results)
    lines, earned, app_max, na_max = [], Decimal("0"), Decimal("0"), Decimal("0")
    for p in PARAMS:
        r = by[p.id]
        mx = Decimal(p.weight) / 4
        e = (CREDIT[r.status] * mx) if r.status in CREDIT else None
        if r.status in CREDIT:
            earned += e
            app_max += mx
        elif r.status == NA:
            na_max += mx
        lines.append({"parameter_id": p.id, "category": p.cat, "name": p.name, "kind": p.kind, "act": p.act, "rule": p.rule, "weight": p.weight, "max": float(mx),
                      "earned": None if e is None else float(e), "status": r.status, "status_label": LABEL[r.status],
                      "in_force": None if p.in_force is None else p.in_force.isoformat(), **{k: v for k, v in r.to_dict().items() if k != "parameter_id" and k != "status"}})
    value = _round1(earned * 100 / app_max) if app_max else None
    cats = {}
    for k, label in CATS.items():
        ls = [x for x in lines if x["category"] == k]
        a = [x for x in ls if x["status"] in CREDIT]
        m = sum((Decimal(str(x["max"])) for x in a), Decimal("0"))
        got = sum((Decimal(str(x["earned"])) for x in a), Decimal("0"))
        cats[k] = {"label": label, "parameters": len(ls), "applicable": len(a), "max": float(m), "max_all": float(sum((Decimal(str(x["max"])) for x in ls), Decimal("0"))),
                   "earned": float(got), "pct": _round1(got * 100 / m) if m else None, "counts": dict(Counter(x["status"] for x in ls))}
    pending = [x["parameter_id"] for x in lines if x["status"] in CREDIT and x["in_force"] and date.fromisoformat(x["in_force"]) > as_of]
    return {
        "name": METHOD_NAME, "version": VERSION, "catalogue_version": CATALOGUE_VERSION, "catalogue_fingerprint": fingerprint(), "legal_review_status": LEGAL_REVIEW_STATUS,
        "as_of": as_of.isoformat(), "score": value, "marks_earned": float(earned), "applicable_max": float(app_max), "not_applicable_max": float(na_max), "max_total": 100.0,
        "basis": "readiness" if pending else "assessment", "not_yet_in_force": pending,
        "counts": dict(Counter(x["status"] for x in lines)), "parameters": len(lines), "scored_parameters": sum(1 for p in PARAMS if p.weight),
        "review": [x["parameter_id"] for x in lines if x["review"]], "categories": cats, "lines": lines,
        "conditional_not_applicable": [x["parameter_id"] for x in lines if x["status"] == NA and BY_ID[x["parameter_id"]].kind == CONDITIONAL],
    }
