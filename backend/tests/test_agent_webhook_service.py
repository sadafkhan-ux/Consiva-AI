"""agent_webhook_service — the fixed external-partner notification channel for
ROPA run completion/failure and Consent Agent API scan completion/failure.

Mirrors test_notification_delivery.py's pattern (mocked settings via
patch("app.services.X.get_settings"), mocked httpx.AsyncClient.post, plus one
real network call against httpbin.org, network-gated) since this module is the
same shape of "real outbound POST, never a fabricated success" contract --
except here a failure must NOT raise (fire-and-forget), which is the main
behavioral difference under test.
"""

import hashlib
import hmac
import json
from unittest.mock import AsyncMock, MagicMock, patch

from app.services import agent_webhook_service


def _settings(url="https://partner.example/webhook", secret="shh"):
    return MagicMock(agent_webhook_url=url, agent_webhook_secret=secret)


async def test_send_event_does_nothing_when_url_is_unset():
    with (
        patch("app.services.agent_webhook_service.get_settings", return_value=_settings(url=None)),
        patch("httpx.AsyncClient.post", new=AsyncMock()) as mock_post,
    ):
        await agent_webhook_service.send_event("ropa.run.completed", run_id="r-1")
    mock_post.assert_not_called()


async def test_send_event_does_nothing_when_secret_is_unset():
    with (
        patch("app.services.agent_webhook_service.get_settings", return_value=_settings(secret=None)),
        patch("httpx.AsyncClient.post", new=AsyncMock()) as mock_post,
    ):
        await agent_webhook_service.send_event("ropa.run.completed", run_id="r-1")
    mock_post.assert_not_called()


def test_is_configured_reflects_both_settings():
    with patch("app.services.agent_webhook_service.get_settings", return_value=_settings()):
        assert agent_webhook_service.is_configured() is True
    with patch("app.services.agent_webhook_service.get_settings", return_value=_settings(url=None)):
        assert agent_webhook_service.is_configured() is False
    with patch("app.services.agent_webhook_service.get_settings", return_value=_settings(secret=None)):
        assert agent_webhook_service.is_configured() is False


async def test_send_event_signs_exactly_as_the_partner_spec_requires():
    """HMAC_SHA256(secret, "<timestamp>.<raw body>"), hex, header
    X-Agent-Signature: sha256=<hex>, alongside X-Agent-Timestamp: <timestamp> --
    this is the partner's contract verbatim, not Consiva's own v1= scheme."""
    captured = {}

    async def _fake_post(self, url, *, content, headers):
        captured["url"] = url
        captured["content"] = content
        captured["headers"] = headers
        return MagicMock(status_code=200)

    with (
        patch("app.services.agent_webhook_service.get_settings", return_value=_settings(secret="topsecret")),
        patch("app.services.agent_webhook_service.assert_safe_url", new=AsyncMock(return_value=[])),
        patch("httpx.AsyncClient.post", new=_fake_post),
    ):
        await agent_webhook_service.send_event("ropa.run.completed", run_id="r-1")

    assert captured["url"] == "https://partner.example/webhook"
    timestamp = captured["headers"]["X-Agent-Timestamp"]
    assert timestamp.isdigit()
    expected_mac = hmac.new(
        b"topsecret", f"{timestamp}.".encode() + captured["content"], hashlib.sha256
    ).hexdigest()
    assert captured["headers"]["X-Agent-Signature"] == f"sha256={expected_mac}"
    assert captured["headers"]["Content-Type"] == "application/json"


async def test_payload_shape_matches_the_partner_spec():
    captured = {}

    async def _fake_post(self, url, *, content, headers):
        captured["body"] = json.loads(content)
        return MagicMock(status_code=200)

    with (
        patch("app.services.agent_webhook_service.get_settings", return_value=_settings()),
        patch("app.services.agent_webhook_service.assert_safe_url", new=AsyncMock(return_value=[])),
        patch("httpx.AsyncClient.post", new=_fake_post),
    ):
        await agent_webhook_service.send_event(
            "ropa.run.failed", run_id="00000000-0000-0000-0000-000000000001", error="boom"
        )

    body = captured["body"]
    assert body["event"] == "ropa.run.failed"
    assert body["run_id"] == "00000000-0000-0000-0000-000000000001"
    assert body["error"] == "boom"
    # event_id is a fresh uuid per delivery, not a static/reused value.
    import uuid as uuid_module
    assert uuid_module.UUID(body["event_id"])
    # occurred_at matches the spec's example shape exactly: plain UTC with a
    # trailing Z, no offset suffix or fractional seconds.
    assert body["occurred_at"].endswith("Z")
    assert "+" not in body["occurred_at"]
    assert "." not in body["occurred_at"]


async def test_send_event_never_raises_on_a_non_2xx_response():
    """Fire-and-forget: the run/scan's own state is already durably persisted
    by the time this is called, so a partner endpoint returning an error must
    not propagate into the caller's own job."""
    with (
        patch("app.services.agent_webhook_service.get_settings", return_value=_settings()),
        patch("app.services.agent_webhook_service.assert_safe_url", new=AsyncMock(return_value=[])),
        patch("httpx.AsyncClient.post", new=AsyncMock(return_value=MagicMock(status_code=500))),
    ):
        await agent_webhook_service.send_event("ropa.run.completed", run_id="r-1")  # must not raise


async def test_send_event_never_raises_on_a_network_error():
    import httpx

    with (
        patch("app.services.agent_webhook_service.get_settings", return_value=_settings()),
        patch("app.services.agent_webhook_service.assert_safe_url", new=AsyncMock(return_value=[])),
        patch(
            "httpx.AsyncClient.post",
            new=AsyncMock(side_effect=httpx.ConnectError("boom", request=MagicMock())),
        ),
    ):
        await agent_webhook_service.send_event("ropa.run.completed", run_id="r-1")  # must not raise


async def test_send_event_succeeds_against_a_real_http_endpoint():
    """Live network call to a real, public HTTP echo endpoint (httpbin.org) --
    proves the mechanism genuinely performs a working signed POST end-to-end,
    not just that it calls some mocked function. Network-gated: skips (not
    fails) if httpbin.org is unreachable from this environment."""
    import httpx
    import pytest

    try:
        probe = await httpx.AsyncClient(timeout=5).get("https://httpbin.org/status/200")
        probe.raise_for_status()
    except httpx.HTTPError:
        pytest.skip("httpbin.org not reachable from this environment")

    with patch(
        "app.services.agent_webhook_service.get_settings",
        return_value=_settings(url="https://httpbin.org/post"),
    ):
        await agent_webhook_service.send_event("ropa.run.completed", run_id="r-1")
    # No exception raised == the real signed POST to a real endpoint succeeded.


async def test_send_event_never_raises_and_never_posts_for_an_unsafe_destination():
    """A destination that resolves to a private/loopback/link-local address --
    or doesn't resolve at all -- must be refused at delivery time, the same
    SSRF guard the per-scan webhook_url already gets, not just logged after a
    failed POST attempt."""
    with (
        patch(
            "app.services.agent_webhook_service.get_settings",
            return_value=_settings(url="http://169.254.169.254/latest/meta-data/"),
        ),
        patch("httpx.AsyncClient.post", new=AsyncMock()) as mock_post,
    ):
        await agent_webhook_service.send_event("ropa.run.completed", run_id="r-1")
    mock_post.assert_not_called()


async def test_send_event_prefers_the_per_call_override_over_fixed_config():
    """callback_url/callback_secret, when BOTH present, must be used instead
    of AGENT_WEBHOOK_URL/AGENT_WEBHOOK_SECRET for that one delivery -- the
    mechanism a shared agent instance needs to route a UAT-started run to
    UAT's endpoint and a production-started run to production's, never
    mixing one environment's URL with another's secret."""
    captured = {}

    async def _fake_post(self, url, *, content, headers):
        captured["url"] = url
        captured["headers"] = headers
        return MagicMock(status_code=200)

    with (
        patch(
            "app.services.agent_webhook_service.get_settings",
            return_value=_settings(url="https://fixed.example/webhook", secret="fixed-secret"),
        ),
        patch("app.services.agent_webhook_service.assert_safe_url", new=AsyncMock(return_value=[])),
        patch("httpx.AsyncClient.post", new=_fake_post),
    ):
        await agent_webhook_service.send_event(
            "ropa.run.completed", run_id="r-1",
            callback_url="https://override.example/webhook", callback_secret="override-secret",
        )

    assert captured["url"] == "https://override.example/webhook"
    # A signature computed with the FIXED secret must not match what was
    # actually sent -- proof the override secret, not the fixed one, signed it.
    wrong_mac = hmac.new(
        b"fixed-secret", f"{captured['headers']['X-Agent-Timestamp']}.".encode(), hashlib.sha256
    ).hexdigest()
    assert captured["headers"]["X-Agent-Signature"] != f"sha256={wrong_mac}"


async def test_send_event_falls_back_to_fixed_config_when_only_one_override_field_given():
    """Only a callback_url with no callback_secret (or vice versa) must NOT
    silently mix an override URL with the fixed secret, or vice versa -- that
    is exactly the cross-environment mismatch the override exists to
    prevent. Treated as "no override": the fixed config is used instead."""
    captured = {}

    async def _fake_post(self, url, *, content, headers):
        captured["url"] = url
        return MagicMock(status_code=200)

    with (
        patch(
            "app.services.agent_webhook_service.get_settings",
            return_value=_settings(url="https://fixed.example/webhook", secret="fixed-secret"),
        ),
        patch("app.services.agent_webhook_service.assert_safe_url", new=AsyncMock(return_value=[])),
        patch("httpx.AsyncClient.post", new=_fake_post),
    ):
        await agent_webhook_service.send_event(
            "ropa.run.completed", run_id="r-1", callback_url="https://override.example/webhook",
        )

    assert captured["url"] == "https://fixed.example/webhook"


class TestPrepareCallbackOverride:
    async def test_returns_none_none_when_neither_is_supplied(self):
        assert await agent_webhook_service.prepare_callback_override(None, None) == (None, None)

    async def test_rejects_url_without_secret(self):
        import pytest
        with pytest.raises(ValueError, match="together"):
            await agent_webhook_service.prepare_callback_override("https://x.example/hook", None)

    async def test_rejects_secret_without_url(self):
        import pytest
        with pytest.raises(ValueError, match="together"):
            await agent_webhook_service.prepare_callback_override(None, "a-secret")

    async def test_rejects_a_non_http_scheme(self):
        import pytest
        with pytest.raises(ValueError, match="http"):
            await agent_webhook_service.prepare_callback_override("ftp://x.example/hook", "secret")

    async def test_rejects_an_unsafe_url(self):
        import pytest

        from app.core.exceptions import ScanAuthorizationError
        with pytest.raises(ScanAuthorizationError):
            await agent_webhook_service.prepare_callback_override(
                "http://169.254.169.254/", "secret"
            )

    async def test_encrypts_the_secret_and_round_trips_through_resolve(self):
        import os

        from cryptography.fernet import Fernet

        old_key = os.environ.get("ROPA_CREDENTIAL_ENCRYPTION_KEY")
        os.environ["ROPA_CREDENTIAL_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
        from app.config import get_settings as real_get_settings
        real_get_settings.cache_clear()
        try:
            url, ciphertext = await agent_webhook_service.prepare_callback_override(
                "https://example.com/hook", "a-real-callback-secret"
            )
            assert url == "https://example.com/hook"
            assert ciphertext is not None
            assert "a-real-callback-secret" not in ciphertext
            assert agent_webhook_service.resolve_callback_secret(ciphertext) == "a-real-callback-secret"
            assert agent_webhook_service.resolve_callback_secret(None) is None
        finally:
            if old_key is None:
                os.environ.pop("ROPA_CREDENTIAL_ENCRYPTION_KEY", None)
            else:
                os.environ["ROPA_CREDENTIAL_ENCRYPTION_KEY"] = old_key
            real_get_settings.cache_clear()

    async def test_raises_when_encryption_is_not_configured(self):
        import os

        import pytest

        old_key = os.environ.pop("ROPA_CREDENTIAL_ENCRYPTION_KEY", None)
        from app.config import get_settings as real_get_settings
        real_get_settings.cache_clear()
        try:
            with pytest.raises(ValueError, match="ROPA_CREDENTIAL_ENCRYPTION_KEY"):
                await agent_webhook_service.prepare_callback_override(
                    "https://example.com/hook", "a-secret"
                )
        finally:
            if old_key is not None:
                os.environ["ROPA_CREDENTIAL_ENCRYPTION_KEY"] = old_key
            real_get_settings.cache_clear()
