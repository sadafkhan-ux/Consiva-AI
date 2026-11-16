"""From a check result to an alert and, when a person must hear about it, an email.

Rules (all pure functions or small database steps, so they are tested directly):
  * One live alert per source. A failure that lasts a day is one alert whose `occurrences` grows, never a stream of rows.
  * A new alert emails at once. While it stays open and unacknowledged it reminds every `reminder_hours`. An acknowledged alert stays on the dashboard but stops reminding.
  * When the check passes again the alert is resolved and one "recovered" email is sent, so nobody keeps watching a problem that is gone.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.monitoring.checks import CheckResult
from app.monitoring.config import MonitoringSettings
from app.monitoring.models import SystemAlert, SystemHealth
from app.monitoring.scrub import scrub

LIVE = ("open", "acknowledged")
NEW, REMINDER, RECOVERED = "new", "reminder", "recovered"
_RANK = {"warning": 0, "critical": 1}


@dataclass(frozen=True)
class Notice:
    kind: str
    alert_id: object
    subject: str
    body: str


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def decide_reminder(status: str, last_notified_at: datetime | None, now: datetime, reminder_hours: int) -> bool:
    """Whether a still-failing alert should email again. Only an open (not acknowledged) alert reminds, and only after `reminder_hours` since the last email."""
    if status != "open":
        return False
    if last_notified_at is None:
        return True
    return now - last_notified_at >= timedelta(hours=reminder_hours)


def _age(since: datetime, now: datetime) -> str:
    minutes = max(0, int((now - since).total_seconds() // 60))
    if minutes < 60:
        return f"{minutes} minute(s)"
    hours = minutes // 60
    return f"{hours} hour(s)" if hours < 48 else f"{hours // 24} day(s)"


def compose(kind: str, alert: SystemAlert, settings: MonitoringSettings, now: datetime | None = None) -> tuple[str, str]:
    """(subject, body) for one email. Plain text, one fact per line, and it always says what to do."""
    now = now or utcnow()
    tag = {NEW: alert.severity.upper(), REMINDER: "STILL OPEN", RECOVERED: "RECOVERED"}[kind]
    subject = f"[Consiva] {tag}: {alert.title}"
    lines = []
    if kind == RECOVERED:
        lines += [f"{alert.title} is fixed. The check passed again.", "", f"It was open for {_age(alert.first_seen_at, alert.resolved_at or now)}."]
    else:
        lines += [alert.title, "", f"What: {alert.detail}", f"Since: {alert.first_seen_at:%d %b %Y %H:%M} UTC ({_age(alert.first_seen_at, now)} ago)",
                  f"Seen in {alert.occurrences} check round(s)."]
        if alert.action:
            lines += ["", f"What to do: {alert.action}"]
    lines += ["", f"Dashboard: {settings.dashboard_url}", "", "This message was sent automatically by the Consiva system monitor."]
    return subject, "\n".join(lines)


async def record_result(db: AsyncSession, result: CheckResult, settings: MonitoringSettings, now: datetime | None = None) -> Notice | None:
    """Store one check result, and open, update or resolve the alert for its source. Returns the email to send, if any. Does not commit."""
    now = now or utcnow()
    health = await db.get(SystemHealth, result.source)
    if health is None:
        health = SystemHealth(source=result.source, label=result.label, status="unknown", consecutive_failures=0)
        db.add(health)
    health.label = result.label
    health.status = "ok" if result.ok else "failing"
    health.detail = scrub(result.detail, 400)
    health.latency_ms = result.latency_ms
    health.checked_at = now
    health.consecutive_failures = 0 if result.ok else (health.consecutive_failures or 0) + 1
    if result.ok:
        health.last_ok_at = now

    live = await db.scalar(select(SystemAlert).where(SystemAlert.source == result.source, SystemAlert.status.in_(LIVE)))

    if result.ok:
        if live is None:
            return None
        live.status, live.resolved_at = "resolved", now
        subject, body = compose(RECOVERED, live, settings, now)
        return Notice(RECOVERED, live.id, subject, body)

    if not (result.definitive or health.consecutive_failures >= settings.failure_threshold):
        return None                                                    # a first blip is recorded on the dashboard but does not alert yet

    title, detail, action = scrub(result.title or f"{result.label} is failing", 200), scrub(result.detail, 400), scrub(result.action, 400)
    if live is None:
        alert = SystemAlert(source=result.source, severity=result.severity, status="open", title=title, detail=detail, action=action, first_seen_at=now, last_seen_at=now, occurrences=1, notify_count=0)
        db.add(alert)
        await db.flush()
        subject, body = compose(NEW, alert, settings, now)
        return Notice(NEW, alert.id, subject, body)

    live.last_seen_at, live.occurrences = now, (live.occurrences or 0) + 1
    live.title, live.detail, live.action = title, detail, action
    if _RANK.get(result.severity, 0) > _RANK.get(live.severity, 0):
        live.severity = result.severity
    if decide_reminder(live.status, live.last_notified_at, now, settings.reminder_hours):
        subject, body = compose(REMINDER, live, settings, now)
        return Notice(REMINDER, live.id, subject, body)
    return None


async def mark_notified(db: AsyncSession, notice: Notice, status: str, reason: str | None, now: datetime | None = None) -> None:
    """Record what happened to the email. `last_notified_at` moves only when the mail server accepted it, so a failed send is retried at the next round."""
    alert = await db.get(SystemAlert, notice.alert_id)
    if alert is None:
        return
    alert.notify_status = status if reason is None else f"{status}: {scrub(reason, 160)}"
    if status == "sent":
        alert.last_notified_at = now or utcnow()
        alert.notify_count = (alert.notify_count or 0) + 1
