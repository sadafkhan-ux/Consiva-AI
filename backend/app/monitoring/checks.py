"""The automatic checks. Each one answers a single question about something the scanner depends on and returns a CheckResult; none of them changes anything.

`definitive` marks a failure that is plainly permanent (a rejected key, no credit, the database refusing connections): it becomes an alert at once. A failure that may be a blip
(a timeout, a 5xx, a rate limit) must repeat for `failure_threshold` rounds first, so a passing network hiccup never wakes anyone.

The Claude and NVIDIA checks call each provider's FREE model-list endpoint: they prove the key is accepted without generating text or spending tokens.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.monitoring.scrub import scrub

AUTH_ERROR_TYPES = ("AuthenticationError", "PermissionDeniedError", "PermissionError")
QUEUE_WAIT_WARN_MINUTES = 15
FAILED_SCANS_WARN = 3
LLM_WINDOW_MINUTES = 60
LLM_MIN_CALLS_FOR_RATIO = 4


@dataclass
class CheckResult:
    source: str
    label: str
    ok: bool = True
    detail: str = "ok"
    latency_ms: int | None = None
    definitive: bool = False
    severity: str = "critical"
    title: str = ""
    action: str = ""


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def classify_provider_response(source: str, label: str, name: str, status: int, body: str, latency_ms: int, *, fix: str) -> CheckResult:
    """Turn one provider answer into a result. `name` is how the provider is called in words ("Claude", "NVIDIA")."""
    if 200 <= status < 300:
        return CheckResult(source, label, True, f"{name} accepted the key (HTTP {status}).", latency_ms)
    message = scrub(body, 200)
    if status in (401, 403):
        return CheckResult(source, label, False, f"{name} answered HTTP {status}: {message}", latency_ms, definitive=True, severity="critical",
                           title=f"{name} API key rejected (HTTP {status})", action=fix)
    if status == 400 and "credit" in body.lower():
        return CheckResult(source, label, False, f"{name} says the account is out of credit: {message}", latency_ms, definitive=True, severity="critical",
                           title=f"{name} account has no credit", action=f"Add credit to the {name} account, then press Run checks now on the System health screen.")
    if status == 429:
        return CheckResult(source, label, False, f"{name} is rate limiting requests (HTTP 429).", latency_ms, severity="warning",
                           title=f"{name} is rate limiting", action="Usually passes by itself. If it keeps happening, check the account's rate limits.")
    return CheckResult(source, label, False, f"{name} answered HTTP {status}: {message}", latency_ms, severity="warning",
                       title=f"{name} API is returning errors (HTTP {status})", action=f"Check the {name} status page. The scanner falls back to the other model while this lasts.")


async def check_anthropic(client: httpx.AsyncClient, key: str | None = None) -> CheckResult:
    source, label = "anthropic_api", "Claude API key"
    key = key if key is not None else get_settings().anthropic_api_key
    if not key:
        return CheckResult(source, label, True, "Not configured (ANTHROPIC_API_KEY is empty), so Claude is not used.")
    started = time.monotonic()
    try:
        r = await client.get("https://api.anthropic.com/v1/models", params={"limit": 1}, headers={"x-api-key": key, "anthropic-version": "2023-06-01"})
    except httpx.HTTPError as exc:
        return CheckResult(source, label, False, f"Could not reach Anthropic: {scrub(type(exc).__name__ + ' ' + str(exc), 160)}", _ms(started), severity="warning",
                           title="Claude API is unreachable", action="Check the server's internet access and Anthropic's status page.")
    return classify_provider_response(
        source, label, "Claude", r.status_code, r.text, _ms(started),
        fix="Create a new key at console.anthropic.com, put it in backend/.env as ANTHROPIC_API_KEY, then restart backend and worker. Until then policy checks show as Not assessed.")


async def check_nvidia(client: httpx.AsyncClient, key: str | None = None) -> CheckResult:
    source, label = "nvidia_api", "NVIDIA API key"
    settings = get_settings()
    key = key if key is not None else settings.nvidia_api_key
    if not key:
        return CheckResult(source, label, True, "Not configured (NVIDIA_API_KEY is empty).")
    started = time.monotonic()
    try:
        r = await client.get(f"{settings.nvidia_api_base_url.rstrip('/')}/models", headers={"Authorization": f"Bearer {key}"})
    except httpx.HTTPError as exc:
        return CheckResult(source, label, False, f"Could not reach NVIDIA: {scrub(type(exc).__name__ + ' ' + str(exc), 160)}", _ms(started), severity="warning",
                           title="NVIDIA API is unreachable", action="Check the server's internet access.")
    return classify_provider_response(
        source, label, "NVIDIA", r.status_code, r.text, _ms(started),
        fix="Create a new key at build.nvidia.com, put it in backend/.env as NVIDIA_API_KEY, then restart backend and worker.")


async def check_database(db: AsyncSession) -> CheckResult:
    source, label = "database", "Database"
    started = time.monotonic()
    try:
        await db.execute(text("select 1"))
    except Exception as exc:
        return CheckResult(source, label, False, f"The database did not answer: {scrub(type(exc).__name__ + ' ' + str(exc), 160)}", _ms(started), definitive=True,
                           title="Database is not answering", action="Check that the database container is running and healthy.")
    return CheckResult(source, label, True, "The database answered.", _ms(started))


async def check_job_queue(db: AsyncSession) -> CheckResult:
    source, label = "job_queue", "Background jobs"
    row = (await db.execute(text(
        "select count(*), coalesce(extract(epoch from (now() - min(run_after))) / 60, 0) "
        "from agent_jobs where status = 'queued' and run_after <= now()"))).one()
    waiting, oldest_minutes = int(row[0]), float(row[1])
    if waiting and oldest_minutes >= QUEUE_WAIT_WARN_MINUTES:
        return CheckResult(source, label, False, f"{waiting} job(s) have been waiting up to {oldest_minutes:.0f} minutes.", severity="warning",
                           title="Background jobs are not being picked up",
                           action="Check that the worker container is running (docker compose ps worker). Restart it if it is not.")
    return CheckResult(source, label, True, f"{waiting} job(s) waiting, none stuck." if waiting else "No jobs waiting.")


async def check_llm_calls(db: AsyncSession) -> CheckResult:
    """The real calls the scanner made in the last hour. This catches what the free key check cannot: a call that is rejected, or a model that keeps returning unusable output."""
    source, label = "llm_calls", "Recent AI calls"
    rows = (await db.execute(text(
        "select provider, status, coalesce(error_type, ''), count(*) from llm_usage_events "
        f"where created_at > now() - interval '{LLM_WINDOW_MINUTES} minutes' group by 1, 2, 3"))).all()
    total = sum(int(r[3]) for r in rows)
    failed = [(r[0], r[2], int(r[3])) for r in rows if r[1] != "success"]
    auth = [f for f in failed if f[1] in AUTH_ERROR_TYPES]
    if auth:
        provider, kind, _n = auth[0]
        return CheckResult(source, label, False, f"{sum(a[2] for a in auth)} call(s) to {provider} were rejected ({kind}) in the last hour.", definitive=True,
                           title=f"AI calls to {provider} are being rejected",
                           action="The key is not accepted. See the key check above, replace the key in backend/.env and restart backend and worker.")
    bad = sum(f[2] for f in failed)
    if total >= LLM_MIN_CALLS_FOR_RATIO and bad / total >= 0.5:
        kinds = ", ".join(f"{p} {k or 'error'} x{n}" for p, k, n in failed[:3])
        return CheckResult(source, label, False, f"{bad} of {total} AI calls failed in the last hour ({kinds}).", severity="warning",
                           title="Many AI calls are failing",
                           action="Reports may be missing analysis. Check the provider status and the usage panel for the error type.")
    return CheckResult(source, label, True, f"{total} AI call(s) in the last hour, {bad} failed." if total else "No AI calls in the last hour.")


async def check_scan_failures(db: AsyncSession) -> CheckResult:
    source, label = "scan_failures", "Scan failures"
    failed = int((await db.execute(text(
        "select count(*) from consent_scans where status = 'failed' and created_at > now() - interval '60 minutes'"))).scalar() or 0)
    if failed >= FAILED_SCANS_WARN:
        return CheckResult(source, label, False, f"{failed} scans failed in the last hour.", severity="warning", title="Several scans failed in the last hour",
                           action="Open Recent scans and read the failure reason of the newest failed scan (a blocked site, a timeout or a service problem).")
    return CheckResult(source, label, True, f"{failed} scan(s) failed in the last hour.")


async def guarded(source: str, label: str, call: Callable[[], Awaitable[CheckResult]]) -> CheckResult:
    """A check that itself crashes is reported as a failing check, never as an exception that stops the others."""
    try:
        return await call()
    except Exception as exc:
        return CheckResult(source, label, False, f"The check could not run: {scrub(type(exc).__name__ + ' ' + str(exc), 160)}", severity="warning",
                           title=f"The {label} check could not run", action="Look at the worker log for the error.")
