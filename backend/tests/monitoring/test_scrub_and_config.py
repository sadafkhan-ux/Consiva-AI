import pytest

from app.monitoring.config import MonitoringSettings, split_addresses
from app.monitoring.scrub import scrub


@pytest.mark.parametrize("text,gone", [
    ("invalid x-api-key sk-ant-api03-AbCdEf123456789xyz", "AbCdEf123456789"),
    ("Authorization: Bearer abcdef1234567890ABCDEF", "abcdef1234567890"),
    ("api_key=supersecretvalue123 failed", "supersecretvalue123"),
    ("postgresql://user:hunter2pass@db:5432/x", "hunter2pass"),
    ("token nvapi-ZZZZ1111aaaa2222bbbb", "ZZZZ1111aaaa"),
    ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.sig_abcdef", "eyJzdWIiOiIx"),
])
def test_secrets_never_survive_scrubbing(text, gone):
    assert gone not in scrub(text)


def test_scrubbing_keeps_the_useful_part_on_one_line_and_cuts_long_text():
    out = scrub("Anthropic answered HTTP 401:\n  invalid x-api-key", 400)
    assert "\n" not in out and "HTTP 401" in out and "invalid" in out
    assert len(scrub("x" * 1000, 100)) == 100 and scrub(None) == ""


def test_addresses_are_split_cleaned_and_deduplicated():
    assert split_addresses(" a@x.com, b@y.com ;a@x.com,, ") == ["a@x.com", "b@y.com"]
    assert split_addresses("") == []


def test_cc_never_repeats_a_to_address_and_the_sender_falls_back_to_the_login():
    s = MonitoringSettings(_env_file=None, email_to="a@x.com", email_cc="a@x.com, c@x.com", smtp_user="alerts@x.com")
    assert s.to_list == ["a@x.com"] and s.cc_list == ["c@x.com"] and s.sender == "alerts@x.com"
    assert MonitoringSettings(_env_file=None, smtp_user="notanaddress").sender is None


@pytest.mark.parametrize("kwargs,expected", [
    ({"email_enabled": False}, "switched off"),
    ({}, "No recipient"),
    ({"email_to": "a@x.com"}, "No mail server"),
    ({"email_to": "a@x.com", "smtp_host": "smtp.x.com"}, "No sender"),
    ({"email_to": "a@x.com", "smtp_host": "smtp.x.com", "from_address": "alerts@x.com"}, None),
])
def test_the_dashboard_is_told_exactly_what_is_missing(kwargs, expected):
    problem = MonitoringSettings(_env_file=None, **kwargs).email_problem()
    assert (problem is None) if expected is None else (expected in problem)


@pytest.mark.parametrize("name,value", [("ALERT_CHECK_INTERVAL_SECONDS", "10"), ("ALERT_REMINDER_HOURS", "0"), ("ALERT_FAILURE_THRESHOLD", "0"), ("ALERT_SMTP_PORT", "70000")])
def test_an_out_of_range_value_stops_the_settings_from_loading(monkeypatch, name, value):
    from pydantic import ValidationError

    monkeypatch.setenv(name, value)
    with pytest.raises(ValidationError):
        MonitoringSettings(_env_file=None)
