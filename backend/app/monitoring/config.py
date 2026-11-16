"""Monitoring settings: ALERT_* variables, read from the same backend/.env as everything else.

Secrets (the SMTP password) live only in the environment, are never logged, and are never returned by the API. Out-of-range values stop the application from starting.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def split_addresses(value: str) -> list[str]:
    """'a@x.com, b@y.com' -> ['a@x.com', 'b@y.com']. Empty and duplicate entries are dropped, order is kept."""
    seen: dict[str, None] = {}
    for part in (value or "").replace(";", ",").split(","):
        address = part.strip()
        if address:
            seen.setdefault(address, None)
    return list(seen)


class MonitoringSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="ALERT_", extra="ignore")

    #: Run the automatic checks and record alerts. Turn off to stop all monitoring.
    enabled: bool = True
    #: Seconds between automatic check rounds.
    check_interval_seconds: int = Field(default=900, ge=60, le=86_400)
    #: A failure that is not clearly permanent (a timeout, a 5xx) must repeat this many rounds in a row before it becomes an alert. A rejected key is permanent: it alerts at once.
    failure_threshold: int = Field(default=2, ge=1, le=10)
    #: While an alert stays open and unacknowledged, remind every this many hours.
    reminder_hours: int = Field(default=6, ge=1, le=168)

    #: Who gets the email. Comma separated. The alert is still recorded and shown on the dashboard when this is empty.
    email_to: str = ""
    #: Copied on every email. Comma separated.
    email_cc: str = ""
    email_enabled: bool = True
    smtp_host: str | None = None
    smtp_port: int = Field(default=587, ge=1, le=65_535)
    smtp_user: str | None = None
    smtp_password: str | None = None
    #: Upgrade the connection with STARTTLS (port 587). Set smtp_ssl instead for an implicit-TLS server (port 465).
    smtp_starttls: bool = True
    smtp_ssl: bool = False
    smtp_timeout_seconds: int = Field(default=20, ge=3, le=120)
    #: The From address. Defaults to the SMTP user when that is an address.
    from_address: str | None = None
    #: Where the email points the reader. Shown as plain text.
    dashboard_url: str = "http://localhost:8100/#/system"

    @property
    def to_list(self) -> list[str]:
        return split_addresses(self.email_to)

    @property
    def cc_list(self) -> list[str]:
        return [a for a in split_addresses(self.email_cc) if a not in self.to_list]

    @property
    def sender(self) -> str | None:
        if self.from_address:
            return self.from_address
        return self.smtp_user if self.smtp_user and "@" in self.smtp_user else None

    def email_problem(self) -> str | None:
        """Why an email cannot be sent right now, in words for the dashboard; None when it can."""
        if not self.email_enabled:
            return "Email alerts are switched off (ALERT_EMAIL_ENABLED=false)."
        if not self.to_list:
            return "No recipient is set (ALERT_EMAIL_TO)."
        if not self.smtp_host:
            return "No mail server is set (ALERT_SMTP_HOST), so no email can be sent."
        if not self.sender:
            return "No sender address is set (ALERT_FROM_ADDRESS)."
        return None


@lru_cache
def get_monitoring_settings() -> MonitoringSettings:
    return MonitoringSettings()
