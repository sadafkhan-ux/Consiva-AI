"""Read-side translation for the /consent-agent integration API.

Everything here derives from records the Consent Agent pipeline already writes. This
module computes nothing about the website itself and calls no model -- it reshapes
consent_scans, agent_runs, agent_run_stages, consent_findings and the evidence tables
into the flatter contract an external caller polls.

The one thing it genuinely decides is `progress`, and it does that from completed
pipeline stages rather than a timer, because a progress bar that advances on wall-clock
while the pipeline is stuck is worse than no progress bar.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    AgentRun, AgentRunStage, ConsentFinding, ConsentRecommendation, ConsentScan,
)
from app.db.repositories import scan_repository

# The pipeline's stages in order, with the share of total progress each represents.
#
# Weighted by measured duration, not spread evenly: on real scans website_scan is
# 10-145 seconds and every other stage except llm_analysis is under a second, so equal
# weighting would park the bar at 10% for the entire crawl and then sprint through the
# rest. These weights make the bar move roughly with time actually spent.
_STAGE_WEIGHTS: tuple[tuple[str, int], ...] = (
    ("url_validation", 2),
    ("website_scan", 55),
    ("classification", 3),
    ("data_structuring", 5),
    ("rules_check", 2),
    ("rag_retrieval", 5),
    ("llm_analysis", 20),
    ("output_validation", 2),
    ("findings_generated", 4),
    ("audit_saved", 2),
)

# create_findings and create_rule_findings are alternatives -- a run reaches one or the
# other, never both -- so they share the same slot in the progress model.
_FINDINGS_STAGES = ("findings_generated", "rule_findings_generated")

_TERMINAL_SCAN_STATUSES = {"completed", "failed", "cancelled"}


async def _stages(db: AsyncSession, scan_id: uuid.UUID) -> list[AgentRunStage]:
    result = await db.execute(
        select(AgentRunStage).where(AgentRunStage.scan_id == scan_id).order_by(AgentRunStage.created_at)
    )
    return list(result.scalars().all())


def _progress(stages: list[AgentRunStage], scan_status: str) -> int:
    """Percent complete, from what the pipeline has actually finished.

    A terminal scan always reads 100: a failed scan is not 40% done, it is finished
    and unsuccessful, and leaving the bar mid-way invites a caller to keep polling
    something that will never move again.
    """
    if scan_status in _TERMINAL_SCAN_STATUSES:
        return 100
    by_name = {s.stage: s for s in stages}
    earned = 0
    for name, weight in _STAGE_WEIGHTS:
        candidates = _FINDINGS_STAGES if name == "findings_generated" else (name,)
        stage = next((by_name[c] for c in candidates if c in by_name), None)
        if stage is None:
            continue
        if stage.status == "completed":
            earned += weight
        elif stage.status in ("running", "failed"):
            # Half credit for a stage that is under way or has ended badly: it has
            # done real work either way, and a failed stage still lets the run move on
            # (llm_analysis failing routes to the rule-derived fallback).
            earned += weight // 2
    return min(earned, 99)


def _current_stage(stages: list[AgentRunStage]) -> str | None:
    running = next((s.stage for s in stages if s.status == "running"), None)
    if running:
        return running
    return stages[-1].stage if stages else None


def _api_status(scan: ConsentScan, run: AgentRun | None, stages: list[AgentRunStage]) -> str:
    """The single status an integrator sees, from the several the platform keeps.

    The platform tracks a scan status AND an agent-run status, and they disagree by
    design -- the crawl can finish while the analysis is still going, and an external
    validation of this product specifically flagged a screen showing "Failed" next to
    "completed" for exactly that reason. One field is exposed here, and it answers the
    only question a poller has: is there a final result yet?
    """
    if scan.status == "cancelled":
        return "cancelled"
    if scan.status == "failed":
        return "failed"
    if scan.status in ("pending", "running"):
        return "queued" if scan.status == "pending" else "running"
    # The crawl is done. The answer now depends on the analysis.
    if run is None:
        return "running"          # queued for analysis, not yet picked up
    if run.status == "failed":
        # The analysis failing does NOT make the scan failed: the rule-derived
        # fallback still produces findings from measured evidence. Only a run that
        # produced nothing at all is a failure to the caller.
        has_findings = any(s.stage in _FINDINGS_STAGES and s.status == "completed" for s in stages)
        return "completed" if has_findings else "failed"
    if run.status in ("completed", "paused"):
        # `paused` is the human-review gate: findings are all present and waiting on a
        # decision, which is a complete result as far as an API consumer is concerned.
        return "completed"
    return "running"


def _page_stats(stages: list[AgentRunStage]) -> dict[str, int]:
    meta = next((s.stage_metadata or {} for s in stages if s.stage == "website_scan"), {})
    return {
        "pages_discovered": int(meta.get("pages_found") or 0),
        "pages_scanned": int(meta.get("pages_found") or 0) - int(meta.get("pages_failed") or 0),
        "pages_failed": int(meta.get("pages_failed") or 0),
        "pages_timeout": int(meta.get("pages_timeout") or 0),
        "pages_blocked_robots": int(meta.get("pages_blocked_robots") or 0),
    }


def _token_metrics(stages: list[AgentRunStage]) -> dict[str, Any] | None:
    """Real measured usage, or nothing.

    Returns None rather than zeros when the analysis has not run: a caller reading
    total_tokens=0 would reasonably conclude the scan was free, when the truth is that
    it has not been billed yet.
    """
    stage = next((s for s in stages if s.stage == "llm_analysis"), None)
    if stage is None:
        return None
    meta = stage.stage_metadata or {}
    usage = meta.get("llm_usage") or {}
    attempts = usage.get("usage_by_attempt") or []
    if not attempts:
        return None
    rag = next((s for s in stages if s.stage == "rag_retrieval"), None)
    return {
        "llm_calls": len(attempts),
        "rag_calls": 1 if rag is not None else 0,
        "input_tokens": sum(a.get("prompt_tokens") or 0 for a in attempts),
        "output_tokens": sum(a.get("completion_tokens") or 0 for a in attempts),
        "total_tokens": sum(a.get("total_tokens") or 0 for a in attempts),
        "provider": meta.get("provider"),
        "model": _public_model_name(meta.get("model")),
    }


def _public_model_name(model: str | None) -> str | None:
    """The model's name, without the path it happens to live at.

    A self-hosted model is configured by filesystem path, and that path went out over
    the API verbatim: "/home/gignaati/dbeaver/MODELS_LLAMA_CPP/models/qwen3.6-35b-a3b/
    Qwen3.6-35B-A3B-Q4_K_M.gguf" tells an external caller the inference host's
    directory layout and an operating-system username, neither of which is any of their
    business. A hosted model id ("openai/gpt-oss-120b") has no path and passes through
    unchanged.
    """
    if not model:
        return None
    if "/" not in model and "\\" not in model:
        return model
    return model.replace("\\", "/").rsplit("/", 1)[-1]


_SEVERITY_ORDER = ("high", "medium", "low")


async def _recommendations_by_finding(
    db: AsyncSession, finding_ids: list[uuid.UUID]
) -> dict[uuid.UUID, str]:
    """Recommendation text per finding, in one query.

    The pipeline stores these in their own table, and the API was reporting
    `recommendation: null` on every finding while three rows sat in
    consent_recommendations for the very scan being returned. Fetched in a single
    statement rather than per finding -- a scan can carry a dozen, and this endpoint is
    polled.
    """
    if not finding_ids:
        return {}
    result = await db.execute(
        select(ConsentRecommendation).where(ConsentRecommendation.finding_id.in_(finding_ids))
    )
    return {r.finding_id: r.recommendation_text for r in result.scalars().all()}


def _finding_out(row: ConsentFinding, recommendation: str | None = None) -> dict[str, Any]:
    """One stored finding, in the API's shape.

    `title` is the finding's own first sentence rather than a separate stored field,
    because the pipeline has never had one: inventing a title here would mean writing
    prose about a compliance finding in a translation layer, which is exactly the kind
    of fabrication the rest of this system goes out of its way to avoid.
    """
    text = (row.finding_text or "").strip()
    first_line = text.split("\n", 1)[0]
    title = first_line if len(first_line) <= 120 else first_line[:117].rstrip() + "..."
    evidence = [
        {"type": "evidence_ref", "local_id": str(ref), "domain": None, "source": None}
        for ref in (row.evidence or [])
    ]
    return {
        "id": row.id,
        "severity": row.risk_level if row.risk_level in _SEVERITY_ORDER else "medium",
        "category": row.category or "other",
        "title": title,
        "description": text,
        "evidence": evidence,
        # Not stored per finding by the pipeline. Reported as null rather than filled
        # in with a plausible-looking number.
        "confidence": None,
        "status": row.status,
        "requires_human_review": bool(row.requires_human_review),
        "recommendation": recommendation,
        "dpdp_references": list(row.dpdp_reference or []),
        "created_at": row.created_at,
    }


async def _findings(db: AsyncSession, scan_id: uuid.UUID) -> list[ConsentFinding]:
    result = await db.execute(
        select(ConsentFinding).where(ConsentFinding.scan_id == scan_id).order_by(ConsentFinding.created_at)
    )
    return list(result.scalars().all())


async def get_status(db: AsyncSession, scan: ConsentScan) -> dict[str, Any]:
    stages = await _stages(db, scan.id)
    run = await scan_repository.latest_agent_run(db, scan.id)
    status = _api_status(scan, run, stages)
    stats = _page_stats(stages)
    error = None
    if scan.error:
        error = {"code": "SCAN_FAILED", "message": scan.error}
    else:
        failed = next((s for s in stages if s.status == "failed"), None)
        if failed is not None:
            error = {
                "code": "STAGE_FAILED",
                "message": f"Stage {failed.stage!r} failed: {failed.error}",
                "stage": failed.stage,
                # Stated explicitly so an integrator is not left guessing whether a
                # failed stage means they got nothing.
                "recoverable": status == "completed",
            }
    return {
        "scan_id": scan.id,
        "website_url": scan.url,
        "status": status,
        "progress": _progress(stages, status),
        "pages_discovered": stats["pages_discovered"],
        "pages_scanned": stats["pages_scanned"],
        "pages_failed": stats["pages_failed"],
        "current_stage": _current_stage(stages),
        "started_at": scan.started_at,
        "completed_at": scan.completed_at,
        "error": error,
    }


async def get_findings(db: AsyncSession, scan: ConsentScan) -> dict[str, Any]:
    rows = await _findings(db, scan.id)
    recs = await _recommendations_by_finding(db, [r.id for r in rows])
    return {"scan_id": scan.id, "findings": [_finding_out(r, recs.get(r.id)) for r in rows]}


async def get_summary(db: AsyncSession, scan: ConsentScan) -> dict[str, Any]:
    stages = await _stages(db, scan.id)
    run = await scan_repository.latest_agent_run(db, scan.id)
    counts = await scan_repository.get_scan_counts(db, scan.id)
    rows = await _findings(db, scan.id)

    by_severity = {"critical": 0, "high": 0, "medium": 0, "low": 0}
    for row in rows:
        key = row.risk_level if row.risk_level in by_severity else "medium"
        by_severity[key] += 1

    scan_meta = next((s.stage_metadata or {} for s in stages if s.stage == "website_scan"), {})
    mechanism = scan_meta.get("consent_mechanism")
    status = _api_status(scan, run, stages)

    if status != "completed":
        compliance = "unknown"
    elif by_severity["critical"] or by_severity["high"]:
        compliance = "issues_found"
    elif rows:
        compliance = "review_required"
    else:
        compliance = "compliant"

    accept = scan_meta.get("accept_interaction")
    reject = scan_meta.get("reject_interaction")
    return {
        "scan_id": scan.id,
        "website": scan.url,
        "status": status,
        "pages_scanned": _page_stats(stages)["pages_scanned"],
        "cookies_detected": counts.get("cookies", 0),
        "trackers_detected": counts.get("trackers", 0),
        "cmp_detected": mechanism in ("banner", "cmp"),
        "cmp_vendor": scan_meta.get("cmp_vendor"),
        "cmp_confidence": scan_meta.get("cmp_confidence"),
        # "tested" means the pass actually operated the control, not that it was
        # attempted -- only `clicked` establishes the consent state it is named after.
        "consent_states_tested": {
            "before": bool(scan_meta.get("pages_found")),
            "accept": accept == "clicked",
            "reject": reject == "clicked",
        },
        "findings": by_severity,
        "compliance_status": compliance,
    }


async def get_result(db: AsyncSession, scan: ConsentScan) -> dict[str, Any]:
    stages = await _stages(db, scan.id)
    run = await scan_repository.latest_agent_run(db, scan.id)
    evidence = await scan_repository.get_scan_evidence_summary(db, scan.id)
    rows = await _findings(db, scan.id)
    recs = await _recommendations_by_finding(db, [r.id for r in rows])
    counts = await scan_repository.get_scan_counts(db, scan.id)
    scan_meta = next((s.stage_metadata or {} for s in stages if s.stage == "website_scan"), {})

    duration_ms = None
    if scan.started_at and scan.completed_at:
        duration_ms = int((scan.completed_at - scan.started_at).total_seconds() * 1000)

    errors = [s.error for s in stages if s.error]
    if scan.error:
        errors.insert(0, scan.error)

    return {
        "scan_id": scan.id,
        "website_url": scan.url,
        "status": _api_status(scan, run, stages),
        "scanner_version": scan.scanner_version,
        "scan_options": scan.scan_options or {},
        "started_at": scan.started_at,
        "completed_at": scan.completed_at,
        "duration_ms": duration_ms,
        "page_stats": _page_stats(stages),
        "consent_mechanism": {
            "mechanism_type": scan_meta.get("consent_mechanism"),
            "cmp_vendor": scan_meta.get("cmp_vendor"),
            "confidence": scan_meta.get("cmp_confidence"),
            "detection_source": scan_meta.get("cmp_detection_source"),
        } if scan_meta else None,
        "consent_states": {
            "accept_interaction": scan_meta.get("accept_interaction"),
            "reject_interaction": scan_meta.get("reject_interaction"),
            "cookies_by_consent_state": scan_meta.get("cookies_by_consent_state") or {},
            "trackers_by_consent_state": scan_meta.get("trackers_by_consent_state") or {},
        },
        "evidence_counts": counts,
        "cookies": evidence.get("cookies", []),
        "trackers": evidence.get("trackers", []),
        "third_party_services": evidence.get("third_party_services", []),
        "forms": evidence.get("forms", []),
        "policies": evidence.get("policies", []),
        "findings": [_finding_out(r, recs.get(r.id)) for r in rows],
        "stages": [
            {"stage": s.stage, "status": s.status, "duration_ms": s.duration_ms} for s in stages
        ],
        "token_metrics": _token_metrics(stages),
        "errors": errors,
    }


async def list_scans(
    db: AsyncSession, *, org_id: uuid.UUID, limit: int, offset: int, status: str | None = None
) -> tuple[list[dict[str, Any]], int]:
    """Recent scans for this organisation, newest first, with the total for paging.

    Agent 1 was the only agent in the platform with no list endpoint -- ROPA, DSR,
    Incidents and Regulatory Watch all have one. A caller who lost a scan_id had no way
    to find it again, and a frontend could not show scan history at all.

    Returns a summary row per scan, not the full result: this is a list view, and
    inlining evidence would make one page of it heavier than every other endpoint
    combined.
    """
    base = select(ConsentScan).where(ConsentScan.org_id == org_id)
    if status:
        base = base.where(ConsentScan.status == status)

    total = await db.scalar(
        select(func.count()).select_from(base.subquery())
    ) or 0

    result = await db.execute(
        base.order_by(ConsentScan.created_at.desc()).limit(limit).offset(offset)
    )
    scans = list(result.scalars().all())
    if not scans:
        return [], total

    # Finding counts for the whole page in ONE query rather than one per scan -- a
    # 50-row page would otherwise be 51 round trips.
    counts = dict(
        (row[0], row[1])
        for row in (await db.execute(
            select(ConsentFinding.scan_id, func.count())
            .where(ConsentFinding.scan_id.in_([s.id for s in scans]))
            .group_by(ConsentFinding.scan_id)
        )).all()
    )

    rows = []
    for scan in scans:
        run = await scan_repository.latest_agent_run(db, scan.id)
        rows.append({
            "scan_id": scan.id,
            "website_url": scan.url,
            # Derived the same way the status endpoint derives it, so a list row and a
            # detail view never disagree about whether a scan is done.
            "status": _api_status(scan, run, []),
            "findings_count": counts.get(scan.id, 0),
            "started_at": scan.started_at,
            "completed_at": scan.completed_at,
            "created_at": scan.created_at,
        })
    return rows, total


async def find_by_idempotency_key(
    db: AsyncSession, *, org_id: uuid.UUID, key: str
) -> ConsentScan | None:
    result = await db.execute(
        select(ConsentScan).where(
            ConsentScan.org_id == org_id, ConsentScan.idempotency_key == key
        )
    )
    return result.scalars().first()


async def count_scans_since(db: AsyncSession, *, org_id: uuid.UUID, since: datetime) -> int:
    return await db.scalar(
        select(func.count()).select_from(ConsentScan).where(
            ConsentScan.org_id == org_id, ConsentScan.created_at >= since
        )
    ) or 0


async def mark_cancelled(db: AsyncSession, scan: ConsentScan) -> None:
    scan.status = "cancelled"
    scan.completed_at = datetime.now(UTC)
    await db.flush()
