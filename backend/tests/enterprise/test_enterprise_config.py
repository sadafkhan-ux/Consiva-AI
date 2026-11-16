import pytest
from pydantic import ValidationError

from app.enterprise.config import EnterpriseSettings


@pytest.fixture(autouse=True)
def _no_enterprise_variables_from_the_machine(monkeypatch):
    """backend/.env (loaded into the environment by the application's own settings) may switch Enterprise on; these tests are about the defaults and the validation, not this machine."""
    import os

    for name in [n for n in os.environ if n.startswith("ENTERPRISE_")]:
        monkeypatch.delenv(name)


def test_it_is_off_by_default():
    assert EnterpriseSettings(_env_file=None).enabled is False


def test_defaults_are_inside_their_own_limits():
    s = EnterpriseSettings(_env_file=None)
    assert s.max_urls > 0 and s.concurrency >= 1 and s.max_attempts >= 1 and s.claim_lease_seconds >= 30


@pytest.mark.parametrize("name,value", [
    ("ENTERPRISE_MAX_URLS", "-1"), ("ENTERPRISE_CONCURRENCY", "0"), ("ENTERPRISE_PER_DOMAIN_RPS", "0"),
    ("ENTERPRISE_CLAIM_LEASE_SECONDS", "5"), ("ENTERPRISE_MAX_ATTEMPTS", "0"), ("ENTERPRISE_MAX_DEPTH", "-2"),
])
def test_an_out_of_range_value_stops_the_settings_from_loading(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValidationError):
        EnterpriseSettings(_env_file=None)


def test_variables_are_read_with_the_enterprise_prefix_only(monkeypatch):
    monkeypatch.setenv("ENTERPRISE_ENABLED", "true")
    monkeypatch.setenv("ENTERPRISE_CONCURRENCY", "8")
    monkeypatch.setenv("CONSENT_APP_MAX_PAGES", "0")          # an existing setting is not read, and not needed
    monkeypatch.setenv("CONCURRENCY", "99")                    # an unprefixed name is ignored
    s = EnterpriseSettings(_env_file=None)
    assert s.enabled is True and s.concurrency == 8
