"""Validate the DPDP Compliance Score against every completed scan in the database.

Run inside the backend environment. The image does not include `scripts/`, so copy it in first:
`docker cp backend/scripts/validate_dpdp_score.py consiva-backend-1:/tmp/` then
`docker exec -e PYTHONPATH=/app consiva-backend-1 sh -c 'cd /app && python /tmp/validate_dpdp_score.py'`.
It recomputes each scan's score from its recorded evidence and checks:

  * invariants that must hold for any evidence (score range, coverage, status counts, no Met without pages, remediation present
    for every gap, severity only on duties in force, no requirement listed twice, nothing about other domains);
  * agreement with the existing rules engine, which reads the same evidence: a check is never Not Met unless the matching rule
    (DPDP-C1 / R-001, DPDP-N1 / R-006, DPDP-C4 / R-005) fired, and never Met when it did. (The rule may fire while the check is
    Unable to Verify: the score refuses to judge a framed wrapper, which the rule only caveats.);
  * reproducibility: scoring the same recorded evidence twice gives the same result.

It prints one line per scan for a person to review against the site, and exits non-zero on any violated invariant.
Read-only: it never writes to the database.
"""

import asyncio
import sys
from dataclasses import asdict

from sqlalchemy import select

from app.db.models import ConsentScan
from app.db.repositories import scan_repository
from app.db.session import async_session_factory
from app.rules.consent_rules import evaluate_consent_rules
from app.scoring.dpdp_score import assess


def check(scan_url: str, evidence: dict, scanned_at: str | None) -> tuple[list[str], str]:
    problems: list[str] = []
    a = assess(evidence, scan_url=scan_url, scanned_at=scanned_at)
    b = assess(evidence, scan_url=scan_url, scanned_at=scanned_at)
    if asdict(a) != asdict(b):
        problems.append("not reproducible: two runs on the same evidence differ")
    r = a.readiness
    if r.score_percent is not None and not 0 <= r.score_percent <= 100:
        problems.append(f"score out of range: {r.score_percent}")
    if (r.status == "scored") != (r.score_percent is not None):
        problems.append("status and score disagree")
    if r.coverage_percent is not None and not 0 <= r.coverage_percent <= 100:
        problems.append(f"coverage out of range: {r.coverage_percent}")
    if r.assessed_weight > r.applicable_weight:
        problems.append("assessed weight exceeds applicable weight")
    if sum(a.status_counts.values()) != len(a.requirements):
        problems.append("status counts do not add up")
    if not evidence["pages"] and any(q.status == "met" for q in a.requirements):
        problems.append("a requirement is Met although no page was scanned")
    for q in a.requirements:
        if q.points_earned > q.weight:
            problems.append(f"{q.id}: earned more than its weight")
        if q.status in ("not_met", "partially_met") and not (q.remediation and q.verification and q.why_it_matters):
            problems.append(f"{q.id}: a gap without remediation")
        if q.check_type == "business" and q.status != "unable_to_verify" and q.status != "not_applicable":
            problems.append(f"{q.id}: a business check was assessed by a scan")
    g = a.finding_groups
    listed = [rid for e in g.confirmed + g.future_readiness + g.unable_to_verify for rid in e.requirement_ids]
    if len(listed) != len(set(listed)):
        problems.append("a requirement is listed in two groups")
    if any(e.severity for e in g.future_readiness + g.needs_review + g.unable_to_verify):
        problems.append("severity on a finding that is not a confirmed finding")
    if sum(c.weight for c in a.methodology.categories) != r.applicable_weight:
        problems.append("category weights do not add up to the applicable weight")
    blob = str(asdict(a)).lower()
    if "subdomain" in blob or "discovered" in blob:
        problems.append("the result mentions subdomains or discovered domains")

    fired = {f.rule_id for f in evaluate_consent_rules(evidence)}
    status = {q.id: q.status for q in a.requirements}
    for rid, rule in (("DPDP-C1", "R-001"), ("DPDP-N1", "R-006"), ("DPDP-C4", "R-005")):
        if status[rid] == "not_met" and rule not in fired:
            problems.append(f"{rid} is Not Met but rule {rule} did not fire")
        if rule in fired and status[rid] == "met":
            problems.append(f"{rid} is Met although rule {rule} fired")

    summary = (
        f"{scan_url:34} pages={len(evidence['pages'])} req={len(evidence['trackers'])} cookies={len(evidence['cookies'])} "
        f"score={r.score_percent if r.score_percent is not None else 'Insufficient Evidence'} cov={r.coverage_percent} "
        f"| {' '.join(f'{k}={v}' for k, v in a.status_counts.items() if v)} | rules={sorted(fired)} "
        f"| groups={g.counts} | tested={a.assessment.states_tested}"
    )
    return problems, summary


async def main() -> int:
    bad = 0
    async with async_session_factory() as db:
        scans = (await db.execute(select(ConsentScan).where(ConsentScan.status == "completed").order_by(ConsentScan.created_at))).scalars().all()
    for scan in scans:
        evidence = await scan_repository.get_scan_evidence_summary_concurrent(scan.id, include_cookie_detail=True)
        done = scan.completed_at or scan.started_at or scan.created_at
        problems, summary = check(scan.url, evidence, done.isoformat() if done else None)
        print(("FAIL " if problems else "ok   ") + summary)
        for p in problems:
            print("      -", p)
        bad += bool(problems)
    print(f"\n{len(scans)} completed scans checked, {bad} with violated invariants")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
