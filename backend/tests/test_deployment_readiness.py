"""Things that are fine in development and wrong in production.

Each of these was a real gap found while preparing the Consent Agent API for handover,
not a hypothetical. They fail loudly here rather than at 2am on the deployment:

  * Production had NO CORS configuration -- the only middleware was gated to
    app_env=="development" -- so a browser on any real origin was blocked outright and
    the integrating team could not call the API at all without editing source.
  * Outbound webhooks were delivered UNSIGNED when no signing secret was configured,
    with a log line as the only protest. Anyone who learned a customer's webhook URL
    could forge scan results into their system.
  * The development auto-login endpoint bypasses authentication entirely and must be
    unreachable anywhere else.
"""

import inspect
import re

import pytest

from app.config import Settings
from app.services import webhook_service


def _settings(**over) -> Settings:
    base = dict(
        nvidia_api_key="x", nvidia_llm_model="x", nvidia_embed_model="x",
        supabase_url="http://localhost", supabase_service_role_key="x",
        database_url="postgresql+asyncpg://u:p@localhost/db", supabase_jwt_secret="x",
    )
    base.update(over)
    return Settings(**base)


# ── CORS ────────────────────────────────────────────────────────────────────────

def test_production_cors_is_an_explicit_allow_list():
    """Checks CODE, not the file's text.

    The first version searched raw source and failed on its own explanation -- the
    comment above the middleware says why `allow_origins=["*"]` is wrong, and an
    assertion that cannot tell prose from code fails on the reasoning for the rule it
    is enforcing.
    """
    source = inspect.getsource(__import__("app.main", fromlist=["main"]))
    code = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("#")
    )
    assert "allow_origins=_settings.cors_origin_list" in code
    assert 'allow_origins=["*"]' not in code, "a credentialed API must never wildcard"
    assert 'allow_headers=["*"]' not in code.split("app_env ==")[-1].split("elif")[-1], (
        "production must list its allowed headers rather than accepting any"
    )


def test_origins_parse_from_a_comma_separated_string():
    s = _settings(cors_allowed_origins="https://consiva.ai, https://app.consiva.ai")
    assert s.cors_origin_list == ["https://consiva.ai", "https://app.consiva.ai"]


def test_no_origins_configured_means_no_cors_rather_than_everything():
    """Empty must not silently become permissive. An API with no browser client should
    not answer preflights at all."""
    assert _settings(cors_allowed_origins="").cors_origin_list == []
    assert _settings(cors_allowed_origins="  ,  ").cors_origin_list == []


def test_the_development_rule_cannot_match_a_public_origin():
    """The dev regex allows any localhost port. If it ever ran in production it would
    still not admit a real site, but the gate matters more than the regex."""
    pattern = re.compile(r"http://(localhost|127\.0\.0\.1):\d+")
    assert not pattern.fullmatch("https://evil.example")
    assert pattern.fullmatch("http://localhost:5173")


# ── Webhook signing ─────────────────────────────────────────────────────────────

async def test_an_unsigned_webhook_is_refused_not_sent(monkeypatch):
    """The whole point. Previously this logged a warning and delivered anyway."""
    from app import config

    monkeypatch.setattr(config, "get_settings", lambda: _settings(webhook_signing_secret=None))
    monkeypatch.setattr(webhook_service, "get_settings", lambda: _settings(webhook_signing_secret=None))

    sent = False

    async def _never(*a, **k):
        nonlocal sent
        sent = True
        raise AssertionError("an unsigned webhook must never reach the network")

    monkeypatch.setattr(webhook_service, "validate_target", lambda url: None)
    monkeypatch.setattr(webhook_service, "_save", _noop_save)

    import uuid
    delivered = await webhook_service.deliver(
        org_id=uuid.uuid4(), scan_id=uuid.uuid4(),
        target_url="https://receiver.example/hook",
        payload={"event": "consent_scan.completed"},
    )
    assert delivered is False
    assert not sent


async def _noop_save(record):
    _noop_save.last = record


def test_is_configured_reports_whether_webhooks_can_be_sent_at_all(monkeypatch):
    monkeypatch.setattr(webhook_service, "get_settings", lambda: _settings(webhook_signing_secret=None))
    assert webhook_service.is_configured() is False
    monkeypatch.setattr(webhook_service, "get_settings", lambda: _settings(webhook_signing_secret="s"))
    assert webhook_service.is_configured() is True


def test_the_api_rejects_a_webhook_it_could_not_sign():
    """Told at request time, not by a callback that silently never arrives."""
    from app.api.v1.routes import consent_agent
    body = inspect.getsource(consent_agent.create_scan)
    assert "webhook_service.is_configured()" in body
    assert "WebhookNotConfiguredError" in body


def test_that_refusal_is_a_server_capability_error_not_a_client_mistake():
    """501, not 400: the request is valid and the caller can do nothing about it.
    A 400 would send an integrator hunting for a bug in their own payload."""
    from app.api.v1.routes.consent_agent import WebhookNotConfiguredError
    assert WebhookNotConfiguredError.status_code == 501


def test_delivery_never_follows_redirects():
    """A 302 to an internal address is the oldest way around an SSRF check that only
    ran on the original URL."""
    assert "follow_redirects=False" in inspect.getsource(webhook_service.deliver)


def test_the_ssrf_check_runs_at_delivery_not_only_at_registration():
    """Passing once says nothing about where the name resolves minutes later."""
    assert "validate_target" in inspect.getsource(webhook_service.deliver)


# ── Authentication bypass ───────────────────────────────────────────────────────

def test_the_auto_login_router_is_only_registered_in_development():
    from app.api.v1 import router as api_router_module
    body = inspect.getsource(api_router_module)
    assert 'if get_settings().app_env == "development":' in body
    assert "api_router.include_router(dev.router)" in body


def test_the_auto_login_handler_checks_the_environment_itself():
    """Defence in depth: if the registration above is ever loosened, this still
    refuses -- and answers 404 rather than 403, so the endpoint's existence is not
    confirmed."""
    from app.api.v1.routes import dev
    body = inspect.getsource(dev.auto_login)
    assert 'settings.app_env != "development"' in body
    assert "HTTPException(status_code=404)" in body


# ── Nothing internal in an error ────────────────────────────────────────────────

def test_errors_never_carry_a_stack_trace():
    from app.main import consiva_error_handler, validation_error_handler
    for handler in (consiva_error_handler, validation_error_handler):
        body = inspect.getsource(handler)
        assert "traceback" not in body.lower()
        assert "exc_info" not in body


@pytest.mark.parametrize("field", [
    "nvidia_api_key", "groq_api_key", "database_url",
    "supabase_jwt_secret", "webhook_signing_secret",
])
def test_no_api_response_model_exposes_a_secret_setting(field):
    """The response models are the contract; a secret can only leak through one if it
    is named there."""
    from app.api.v1.schemas import consent_agent as schemas
    assert field not in inspect.getsource(schemas)


def test_the_model_path_sanitiser_is_applied_to_api_output():
    """A self-hosted model is configured by filesystem path, and that path went out in
    token_metrics.model -- publishing the inference host's directory layout and an OS
    username."""
    from app.services import consent_agent_api_service as svc
    assert "_public_model_name(meta.get(\"model\"))" in inspect.getsource(svc._token_metrics)
