"""When does a failing check become an alert, when does it email, and when does it stop. Uses an in-memory stand-in for the database session."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.monitoring import alerts
from app.monitoring.checks import CheckResult
from app.monitoring.config import MonitoringSettings
from app.monitoring.models import SystemAlert, SystemHealth

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)
S = MonitoringSettings(_env_file=None, failure_threshold=2, reminder_hours=6, dashboard_url="http://x/#/system")


class FakeDb:
    """Just the four session calls record_result makes."""

    def __init__(self, live=None, health=None):
        self.live, self.health, self.added = live, health, []

    async def get(self, model, key):
        return self.health

    async def scalar(self, _query):
        return self.live

    def add(self, obj):
        self.added.append(obj)
        if isinstance(obj, SystemHealth):
            self.health = obj
        if isinstance(obj, SystemAlert):
            self.live = obj

    async def flush(self):
        pass


def fail(definitive=False, severity="critical", title="Claude API key rejected (HTTP 401)"):
    return CheckResult("anthropic_api", "Claude API key", False, "Anthropic answered HTTP 401: invalid x-api-key", 120, definitive=definitive, severity=severity, title=title,
                       action="Create a new key at console.anthropic.com")


def ok():
    return CheckResult("anthropic_api", "Claude API key", True, "Claude accepted the key (HTTP 200).", 90)


def run(db, result, now=NOW):
    return asyncio.run(alerts.record_result(db, result, S, now))


def failing_health(n):
    return SystemHealth(source="anthropic_api", label="x", status="failing", consecutive_failures=n)


def live_alert(status="open", last_notified=None, occurrences=1):
    return SystemAlert(source="anthropic_api", severity="critical", status=status, title="t", detail="d", action="a", first_seen_at=NOW - timedelta(hours=10),
                       last_seen_at=NOW - timedelta(minutes=15), occurrences=occurrences, last_notified_at=last_notified, notify_count=1 if last_notified else 0)


def test_a_rejected_key_alerts_at_once_and_says_what_to_do():
    db = FakeDb()
    notice = run(db, fail(definitive=True))
    assert notice.kind == alerts.NEW and notice.subject.startswith("[Consiva] CRITICAL: Claude API key rejected")
    assert "What to do: Create a new key" in notice.body and "http://x/#/system" in notice.body and "sent automatically" in notice.body


def test_a_single_blip_is_recorded_but_does_not_alert_a_repeat_does():
    db = FakeDb()
    assert run(db, fail(severity="warning", title="Claude API is unreachable")) is None
    assert not any(isinstance(o, SystemAlert) for o in db.added)
    assert db.health.consecutive_failures == 1 and db.health.status == "failing"
    second = run(db, fail(severity="warning", title="Claude API is unreachable"))
    assert second is not None and second.kind == alerts.NEW and db.health.consecutive_failures == 2


def test_a_long_failure_is_one_alert_with_a_growing_count_not_many_rows():
    db = FakeDb(live=live_alert(last_notified=NOW - timedelta(hours=1), occurrences=4), health=failing_health(5))
    assert run(db, fail(definitive=True)) is None                       # emailed an hour ago: too soon to remind
    assert db.live.occurrences == 5 and not any(isinstance(o, SystemAlert) for o in db.added)


def test_an_open_alert_reminds_after_the_interval_and_an_acknowledged_one_does_not():
    due = run(FakeDb(live=live_alert(last_notified=NOW - timedelta(hours=7)), health=failing_health(9)), fail(definitive=True))
    assert due.kind == alerts.REMINDER and due.subject.startswith("[Consiva] STILL OPEN")
    quiet = run(FakeDb(live=live_alert(status="acknowledged", last_notified=NOW - timedelta(hours=99)), health=failing_health(9)), fail(definitive=True))
    assert quiet is None


def test_an_alert_that_was_never_emailed_is_emailed_on_the_next_round():
    """The first email failed (the mail server was down): last_notified_at stayed empty, so the next round tries again."""
    db = FakeDb(live=live_alert(last_notified=None), health=failing_health(3))
    assert run(db, fail(definitive=True)).kind == alerts.REMINDER


def test_the_alert_never_gets_less_severe_while_it_stays_open():
    db = FakeDb(live=live_alert(last_notified=NOW), health=failing_health(3))
    run(db, fail(severity="warning", title="Claude is rate limiting"))
    assert db.live.severity == "critical"


def test_when_the_check_passes_again_the_alert_is_resolved_and_one_recovery_email_is_made():
    db = FakeDb(live=live_alert(), health=failing_health(6))
    notice = run(db, ok())
    assert db.live.status == "resolved" and db.live.resolved_at == NOW
    assert db.health.consecutive_failures == 0 and db.health.last_ok_at == NOW
    assert notice.kind == alerts.RECOVERED and "RECOVERED" in notice.subject and "is fixed" in notice.body and "10 hour(s)" in notice.body


def test_a_passing_check_with_nothing_open_says_nothing():
    assert run(FakeDb(), ok()) is None


@pytest.mark.parametrize("status,last,expected", [
    ("open", None, True), ("open", NOW - timedelta(hours=5), False), ("open", NOW - timedelta(hours=6), True), ("acknowledged", None, False), ("resolved", None, False)])
def test_the_reminder_rule(status, last, expected):
    assert alerts.decide_reminder(status, last, NOW, 6) is expected


def test_secrets_in_a_failing_result_never_reach_the_stored_alert_or_the_email():
    r = fail(definitive=True)
    r.detail = "Anthropic answered HTTP 401: invalid x-api-key sk-ant-api03-REALSECRETKEYVALUE12345"
    db = FakeDb()
    notice = run(db, r)
    assert "REALSECRETKEYVALUE" not in db.live.detail
    assert "REALSECRETKEYVALUE" not in notice.body and "REALSECRETKEYVALUE" not in db.health.detail
