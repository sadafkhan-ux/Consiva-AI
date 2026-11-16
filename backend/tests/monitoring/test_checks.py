"""Each automatic check turns a provider answer into the right result. Providers are simulated with httpx's mock transport: nothing leaves the machine."""
import asyncio

import httpx
import pytest

from app.monitoring import checks


def client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def run_anthropic(status, body="{}", key="sk-ant-test-key-0123456789"):
    async def go():
        async with client(lambda request: httpx.Response(status, text=body)) as c:
            return await checks.check_anthropic(c, key=key)
    return asyncio.run(go())


def test_an_accepted_key_is_ok():
    r = run_anthropic(200)
    assert r.ok and "accepted" in r.detail


@pytest.mark.parametrize("status", [401, 403])
def test_a_rejected_key_is_critical_and_alerts_at_once(status):
    r = run_anthropic(status, '{"error":{"message":"invalid x-api-key"}}')
    assert not r.ok and r.definitive and r.severity == "critical" and f"HTTP {status}" in r.title and "console.anthropic.com" in r.action


def test_no_credit_is_critical():
    r = run_anthropic(400, '{"error":{"message":"Your credit balance is too low"}}')
    assert not r.ok and r.definitive and "no credit" in r.title


@pytest.mark.parametrize("status", [429, 500, 503])
def test_a_rate_limit_or_a_server_error_is_a_warning_that_must_repeat_before_it_alerts(status):
    r = run_anthropic(status)
    assert not r.ok and not r.definitive and r.severity == "warning"


def test_an_unreachable_provider_is_a_warning_not_a_crash():
    def boom(request):
        raise httpx.ConnectError("no route to host")

    async def go():
        async with client(boom) as c:
            return await checks.check_anthropic(c, key="sk-ant-x-0123456789")
    r = asyncio.run(go())
    assert not r.ok and not r.definitive and "unreachable" in r.title


def test_the_key_is_sent_only_to_the_provider_and_never_appears_in_the_result():
    sent = {}

    def handler(request):
        sent["key"] = request.headers.get("x-api-key")
        return httpx.Response(401, text="invalid x-api-key sk-ant-test-key-0123456789")

    async def go():
        async with client(handler) as c:
            return await checks.check_anthropic(c, key="sk-ant-test-key-0123456789")
    r = asyncio.run(go())
    assert sent["key"] == "sk-ant-test-key-0123456789" and "0123456789" not in r.detail


def test_a_provider_with_no_key_set_is_not_an_alert():
    r = asyncio.run(checks.check_anthropic(httpx.AsyncClient(), key=""))
    assert r.ok and "Not configured" in r.detail


def test_a_check_that_crashes_is_reported_not_raised():
    async def broken():
        raise RuntimeError("boom sk-ant-api03-SECRETSECRETSECRET")

    r = asyncio.run(checks.guarded("x", "X", broken))
    assert not r.ok and "could not run" in r.detail and "SECRETSECRET" not in r.detail
