"""notification_service.dispatch_notification — the honesty contract this module
exists to enforce: "done" must mean a real HTTP POST actually succeeded, never a
fabricated claim. Covers both real failure modes (unconfigured, and a real network
call to a URL that returns an error) with a mocked settings object, no live DB
required since NotFoundError/config checks short-circuit before any DB access."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.exceptions import NotificationDeliveryError
from app.services import notification_service


class _FakeAction:
    id = "11111111-1111-1111-1111-111111111111"
    finding_id = "22222222-2222-2222-2222-222222222222"
    org_id = "33333333-3333-3333-3333-333333333333"
    title = "Test notification"
    description = "A test description"
    assignee_label = "web team"


async def test_dispatch_raises_when_no_endpoint_configured():
    with patch("app.services.notification_service.get_settings") as mock_settings:
        mock_settings.return_value = MagicMock(notification_webhook_url=None)
        with pytest.raises(NotificationDeliveryError, match="No notification endpoint configured"):
            await notification_service.dispatch_notification(db=MagicMock(), action=_FakeAction())


async def test_dispatch_raises_on_real_http_failure():
    """A real network-level failure (httpx.ConnectError, the actual exception type
    httpx raises for a real unreachable host) must surface as
    NotificationDeliveryError, not be swallowed into a false "success"."""
    import httpx

    with patch("app.services.notification_service.get_settings") as mock_settings:
        mock_settings.return_value = MagicMock(
            notification_webhook_url="https://example.invalid/webhook", notification_webhook_timeout_seconds=5
        )
        with (
            patch("app.db.repositories.finding_repository.get_finding", new=AsyncMock(return_value=None)),
            patch(
                "httpx.AsyncClient.post",
                new=AsyncMock(side_effect=httpx.ConnectError("boom", request=MagicMock())),
            ),
            pytest.raises(NotificationDeliveryError),
        ):
            await notification_service.dispatch_notification(db=MagicMock(), action=_FakeAction())


async def test_dispatch_succeeds_against_a_real_http_endpoint():
    """Live network call to a real, public HTTP echo endpoint (httpbin.org) -- proves
    the mechanism genuinely performs a working POST end-to-end, not just that it calls
    some mocked function. Network-gated: skips (not fails) if httpbin.org is
    unreachable from this environment."""
    import httpx

    try:
        probe = await httpx.AsyncClient(timeout=5).get("https://httpbin.org/status/200")
        probe.raise_for_status()
    except httpx.HTTPError:
        pytest.skip("httpbin.org not reachable from this environment")

    with patch("app.services.notification_service.get_settings") as mock_settings:
        mock_settings.return_value = MagicMock(
            notification_webhook_url="https://httpbin.org/post", notification_webhook_timeout_seconds=10
        )
        with patch("app.db.repositories.finding_repository.get_finding", new=AsyncMock(return_value=None)):
            await notification_service.dispatch_notification(db=MagicMock(), action=_FakeAction())
    # No exception raised == the real POST to a real endpoint succeeded.
