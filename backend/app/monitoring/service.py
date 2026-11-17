"""One round of checks: run them all, record the results, send the emails. Called by the worker every maintenance tick (it does nothing until a round is due) and by the
"Run checks now" button.

Several workers may call it. A database advisory lock makes sure only one runs a round at a time, and the round is skipped when the last one is recent enough.
"""

from __future__ import annotations

import logging
from datetime import timedelta

import httpx
from sqlalchemy import func, select, text

from app.db.session import async_session_factory
from app.monitoring import alerts, checks, events, mailer
from app.monitoring.config import MonitoringSettings, get_monitoring_settings
from app.monitoring.models import SystemAlert, SystemHealth
from app.monitoring.scrub import scrub

logger = logging.getLogger(__name__)


def get_gateway_url() -> str:
    """The address people reach the application through, probed each round (ALERT_GATEWAY_URL, for example http://frontend:8080/health). Empty = not probed."""
    import os
    return os.environ.get("ALERT_GATEWAY_URL", "").strip()
LOCK_KEY = 7_240_053          # arbitrary, fixed: "the monitoring round"


async def _send(settings: MonitoringSettings, notices: list[alerts.Notice]) -> None:
    """Email each notice and record the outcome. Never raises."""
    for notice in notices:
        status, reason = await mailer.send(settings, notice.subject, notice.body)
        if status == mailer.FAILED:
            logger.warning("monitoring: alert email failed: %s", reason)
        try:
            async with async_session_factory() as db:
                await alerts.mark_notified(db, notice, status, reason)
                await db.commit()
        except Exception:
            logger.exception("monitoring: could not record the outcome of an alert email")


async def run_round(force: bool = False) -> list[dict]:
    """Run every check now (when `force`) or when the interval has passed. Returns the health rows. Never raises: monitoring must not take the worker down."""
    settings = get_monitoring_settings()
    if not settings.enabled and not force:
        return []
    notices: list[alerts.Notice] = []
    try:
        async with async_session_factory() as db:
            if not (await db.execute(text("select pg_try_advisory_xact_lock(:k)"), {"k": LOCK_KEY})).scalar():
                return []                                              # another worker is running this round
            if not force:
                latest = await db.scalar(select(func.max(SystemHealth.checked_at)))
                if latest is not None and alerts.utcnow() - latest < timedelta(seconds=settings.check_interval_seconds):
                    return []
            db_result = await checks.check_database(db)
            async with httpx.AsyncClient(timeout=15) as client:
                results = [
                    db_result,
                    await checks.guarded("anthropic_api", "Claude API key", lambda: checks.check_anthropic(client)),
                    await checks.guarded("nvidia_api", "NVIDIA API key", lambda: checks.check_nvidia(client)),
                    await checks.guarded("embedding_api", "Embedding model (RAG)", lambda: checks.check_embedding_api(client)),
                ]
            async def inside_savepoint(fn):
                # a check whose query fails must not poison the transaction the other checks and the recording share
                async with db.begin_nested():
                    return await fn()

            gateway = get_gateway_url()
            if gateway:
                async with httpx.AsyncClient(timeout=15) as gclient:
                    results.append(await checks.guarded("gateway", "Gateway to the backend", lambda: checks.check_gateway(gateway, gclient)))
            if db_result.ok:
                results += [
                    await checks.guarded("vector_db", "Vector search (pgvector)", lambda: inside_savepoint(lambda: checks.check_vector_db(db))),
                    await checks.guarded("job_queue", "Background jobs", lambda: inside_savepoint(lambda: checks.check_job_queue(db))),
                    await checks.guarded("llm_calls", "Recent AI calls", lambda: inside_savepoint(lambda: checks.check_llm_calls(db))),
                    await checks.guarded("scan_failures", "Scan failures", lambda: inside_savepoint(lambda: checks.check_scan_failures(db))),
                ]
            for result in results:
                try:
                    await events.mirror_check_result(result)           # the same outcome, shown beside the other errors (no second email)
                except Exception:
                    logger.exception("monitoring: could not mirror a check result")
                notice = await alerts.record_result(db, result, settings)
                if notice is not None:
                    notices.append(notice)
            await db.commit()
        try:
            await events.record_agent_run_failures()                   # five-page analyses that ended failed (RAG retrieval, model calls), read from their own run records
        except Exception:
            logger.exception("monitoring: could not read failed agent runs")
    except Exception as exc:                                           # the database itself is unreachable: nothing can be recorded, so say it by email directly
        logger.exception("monitoring: the check round could not run")
        status, reason = await mailer.send(
            settings, "[Consiva] CRITICAL: the system monitor cannot reach the database",
            f"The monitor could not run its checks: {scrub(type(exc).__name__ + ' ' + str(exc), 200)}\n\nCheck that the database container is running.\n\nDashboard: {settings.dashboard_url}")
        logger.warning("monitoring: direct database alert email -> %s", status)
        return []
    await _send(settings, notices)
    return await health_rows()


async def health_rows() -> list[dict]:
    async with async_session_factory() as db:
        rows = (await db.scalars(select(SystemHealth).order_by(SystemHealth.label))).all()
        return [{"source": r.source, "label": r.label, "status": r.status, "detail": r.detail, "latency_ms": r.latency_ms, "checked_at": r.checked_at,
                 "last_ok_at": r.last_ok_at, "consecutive_failures": r.consecutive_failures} for r in rows]


def alert_dict(a: SystemAlert) -> dict:
    return {"id": a.id, "source": a.source, "severity": a.severity, "status": a.status, "title": a.title, "detail": a.detail, "action": a.action,
            "first_seen_at": a.first_seen_at, "last_seen_at": a.last_seen_at, "occurrences": a.occurrences, "resolved_at": a.resolved_at,
            "acknowledged_by": a.acknowledged_by, "acknowledged_at": a.acknowledged_at, "last_notified_at": a.last_notified_at,
            "notify_count": a.notify_count, "notify_status": a.notify_status}


async def alert_rows(limit_resolved: int = 20) -> list[dict]:
    async with async_session_factory() as db:
        live = (await db.scalars(select(SystemAlert).where(SystemAlert.status.in_(alerts.LIVE)).order_by(SystemAlert.first_seen_at.desc()))).all()
        done = (await db.scalars(select(SystemAlert).where(SystemAlert.status == "resolved").order_by(SystemAlert.resolved_at.desc()).limit(limit_resolved))).all()
        return [alert_dict(a) for a in [*live, *done]]


async def send_test_email() -> tuple[str, str | None]:
    """A real message through the real settings, so the first time the mail setup is proven is not the first time something breaks."""
    settings = get_monitoring_settings()
    return await mailer.send(
        settings, "[Consiva] TEST: alert emails are working",
        "This is a test message from the Consiva system monitor.\n\nIf you can read it, the mail server settings are correct and real alerts will reach this address.\n\n"
        f"Dashboard: {settings.dashboard_url}")
