"""Fixes from the 2026-10-26 code audit: each test pins the behaviour that was wrong."""

from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.agents.dsr.connectors import factory as dsr_factory
from app.agents.dsr.connectors.authorization import SourceNotAuthorizedError
from app.agents.regwatch.services import collection_service
from app.agents.ropa.connectors import factory as ropa_factory
from app.agents.ropa.connectors.base import ConnectorError
from app.config import get_settings
from app.core.credential_refs import CredentialRefNotAllowedError, assert_credential_ref_allowed
from app.jobs import queue
from app.llm import usage_tracking
from app.scanner.url_normalize import normalize_url
from app.services import consent_agent_api_service as api_service
from app.services.review_service import InvalidEditError, _validate_edited_payload

# ── a source cannot name the platform's own secrets ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", ["DATABASE_URL", "CONSIVA_JWT_SECRET", "SUPABASE_JWT_SECRET", "anthropic_api_key", "NVIDIA_API_KEY", "ROPA_CREDENTIAL_ENCRYPTION_KEY",
                                  "WEBHOOK_SIGNING_SECRET", "POSTGRES_PASSWORD", "PGPASSWORD", "WORKER_DATABASE_URL"])
def test_a_variable_the_platform_uses_itself_cannot_be_a_credential_ref(name):
    with pytest.raises(CredentialRefNotAllowedError):
        assert_credential_ref_allowed(name)


@pytest.mark.parametrize("name", ["PREPMYEVENT_DB_PASSWORD", "REGWATCH_MEITY_TOKEN", "CRM_READONLY_PASSWORD"])
def test_a_variable_made_for_a_source_is_still_allowed(name):
    assert assert_credential_ref_allowed(name) == name


def test_the_required_prefix_setting_turns_the_rule_into_an_allowlist():
    settings = get_settings().model_copy(update={"credential_ref_required_prefix": "SRC_"})
    assert assert_credential_ref_allowed("SRC_CRM_PASSWORD", settings=settings) == "SRC_CRM_PASSWORD"
    with pytest.raises(CredentialRefNotAllowedError):
        assert_credential_ref_allowed("PREPMYEVENT_DB_PASSWORD", settings=settings)


def test_the_error_never_repeats_the_name_it_refused():
    with pytest.raises(CredentialRefNotAllowedError) as err:
        assert_credential_ref_allowed("DATABASE_URL")
    assert "DATABASE_URL" not in str(err.value)


def test_no_connector_reads_a_platform_secret_from_the_environment(monkeypatch):
    """The three places that read a credential_ref from the environment all refuse a platform variable BEFORE reading it."""
    monkeypatch.setenv("CONSIVA_JWT_SECRET", "must-never-be-returned")
    with pytest.raises(ConnectorError):
        ropa_factory._resolve_secret("CONSIVA_JWT_SECRET")
    with pytest.raises(SourceNotAuthorizedError):
        dsr_factory._resolve_secret("CONSIVA_JWT_SECRET", purpose="read")
    with pytest.raises(collection_service.SourceNotAuthorizedError):
        collection_service._resolve_credential(SimpleNamespace(credential_ref="CONSIVA_JWT_SECRET"))


def test_a_source_credential_still_resolves_for_a_variable_made_for_it(monkeypatch):
    monkeypatch.setenv("CRM_READONLY_PASSWORD", "s3cret")
    assert ropa_factory._resolve_secret("CRM_READONLY_PASSWORD") == "s3cret"
    assert dsr_factory._resolve_secret("CRM_READONLY_PASSWORD", purpose="read") == "s3cret"


# ── one bad link must not fail a scan ───────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("url", ["https://example.com:99999/x", "https://example.com:abc/x"])
def test_a_link_with_a_malformed_port_is_returned_as_it_came_not_raised(url):
    assert normalize_url(url) == url


def test_a_normal_link_is_still_normalised():
    assert normalize_url("https://www.example.com:443/a?utm_source=x&b=2&a=1#top") == "https://example.com/a?a=1&b=2"


# ── an API scan is not reaped as a lost job while it is still running ───────────────────────────────────────────────────────────

def _job(job_type, profile=None):
    return SimpleNamespace(job_type=job_type, payload={"scan_profile": profile} if profile else {})


def test_an_application_scan_run_through_the_api_gets_the_long_lease():
    chain = queue.stale_threshold_for(_job("consent_api_chain", "application"))
    assert chain >= timedelta(hours=24)
    assert chain >= queue.stale_threshold_for(_job("scan", "application"))


def test_a_web_chain_job_covers_its_crawl_and_the_analysis_after_it():
    chain = queue.stale_threshold_for(_job("consent_api_chain", "web"))
    assert chain > queue.stale_threshold_for(_job("scan", "web"))
    assert queue.stale_threshold_for(_job("dsr_search")) == queue.stale_threshold_for(_job("anything_else"))


# ── a reviewer's cookie correction ──────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("payload", [
    {"reclassify_cookie": "sessionid"},
    {"reclassify_cookie": {"category": "marketing"}},
    {"reclassify_cookie": {"name_pattern": "  ", "category": "marketing"}},
    {"reclassify_cookie": {"name_pattern": "sessionid", "category": "banana"}},
    {"reclassify_cookie": {"name_pattern": "x" * 201, "category": "analytics"}},
    {"reclassify_cookie": {"name_pattern": "sessionid", "category": "analytics", "vendor": 5}},
])
def test_a_malformed_cookie_correction_is_refused_before_anything_is_written(payload):
    with pytest.raises(InvalidEditError):
        _validate_edited_payload(payload)


def test_a_well_formed_cookie_correction_passes_validation():
    _validate_edited_payload({"reclassify_cookie": {"name_pattern": "sessionid", "category": "functional", "vendor": "Acme"}})


# ── token figures ───────────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_openai_shaped_usage_does_not_count_cached_tokens_twice():
    s = usage_tracking.summarise_usage([{"prompt_tokens": 1000, "completion_tokens": 100, "prompt_tokens_details": {"cached_tokens": 400}}])
    assert (s["input_tokens"], s["cache_read_tokens"], s["output_tokens"]) == (600, 400, 100)


def test_anthropic_shaped_usage_is_unchanged():
    s = usage_tracking.summarise_usage([{"input_tokens": 600, "output_tokens": 100, "cache_read_input_tokens": 400}])
    assert (s["input_tokens"], s["cache_read_tokens"]) == (600, 400)


def _stage(attempts, provider="claude"):
    return SimpleNamespace(stage="llm_analysis", stage_metadata={"llm_usage": {"usage_by_attempt": attempts}, "provider": provider, "model": "m"})


def test_the_integration_api_reports_claude_usage_instead_of_zero():
    m = api_service._token_metrics([_stage([{"input_tokens": 5000, "output_tokens": 900}])])
    assert (m["input_tokens"], m["output_tokens"], m["total_tokens"]) == (5000, 900, 5900)


def test_every_analysis_attempt_is_counted():
    m = api_service._token_metrics([_stage([{"input_tokens": 100, "output_tokens": 10}]), _stage([{"input_tokens": 200, "output_tokens": 20}])])
    assert (m["llm_calls"], m["total_tokens"]) == (2, 330)


def test_openai_shaped_usage_still_reported_by_the_integration_api():
    m = api_service._token_metrics([_stage([{"prompt_tokens": 70, "completion_tokens": 30, "total_tokens": 100}], provider="nvidia")])
    assert (m["input_tokens"], m["output_tokens"], m["total_tokens"]) == (70, 30, 100)
