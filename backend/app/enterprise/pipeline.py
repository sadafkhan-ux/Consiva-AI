"""The Enterprise analysis phase: from a finished crawl to assessments, a score and findings.

    crawl (crawl/runner.py)  ->  rule assessors (technical, consent)  ->  text analysis (analysis/, model-assisted)  ->  score  ->  findings

The crawl and the analysis are separate: a scan's `status` is the crawl, `analysis_status` is this phase. Each step saves what it produces before the next starts, so a failure in one
loses nothing already done, and running the phase again continues rather than starts over (the rule assessors are cheap and deterministic; the text analysis resumes by chunk).

A step that cannot run (the model is unreachable, a module is missing) leaves the parameters it feeds as NOT ASSESSED and the scan's analysis as `partial`, with the reason in
`analysis_error`. It is never turned into a failure of the website.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date

from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import async_session_factory, set_org_scope
from app.enterprise import store
from app.enterprise.assess.loader import assess_scan_consent, assess_scan_technical
from app.enterprise.findings import build_findings
from app.enterprise.models import EntScan
from app.enterprise.coverage import LIMIT_STOPS
from app.enterprise.scoring.engine import NOT_ASSESSED, NOT_MET, Assessment, ScoreResult, score

logger = logging.getLogger(__name__)


async def set_analysis_status(db: AsyncSession, scan_id: uuid.UUID, status: str, *, error: str | None = None) -> None:
    values: dict = {"analysis_status": status, "analysis_error": error, "updated_at": text("now()")}
    if status == "running":
        values.update(analysis_started_at=text("now()"), analysis_completed_at=None)
    if status in ("completed", "partial", "failed"):
        values["analysis_completed_at"] = text("now()")
    await db.execute(update(EntScan).where(EntScan.id == scan_id).values(**values))


async def run_rule_assessors(db: AsyncSession, scan_id: uuid.UUID, as_of: date) -> dict[str, int]:
    """The parameters decided by fixed rules from observations (no model). Returns how many each wrote."""
    technical = await assess_scan_technical(db, scan_id, as_of)
    consent = await assess_scan_consent(db, scan_id)
    return {
        "technical": await store.save_assessments(db, scan_id, technical, assessor="rule:technical"),
        "consent": await store.save_assessments(db, scan_id, consent, assessor="rule:consent"),
    }


async def recalculate(db: AsyncSession, scan_id: uuid.UUID, as_of: date | None = None, *, actor: str = "system") -> ScoreResult:
    """Score the current assessments and rewrite the findings from them. Called after the analysis and after every human review, so the score, the findings and the parameters never disagree."""
    as_of = as_of or date.today()
    current = await store.load_current(db, scan_id)
    result = score(current, as_of=as_of)
    score_id = await store.save_score(db, scan_id, result)
    rows = await store.load_current_rows(db, scan_id)
    n = await store.replace_findings(db, scan_id, await build_findings(db, rows))
    await store.audit(db, scan_id, actor, "score_calculated", {"score_result_id": str(score_id), "score": result.score, "coverage": result.coverage, "basis": result.basis, "findings": n})
    return result


async def apply_crawl_completeness_guard(db: AsyncSession, scan_id: uuid.UUID) -> bool:
    """"Not found" is only a finding when the whole site was looked at. If the crawl stopped on a limit, or URLs are still waiting, a privacy notice that was not found may simply be on a page that
    was not reached: N1 is then NOT ASSESSED, with the reason, instead of Not met. Returns whether it changed anything."""
    row = (await db.execute(text("select stop_reason from ent_scans where id = :s"), {"s": scan_id})).first()
    pending = await db.scalar(text("select count(*) from ent_frontier where scan_id = :s and status in ('pending', 'in_progress')"), {"s": scan_id}) or 0
    reason = row.stop_reason if row and row.stop_reason in LIMIT_STOPS else None
    if reason is None and not pending:
        return False
    current = {a.parameter_id: a for a in await store.load_current(db, scan_id)}
    n1 = current.get("N1")
    if n1 is None or n1.state != NOT_MET:
        return False
    why = f"the crawl stopped early ({reason})" if reason else f"{pending} discovered URLs were not reached"
    note = f"No privacy notice was found in the pages reached, but {why}, so a notice may exist on a page that was not read. Scan again with a higher limit before relying on this."
    await store.save_assessments(db, scan_id, [Assessment("N1", NOT_ASSESSED, (), None, note)], assessor="rule:coverage")
    return True


def _text_analysis():
    """The model-assisted text analysis, if it is installed. None when it is not, which leaves the parameters it feeds not assessed."""
    try:
        from app.enterprise.analysis.pipeline import run_text_analysis
    except ImportError:
        return None
    return run_text_analysis


async def _sync_policy_kb(scan_id: uuid.UUID, org_id: uuid.UUID) -> None:
    """Bring the website's policy knowledge base up to date from this scan. A failure here is logged and audited; it is a processing failure of the knowledge base, never a finding about the website,
    and it never changes the scan's own status or score."""
    from app.enterprise.config import get_enterprise_settings
    if not get_enterprise_settings().policy_kb_enabled:
        return
    try:
        from app.enterprise.policykb import history, sync
        result = await sync.sync_scan(scan_id, org_id)
        async with async_session_factory() as db:
            await store.audit(db, scan_id, "system", "policy_kb_synced", {
                "counts": result.counts, "embeddings_generated": result.embeddings_generated, "embeddings_reused": result.embeddings_reused, "errors": result.errors[:5],
                "parameters_to_reassess": history.affected_parameters(result.changed_types)})
            await db.commit()
    except Exception as exc:
        logger.exception("enterprise scan %s: the policy knowledge base could not be synchronised", scan_id)
        from app.monitoring import events as error_events
        await error_events.record_error(agent="enterprise", module="enterprise.pipeline.policy_kb_sync", exc=exc, service="policy_kb", scan_id=scan_id)
        try:
            async with async_session_factory() as db:
                await store.audit(db, scan_id, "system", "policy_kb_sync_failed", {"error": f"{type(exc).__name__}: {str(exc)[:300]}"})
                await db.commit()
        except Exception:
            logger.exception("enterprise scan %s: the failed synchronisation could not be audited", scan_id)


async def run_analysis_phase(scan_id: uuid.UUID, org_id: uuid.UUID, *, as_of: date | None = None, llm=None) -> str:
    """Run the whole analysis phase for a scan whose crawl has finished. Returns the scan's analysis status."""
    set_org_scope(org_id)
    as_of = as_of or date.today()
    async with async_session_factory() as db:
        await set_analysis_status(db, scan_id, "running")
        await store.audit(db, scan_id, "system", "analysis_started", {})
        await db.commit()

    problems: list[str] = []
    try:
        async with async_session_factory() as db:
            wrote = await run_rule_assessors(db, scan_id, as_of)
            await db.commit()
        logger.info("enterprise scan %s: rule assessors wrote %s", scan_id, wrote)
    except Exception as exc:
        logger.exception("enterprise scan %s: the rule assessors failed", scan_id)
        problems.append(f"rule assessors: {type(exc).__name__}: {exc}"[:300])

    outcome_status = None
    run_text = _text_analysis()
    if run_text is None:
        problems.append("text analysis is not installed: the notice, contact, rights and cross-check parameters were not assessed")
    else:
        try:
            outcome = await run_text(scan_id, org_id, llm=llm, as_of=as_of) if llm is not None else await run_text(scan_id, org_id, as_of=as_of)
            outcome_status = getattr(outcome, "status", None)
        except Exception as exc:
            logger.exception("enterprise scan %s: the text analysis failed", scan_id)
            problems.append(f"text analysis: {type(exc).__name__}: {exc}"[:300])

    await _sync_policy_kb(scan_id, org_id)

    try:
        async with async_session_factory() as db:
            if await apply_crawl_completeness_guard(db, scan_id):
                problems.append("the crawl was cut short, so the absence of a privacy notice is not a finding (N1 not assessed)")
            await recalculate(db, scan_id, as_of)
            await db.commit()
    except Exception as exc:
        logger.exception("enterprise scan %s: scoring failed", scan_id)
        problems.append(f"scoring: {type(exc).__name__}: {exc}"[:300])

    async with async_session_factory() as db:
        current = await db.scalar(text("select analysis_status from ent_scans where id = :i"), {"i": scan_id})
        if problems:
            final = "failed" if current == "failed" and not outcome_status else "partial"
        else:
            final = current if current in ("completed", "partial", "failed") else "completed"
        await set_analysis_status(db, scan_id, final, error="; ".join(problems) or None)
        await store.audit(db, scan_id, "system", "analysis_finished", {"status": final, "problems": problems})
        await db.commit()
    from app.cleanup import core as cleanup
    await cleanup.after_scan()          # clears the scan data of finished scans (both agents) once their grace period has passed; never changes the result
    return final
