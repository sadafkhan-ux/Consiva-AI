import asyncio

import pytest

from app.monitoring import mailer
from app.monitoring.config import MonitoringSettings


def settings(**kw):
    base = dict(email_to="sadaf@x.com", email_cc="boss@x.com", smtp_host="smtp.x.com", from_address="alerts@x.com")
    return MonitoringSettings(_env_file=None, **{**base, **kw})


def test_the_message_is_addressed_to_the_recipient_with_the_copy_and_marked_automatic():
    msg = mailer.build_message(settings(), "[Consiva] CRITICAL: key rejected", "body")
    assert msg["To"] == "sadaf@x.com" and msg["Cc"] == "boss@x.com" and msg["From"] == "alerts@x.com"
    assert msg["Auto-Submitted"] == "auto-generated" and msg.get_content().strip() == "body"


def test_a_subject_can_never_carry_a_second_header():
    msg = mailer.build_message(settings(), "title\r\nBcc: attacker@evil.com", "b")
    assert "\n" not in msg["Subject"] and "\r" not in msg["Subject"] and msg["Bcc"] is None


def test_no_copy_header_when_there_is_no_copy():
    assert mailer.build_message(settings(email_cc=""), "s", "b")["Cc"] is None


def test_an_unaddressable_message_is_refused():
    with pytest.raises(ValueError):
        mailer.build_message(MonitoringSettings(_env_file=None), "s", "b")


def test_nothing_is_claimed_sent_when_the_mail_setup_is_incomplete():
    status, reason = asyncio.run(mailer.send(settings(smtp_host=None), "s", "b"))
    assert status == mailer.NOT_CONFIGURED and "mail server" in reason


def test_sent_means_the_mail_server_accepted_it_and_it_reaches_to_and_cc(monkeypatch):
    seen = {}

    def fake(s, msg):
        seen["to"] = s.to_list + s.cc_list

    monkeypatch.setattr(mailer, "_send_blocking", fake)
    assert asyncio.run(mailer.send(settings(), "s", "b")) == (mailer.SENT, None)
    assert seen["to"] == ["sadaf@x.com", "boss@x.com"]


def test_a_failed_send_is_reported_without_the_password(monkeypatch):
    def boom(s, msg):
        raise RuntimeError("535 login failed for password hunter2secret")

    monkeypatch.setattr(mailer, "_send_blocking", boom)
    status, reason = asyncio.run(mailer.send(settings(smtp_user="u", smtp_password="hunter2secret"), "s", "b"))
    assert status == mailer.FAILED and "hunter2secret" not in reason and "login failed" in reason
