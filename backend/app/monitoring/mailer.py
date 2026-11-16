"""Sending the alert email. Honest by construction: "sent" is returned only when the mail server accepted the message; anything else says what is missing or what failed.

The message goes To the configured recipients with the configured CC. The SMTP password is read from the environment and never appears in a log line, an error text or a return value.
"""

from __future__ import annotations

import asyncio
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from app.monitoring.config import MonitoringSettings
from app.monitoring.scrub import scrub

SENT, NOT_CONFIGURED, FAILED = "sent", "not_configured", "failed"


def build_message(settings: MonitoringSettings, subject: str, body: str) -> EmailMessage:
    """The message, ready to send. Raises ValueError when it could not be addressed (callers check `email_problem` first)."""
    to, cc, sender = settings.to_list, settings.cc_list, settings.sender
    if not to or not sender:
        raise ValueError("a message needs a sender and at least one recipient")
    msg = EmailMessage()
    msg["Subject"] = " ".join(subject.split())[:200]              # one line: a header cannot carry a newline
    msg["From"] = sender
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=sender.split("@")[-1])
    msg["Auto-Submitted"] = "auto-generated"                      # so auto-responders do not answer it
    msg.set_content(body)
    return msg


def _send_blocking(settings: MonitoringSettings, msg: EmailMessage) -> None:
    recipients = settings.to_list + settings.cc_list
    context = ssl.create_default_context()
    if settings.smtp_ssl:
        server: smtplib.SMTP = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=settings.smtp_timeout_seconds, context=context)
    else:
        server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=settings.smtp_timeout_seconds)
    try:
        if not settings.smtp_ssl and settings.smtp_starttls:
            server.starttls(context=context)
        if settings.smtp_user and settings.smtp_password:
            server.login(settings.smtp_user, settings.smtp_password)
        server.send_message(msg, from_addr=settings.sender, to_addrs=recipients)
    finally:
        try:
            server.quit()
        except Exception:
            pass


async def send(settings: MonitoringSettings, subject: str, body: str) -> tuple[str, str | None]:
    """(status, reason). status is sent, not_configured or failed; reason is a short plain sentence for the dashboard."""
    problem = settings.email_problem()
    if problem:
        return NOT_CONFIGURED, problem
    try:
        msg = build_message(settings, subject, body)
        await asyncio.to_thread(_send_blocking, settings, msg)
    except Exception as exc:                                       # a mail failure must never take the worker down
        reason = scrub(f"{type(exc).__name__}: {exc}", 200)
        if settings.smtp_password:
            reason = reason.replace(settings.smtp_password, "[hidden]")
        return FAILED, reason
    return SENT, None
