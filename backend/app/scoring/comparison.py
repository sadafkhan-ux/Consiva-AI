"""Why two scans of one website scored differently, check by check.

The DPDP score is computed on demand from a scan's stored evidence (scoring/dpdp_score.py `assess`); nothing about a past score is stored. So this does not
read an old number: it re-assesses BOTH scans from their own evidence with the SAME methodology, the same weights and the same assessment date, and then
explains the difference between the two results:

  * which requirements changed status, and the evidence each rested on;
  * whether the weights or the applicable / assessed denominators moved;
  * what the scan itself recorded differently (pages scanned, consent states exercised, the banner, a framed wrapper, the policies, what fired in each state);
  * how much of the score change each changed requirement accounts for;
  * why: a requirement that moved to or from Unable to Verify changed because the EVIDENCE changed (the scan could or could not exercise something); one
    that moved between Met, Partially Met and Not Met changed because what the website showed changed. A change with no changed input is reported as
    unexplained, never given a reason.

The contribution of each change is a sequential attribution: starting from the earlier result, each changed requirement is switched to its later state in
turn (heaviest first) and the score recomputed. The steps add up exactly to the total change; with several changes the split between them depends on
that order, which the result states.

Nothing here invents history. A score shown in an earlier report that was produced by an earlier version of the logic cannot be re-derived, and the result
says so (`methodology_note`).
"""

from __future__ import annotations

import dataclasses
from datetime import date
from typing import Any

from app.rules.consent_rules import build_consent_context
from app.scoring import dpdp_score

_STATUS_WORD = {"met": "Met", "partially_met": "Partially Met", "not_met": "Not Met", "unable_to_verify": "Unable to Verify", "not_applicable": "Not Applicable"}


def scan_inputs(evidence: dict) -> dict[str, Any]:
    """What the scan recorded that the checks read: its completeness and its observations, kept apart."""
    ctx = build_consent_context(evidence)

    def fired(state: str) -> int:
        return sum(1 for e in ctx.non_essential if state in e.get("consent_states", []))

    return {
        "completeness": {
            "pages_scanned": len(ctx.pages),
            "three_state_scan": ctx.three_pass,
            "consent_mechanism": ctx.mechanism_type,
            "banner_displayed_to_scanner": not ctx.banner_not_displayed,
            "accept_outcome": ctx.accept_outcome,
            "reject_outcome": ctx.reject_outcome,
            "reject_confirmed": ctx.reject_confirmed,
            "framed_wrapper": ctx.framed_sites[0] if ctx.framed_sites else None,
        },
        "observations": {
            "analytics_or_marketing_before_consent": fired("pre_consent"),
            "analytics_or_marketing_after_reject": fired("post_reject"),
            "analytics_or_marketing_total": len(ctx.non_essential),
            "unclassified_items": sum(1 for e in (*ctx.trackers, *ctx.cookies) if e.get("category") is None),
            "policies_found": sorted(ctx.policy_types),
        },
    }


def _changed_inputs(prev: dict, latest: dict) -> list[dict]:
    out = []
    for group in ("completeness", "observations"):
        for key, old in prev[group].items():
            new = latest[group][key]
            if old != new:
                out.append({"group": group, "name": key, "previous": old, "latest": new})
    return out


def _requirement_row(res: dpdp_score.RequirementResult | None) -> dict | None:
    if res is None:
        return None
    return {"status": res.status, "status_label": _STATUS_WORD[res.status], "points_earned": res.points_earned, "weight": res.weight,
            "counts_in_score": res.counts_in_score, "evidence": res.observations[:2], "evidence_ids": res.evidence_ids[:10]}


def _score(block: dpdp_score.ScoreBlock) -> dict:
    return {"score_percent": block.score_percent, "status": block.status, "coverage_percent": block.coverage_percent, "applicable_weight": block.applicable_weight,
            "assessed_weight": block.assessed_weight, "points_earned": block.points_earned, "insufficient_reason": block.insufficient_reason}


def _differs(a: dpdp_score.RequirementResult, b: dpdp_score.RequirementResult) -> bool:
    return (a.status, a.weight, a.counts_in_score, a.points_earned) != (b.status, b.weight, b.counts_in_score, b.points_earned)


def _cause(prev: dpdp_score.RequirementResult, latest: dpdp_score.RequirementResult, inputs: list[dict]) -> tuple[str, str]:
    """(cause, plain sentence). Evidence-completeness changes and website-behaviour changes are told apart by the status moved to or from."""
    if prev.weight != latest.weight and prev.status == latest.status:
        return "scoring_configuration", f"The weight of this check changed from {prev.weight} to {latest.weight}."
    moved_to_or_from_unknown = "unable_to_verify" in (prev.status, latest.status)
    completeness = [i for i in inputs if i["group"] == "completeness"]
    observed = [i for i in inputs if i["group"] == "observations"]
    if moved_to_or_from_unknown:
        if completeness:
            names = ", ".join(i["name"].replace("_", " ") for i in completeness)
            return "scan_coverage", f"The scan could {'not ' if latest.status == 'unable_to_verify' else ''}test this in the later scan. What the scans recorded differently: {names}."
        return "unexplained", "This check moved to or from Unable to Verify, but no recorded scan input differs. This is a data-quality question to investigate."
    if observed or completeness:
        names = ", ".join(i["name"].replace("_", " ") for i in (observed or completeness))
        kind = "website_behaviour" if observed else "scan_coverage"
        return kind, f"What the scans recorded differently: {names}."
    return "unexplained", "The status changed but no recorded scan input differs. This is a data-quality question to investigate."


def compare_scans(
    previous_evidence: dict, latest_evidence: dict, *, as_of: date, previous_info: dict, latest_info: dict,
    applicability: dpdp_score.Applicability | None = None, weights: dict[str, int] | None = None,
) -> dict:
    """`*_info`: {"scan_id", "scanned_at", "url"}. Both scans are assessed with the same `as_of`, weights and applicability."""
    prev = dpdp_score.assess(previous_evidence, as_of, applicability, weights, scan_url=previous_info.get("url"), scanned_at=previous_info.get("scanned_at"))
    latest = dpdp_score.assess(latest_evidence, as_of, applicability, weights, scan_url=latest_info.get("url"), scanned_at=latest_info.get("scanned_at"))
    prev_in, latest_in = scan_inputs(previous_evidence), scan_inputs(latest_evidence)
    inputs = _changed_inputs(prev_in, latest_in)

    prev_by = {r.id: r for r in prev.requirements}
    latest_by = {r.id: r for r in latest.requirements}
    changed_ids = [rid for rid in latest_by if rid in prev_by and _differs(prev_by[rid], latest_by[rid])]
    changed_ids.sort(key=lambda rid: (-max(prev_by[rid].weight, latest_by[rid].weight), rid))

    # Sequential attribution: switch each changed requirement from its earlier to its later state, heaviest first.
    working = [dataclasses.replace(r) for r in prev.requirements]
    index = {r.id: i for i, r in enumerate(working)}
    before = dpdp_score._block("readiness", working)
    steps: dict[str, dict] = {}
    for rid in changed_ids:
        working[index[rid]] = dataclasses.replace(latest_by[rid])
        after = dpdp_score._block("readiness", working)
        steps[rid] = {
            "score_before": before.score_percent, "score_after": after.score_percent,
            "score_change": round(after.score_percent - before.score_percent, 1) if before.score_percent is not None and after.score_percent is not None else None,
            "score_becomes_available": before.score_percent is None and after.score_percent is not None,
            "score_becomes_withheld": before.score_percent is not None and after.score_percent is None,
        }
        before = after
    attribution_matches = dpdp_score._block("readiness", working).score_percent == latest.readiness.score_percent

    changes = []
    for rid in changed_ids:
        cause, sentence = _cause(prev_by[rid], latest_by[rid], inputs)
        changes.append({
            "id": rid, "title": prev_by[rid].title, "category": prev_by[rid].category,
            "previous": _requirement_row(prev_by[rid]), "latest": _requirement_row(latest_by[rid]),
            "cause": cause, "explanation": sentence, **steps[rid],
        })
    only_in_one = sorted(set(prev_by) ^ set(latest_by))

    p, n = _score(prev.readiness), _score(latest.readiness)
    delta = round(n["score_percent"] - p["score_percent"], 1) if p["score_percent"] is not None and n["score_percent"] is not None else None
    denominators_changed = (p["applicable_weight"], p["assessed_weight"]) != (n["applicable_weight"], n["assessed_weight"])
    causes = sorted({c["cause"] for c in changes})
    cov_change = round(n["coverage_percent"] - p["coverage_percent"], 1) if p["coverage_percent"] is not None and n["coverage_percent"] is not None else None
    if p["score_percent"] is None and n["score_percent"] is None:
        headline = "Neither scan has a score" + (f" ({n['insufficient_reason']})" if n["insufficient_reason"] else "")
    elif p["score_percent"] is None:
        headline = f"The earlier scan had no score; this one scores {n['score_percent']}%"
    elif n["score_percent"] is None:
        headline = f"The earlier scan scored {p['score_percent']}%; this one has no score ({n['insufficient_reason']})"
    elif delta == 0:
        headline = f"The score is unchanged at {n['score_percent']}%"
    else:
        headline = f"The score went from {p['score_percent']}% to {n['score_percent']}% ({delta:+g} points)"
    if cov_change not in (None, 0.0):
        headline += f"; assessment coverage went from {p['coverage_percent']}% to {n['coverage_percent']}%"
    if not changes and p["score_percent"] == n["score_percent"] and p["status"] == n["status"] and cov_change in (None, 0.0):
        summary = "The two scans give the same result: no requirement changed."
    elif not changes:
        summary = headline + ", but no requirement changed. This is a data-quality problem to investigate."
        causes = ["unexplained"]
    else:
        parts = {"scan_coverage": "the later scan could test, or could not test, something the earlier one could not (scan coverage)",
                 "website_behaviour": "what the website did differed between the scans",
                 "scoring_configuration": "the weight of a check differs",
                 "unexplained": "a change with no recorded cause"}
        summary = headline + ". Why: " + "; and ".join(parts[c] for c in causes) + "."
    return {
        "previous": {**previous_info, **p, "inputs": prev_in},
        "latest": {**latest_info, **n, "inputs": latest_in},
        "score_change": delta,
        "summary": summary,
        "causes": causes,
        "changes": changes,
        "changed_inputs": inputs,
        "denominators": {"previous": {"applicable_weight": p["applicable_weight"], "assessed_weight": p["assessed_weight"]},
                         "latest": {"applicable_weight": n["applicable_weight"], "assessed_weight": n["assessed_weight"]}, "changed": denominators_changed},
        "requirements_missing_from_one_scan": only_in_one,
        "attribution": {"method": "sequential, heaviest change first", "sums_to_total": attribution_matches},
        "methodology_version": dpdp_score.METHODOLOGY_VERSION,
        "assessed_on": as_of.isoformat(),
        "methodology_note": (
            f"Both scans were assessed now, from their own stored evidence, with methodology v{dpdp_score.METHODOLOGY_VERSION}, the same weights and the same "
            "assessment date. A score shown in a report made earlier under an older version of the logic cannot be re-derived from the stored evidence; if it "
            "differs from the previous score above, that difference is not explained here."),
    }
