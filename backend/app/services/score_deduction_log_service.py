"""The score's deductions, kept where the Consent Agent runs and nowhere a customer reads.

The client-facing report, the dashboard and the score API do not carry the scoring formula or the deduction explanation. They are written here, once per
analysis run, beside the rest of the scan's logs and results: a readable block in the server log (logger `consiva.score_deductions`) and one row in
`audit_logs` (`action = "scan.score_deductions"`, the full breakdown in `after`). The audit API lists a row's action and ids only, never its `after`, so
the breakdown is read from the database or the log by whoever operates the agent.

The figures are the score's own (scoring/deductions.py, computed from the same results as the score); nothing is re-derived or estimated here.
"""

import logging
import uuid
from dataclasses import asdict

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.repositories import audit_repository, scan_repository
from app.scoring import dpdp_score
from app.scoring.deductions import Deductions

logger = logging.getLogger("consiva.score_deductions")

ACTION = "scan.score_deductions"


def format_log(scan_id: uuid.UUID, url: str | None, d: Deductions, *, methodology_version: str, as_of: str) -> str:
    """The breakdown as plain log lines: one header, the formula, then each deducting check with its marks, evidence and the checks that cost nothing."""
    lines = [f"Score deductions for scan {scan_id} ({url or 'unknown address'}); methodology {methodology_version}, assessed on {as_of}", f"  {d.summary}", f"  {d.formula}"]
    if d.rounding_note:
        lines.append(f"  {d.rounding_note}")
    for s in d.steps:
        lines.append(f"  - {s.requirement_id} {s.title}: {s.outcome}; weight {s.weight}, earned {s.points_earned:g}, lost {s.points_lost:g}; "
                     f"-{s.percent_lost} points ({s.score_before}% -> {s.score_after}%)")
        lines.append(f"      {s.calculation}")
        lines.append(f"      Why: {s.why}")
        if not s.in_force:
            lines.append(f"      Applies from {s.effective}: a readiness gap, not a current violation.")
        for e in s.evidence:
            where = ", ".join(x for x in (e.vendor, e.category, *e.consent_states) if x)
            lines.append(f"      Evidence ({e.kind}): {e.text}{f' [{where}]' if where else ''}{f' on {e.url}' if e.url else ''}")
    for n in d.not_deducted:
        lines.append(f"  - not deducted, {n.requirement_id} {n.title}: {n.status}; {n.reason}")
    return "\n".join(lines)


async def record_scan_deductions(db: AsyncSession, *, scan_id: uuid.UUID, org_id: uuid.UUID, agent_run_id: uuid.UUID | None = None) -> Deductions | None:
    """Assess the scan from its stored evidence exactly as the score endpoint does, then log and store the deductions. Returns them, or None when the scan is gone.
    The caller commits. A failure here must never cost the analysis its own audit trail, so callers wrap it."""
    scan = await scan_repository.get_scan(db, scan_id, org_id)
    if scan is None:
        return None
    evidence = await scan_repository.get_scan_evidence_summary_concurrent(scan_id, include_cookie_detail=True)
    when = scan.completed_at or scan.started_at or scan.created_at
    result = dpdp_score.assess(
        evidence, None, None, get_settings().dpdp_score_weights or None, scan_url=scan.url, scanned_at=when.isoformat() if when else None,
    )
    d = result.deductions
    if d is None:
        return None
    as_of = result.as_of.isoformat()
    logger.info("%s", format_log(scan_id, scan.url, d, methodology_version=result.methodology_version, as_of=as_of))
    await audit_repository.record(
        db, org_id=org_id, actor_user_id=None, action=ACTION, entity_type="scan", entity_id=scan_id, agent_run_id=agent_run_id,
        after={"methodology_version": result.methodology_version, "as_of": as_of, "deductions": asdict(d)},
    )
    return d
