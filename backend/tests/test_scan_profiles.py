"""Scan profiles: public web scans are capped at 5 pages, application scans have no fixed
page-count cap -- decided by the server, on one shared engine.

Every property below is one the requirement states, held against the real code with synthetic
sites and fakes (no real browser, network, or database). The properties, in the order the
requirement lists them:

  * the web profile stops at 5 unique pages; the application profile can scan more
  * a caller cannot widen the web limit by asking for more
  * a caller who is not authorised cannot reach the application profile
  * invalid configuration fails at startup, and a MISSING value never means "unlimited"
  * "no page cap" is still bounded: a deadline, cancellation, same-site, de-duplication
  * existing behaviour -- timeouts, cancellation, SSRF -- still works
"""

import asyncio
import os
import subprocess
import time
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from app.api.v1.routes import consent_agent as route
from app.api.v1.schemas.consent_agent import CreateScanRequest, ScanOptions
from app.config import Settings, get_settings
from app.core import integration_auth
from app.core.exceptions import ScanAuthorizationError, ScanTimeoutError
from app.core.security import CurrentUser
from app.jobs import queue
from app.scanner import crawler, run_single_scan, scan_profiles
from app.scanner.scan_profiles import APPLICATION, WEB, resolve_profile
from app.services import scan_service

from tests.test_error_modes_db_partial import _fake_scan_result
from tests.test_scanner_coverage import ROOT, _FakeContext, _crawl, _links

ORG = uuid.uuid4()
USER = uuid.uuid4()


def _settings(**overrides) -> Settings:
    return get_settings().model_copy(update=overrides)


# ── Configuration: invalid values fail safely, missing values never mean "unlimited" ─

@pytest.mark.parametrize("name,value", [
    ("consent_web_max_pages", 0),             # a zero web limit would read as "none"
    ("consent_web_max_pages", -3),
    ("consent_web_max_pages", "abc"),
    ("consent_web_max_pages", None),
    ("consent_app_max_pages", -1),            # 0 is the documented "no cap"; negatives are not
    ("consent_app_max_pages", "lots"),
    ("consent_app_max_seconds", 1),           # 0 means "no deadline"; 1..59 is neither
    ("consent_app_max_seconds", -60),
    ("consent_app_max_seconds", 59),
    ("consent_app_max_seconds", 10**9),
])
def test_invalid_scan_profile_configuration_is_rejected_when_settings_load(name, value):
    with pytest.raises(ValidationError):
        Settings.model_validate({**get_settings().model_dump(), name: value})


@pytest.mark.parametrize("variable", [
    "CONSENT_WEB_MAX_PAGES", "CONSENT_APP_MAX_PAGES", "CONSENT_APP_MAX_SECONDS",
])
@pytest.mark.parametrize("bad", ["", "abc", "-1", "0.5", "none", "unlimited"])
def test_a_bad_environment_value_stops_the_app_from_starting(monkeypatch, variable, bad):
    """Through the real environment path, not by constructing the model directly: an empty or
    mistyped variable must be an error, never a silently removed limit.

    (`0` for CONSENT_APP_MAX_PAGES is valid -- it is the documented "no cap" -- and
    `-1` for CONSENT_APP_MAX_SECONDS etc. is covered by the bounds above.)"""
    monkeypatch.setenv(variable, bad)
    with pytest.raises(ValidationError):
        Settings()


def test_missing_variables_take_the_documented_defaults_not_unlimited(monkeypatch):
    for variable in ("CONSENT_WEB_MAX_PAGES", "CONSENT_APP_MAX_PAGES", "CONSENT_APP_MAX_SECONDS"):
        monkeypatch.delenv(variable, raising=False)
    # Settings() also reads backend/.env; pass the three fields explicitly absent by building from
    # the class defaults rather than the file.
    fields = Settings.model_fields
    assert fields["consent_web_max_pages"].default == 3
    assert fields["consent_app_max_pages"].default == 0
    assert fields["consent_app_max_seconds"].default == 0, "no crawl deadline unless one is set"
    assert resolve_profile(WEB, Settings.model_validate(
        {**get_settings().model_dump(), "consent_web_max_pages": 3}
    )).max_pages == 3


def test_the_example_env_documents_all_three_variables_without_secrets():
    from pathlib import Path

    text = (Path(__file__).resolve().parent.parent / ".env.example").read_text(encoding="utf-8")
    for line in ("CONSENT_WEB_MAX_PAGES=3", "CONSENT_APP_MAX_PAGES=0", "CONSENT_APP_MAX_SECONDS=0"):
        assert line in text
    block = text[text.index("Consent scan profiles"):text.index("CONSENT_APP_MAX_SECONDS=")]
    assert "no fixed page-count cap" in block, "0 must be documented as 'no fixed page-count cap'"
    assert "key" not in block.lower().replace("keyword", "") or "KEY=" not in block


# ── Profile resolution ──────────────────────────────────────────────────────────

def test_the_web_profile_is_capped_and_the_application_profile_is_not():
    settings = _settings(consent_web_max_pages=5, consent_app_max_pages=0)
    web, app = resolve_profile(WEB, settings), resolve_profile(APPLICATION, settings)

    assert web.max_pages == 5 and not web.requires_verified_domain
    assert app.max_pages is None and app.requires_verified_domain
    assert app.page_cap_label == "uncapped" and web.page_cap_label == "5"


def test_an_application_deployment_may_still_choose_an_explicit_cap():
    assert resolve_profile(APPLICATION, _settings(consent_app_max_pages=40)).max_pages == 40


@pytest.mark.parametrize("name", [None, "", "web", "WEB", "Application", "APPLICATION", "app",
                                  "admin", "unlimited", 1, ["application"], {"profile": "application"}])
def test_anything_but_exactly_application_resolves_to_the_small_web_profile(name):
    """Failing small: a typo, a null or a tampered value can only land on the capped profile."""
    profile = resolve_profile(name, _settings(consent_web_max_pages=5))
    assert profile.name == WEB and profile.max_pages == 5


def test_a_request_can_only_lower_a_page_limit_never_raise_it():
    web = resolve_profile(WEB, _settings(consent_web_max_pages=5))
    assert web.effective_max_pages(None) == 5
    assert web.effective_max_pages(3) == 3
    assert web.effective_max_pages(5) == 5
    assert web.effective_max_pages(6) == 5
    assert web.effective_max_pages(100) == 5

    app = resolve_profile(APPLICATION, _settings(consent_app_max_pages=0))
    assert app.effective_max_pages(None) is None
    assert app.effective_max_pages(40) == 40, "a caller may voluntarily scan LESS than 'no cap'"

    capped_app = resolve_profile(APPLICATION, _settings(consent_app_max_pages=40))
    assert capped_app.effective_max_pages(500) == 40


def test_an_application_profile_with_a_deadline_has_a_finite_one_and_the_hard_timeout_outlasts_the_crawl():
    settings = _settings(consent_web_max_pages=5, consent_app_max_seconds=3600, scanner_timeout_seconds=30)
    profile = resolve_profile(APPLICATION, settings)

    assert 0 < profile.crawl_deadline_seconds < float("inf")
    assert profile.hard_timeout_seconds > profile.crawl_deadline_seconds
    assert profile.crawl_deadline_seconds == 3600


def test_an_application_scan_has_no_crawl_deadline_by_default():
    profile = resolve_profile(APPLICATION, _settings(consent_app_max_seconds=0))

    assert profile.crawl_deadline_seconds is None
    assert profile.as_options()["max_seconds"] is None, "stored and returned as 'no deadline', not 0"


def test_with_no_crawl_deadline_the_whole_scan_still_has_a_finite_ceiling():
    from app.scanner.scan_profiles import _APP_HARD_CEILING_SECONDS

    profile = resolve_profile(APPLICATION, _settings(consent_app_max_seconds=0))

    assert profile.hard_timeout_seconds == _APP_HARD_CEILING_SECONDS == 86_400
    assert 0 < profile.hard_timeout_seconds < float("inf"), (
        "the outer timeout and the job lease both need a finite number, or a hung scan holds a worker forever"
    )


def test_the_web_profile_has_no_crawl_deadline_and_outlasts_the_old_330_second_limit():
    """A slow site (a real one took longer than 330 s) used to be aborted at "5 pages x 30 s + 2 x 30 s
    + 120 s", then retried, then aborted again. The web crawl is bounded by its page limit instead."""
    web = resolve_profile(WEB, _settings(consent_web_max_pages=5, scanner_timeout_seconds=30))

    assert web.crawl_deadline_seconds is None
    assert web.as_options()["max_seconds"] is None
    assert web.max_pages == 5, "removing the time bound must not touch the page bound"
    assert web.hard_timeout_seconds == 1_800 > 330
    assert 0 < web.hard_timeout_seconds < float("inf"), "a hung scan must still be aborted eventually"


def test_the_web_ceiling_is_a_fixed_number_not_derived_from_the_page_limit():
    few = resolve_profile(WEB, _settings(consent_web_max_pages=1))
    many = resolve_profile(WEB, _settings(consent_web_max_pages=25))

    assert few.hard_timeout_seconds == many.hard_timeout_seconds == 1_800


def test_an_explicit_deadline_still_works_for_deployments_that_want_one():
    profile = resolve_profile(APPLICATION, _settings(consent_app_max_seconds=1800))

    assert profile.crawl_deadline_seconds == 1800
    assert profile.hard_timeout_seconds < 86_400


@pytest.mark.parametrize("seconds", [0, 60, 3600, 86_400])
def test_valid_application_deadline_values_load(seconds):
    loaded = Settings.model_validate({**get_settings().model_dump(), "consent_app_max_seconds": seconds})
    assert loaded.consent_app_max_seconds == seconds


@pytest.mark.parametrize("options,expected", [
    (None, WEB), ({}, WEB), ({"max_pages": 25}, WEB), ({"profile": "web"}, WEB),
    ({"profile": "application"}, APPLICATION), ("application", WEB), (["application"], WEB),
    ({"profile": "Application"}, WEB),
])
def test_a_stored_scan_runs_under_the_profile_the_server_wrote(options, expected):
    assert scan_profiles.profile_for_scan_options(options).name == expected


# ── The crawl: 5 unique pages vs more, and what stays bounded ───────────────────

def _site(n: int) -> dict:
    pages = {f"{ROOT}/": _links(*[f"/p{i}" for i in range(n)])}
    pages.update({f"{ROOT}/p{i}": _links() for i in range(n)})
    return pages


async def test_the_web_profile_stops_at_three_unique_pages():
    context = _FakeContext(_site(40))
    profile = resolve_profile(WEB, _settings(consent_web_max_pages=3))

    *_rest, diagnostics = await _crawl_with(context, profile)

    assert diagnostics["pages_attempted"] == 3 and len(context.navigations) == 3
    assert len(set(context.navigations)) == 3, "three UNIQUE pages"
    assert diagnostics["stop_reason"] == "page_limit"
    assert diagnostics["pages_not_attempted_budget_exhausted"] > 0
    assert diagnostics["scan_profile"] == WEB and diagnostics["page_limit"] == 3


async def test_the_web_limit_holds_even_when_the_deployment_wide_limit_is_enormous():
    """The old global limit must not leak into the web profile."""
    context = _FakeContext(_site(40))
    profile = resolve_profile(WEB, _settings(consent_web_max_pages=3))

    *_rest, diagnostics = await _crawl_with(context, profile, settings=_settings(scanner_max_pages=500))

    assert diagnostics["pages_attempted"] == 3


async def test_the_application_profile_scans_more_than_five_pages_with_no_fixed_cap():
    context = _FakeContext(_site(60))
    profile = resolve_profile(APPLICATION, _settings(consent_app_max_pages=0, consent_app_max_seconds=3600))

    records, *_rest, diagnostics = await _crawl_with(context, profile)

    assert diagnostics["pages_attempted"] == 61 and diagnostics["pages_scanned"] == 61
    assert len(context.navigations) == 61 > 5
    assert diagnostics["page_limit"] is None and diagnostics["page_limit_mode"] == "uncapped"
    assert diagnostics["stop_reason"] == "queue_exhausted", "it ended because the site ran out, not a cap"
    assert len([p for p in records if p.discovered_via in ("seed", "link")]) == 61


async def test_an_explicit_application_cap_is_honoured():
    context = _FakeContext(_site(60))
    profile = resolve_profile(APPLICATION, _settings(consent_app_max_pages=12))

    *_rest, diagnostics = await _crawl_with(context, profile)

    assert diagnostics["pages_attempted"] == 12 and diagnostics["stop_reason"] == "page_limit"


async def test_without_a_profile_the_historical_deployment_wide_limit_still_applies():
    """Direct callers of the crawler (and every existing test) are unchanged."""
    context = _FakeContext(_site(40))

    *_rest, diagnostics = await _crawl(context, _settings(scanner_max_pages=7))   # above the ceiling: clamped

    assert diagnostics["pages_attempted"] == 3 and diagnostics["scan_profile"] is None


class _SlowContext(_FakeContext):
    """Every page takes real time to load, so a wall-clock deadline can fire mid-crawl."""

    def __init__(self, pages, delay):
        super().__init__(pages)
        self._delay = delay

    async def new_page(self):
        page = await super().new_page()
        original = page.goto

        async def slow_goto(url, timeout=None, wait_until=None):
            await asyncio.sleep(self._delay)
            return await original(url, timeout=timeout, wait_until=wait_until)

        page.goto = slow_goto
        return page


async def test_an_uncapped_scan_is_still_bounded_by_its_deadline():
    """THE safety property: no page cap must not mean no end."""
    context = _SlowContext(_site(200), delay=0.05)
    profile = resolve_profile(APPLICATION, _settings())
    started = time.monotonic()

    records, *_rest, diagnostics = await _crawl_with(
        context, profile, deadline=time.monotonic() + 0.25,
    )

    assert diagnostics["stop_reason"] == "deadline"
    assert 0 < diagnostics["pages_attempted"] < 201, "it did real work, then stopped"
    assert diagnostics["pages_not_attempted_deadline_exceeded"] > 0
    assert diagnostics["pages_not_attempted_budget_exhausted"] == 0
    assert time.monotonic() - started < 5, "it stopped promptly instead of finishing 200 pages"
    # ...and what it did collect is kept, not thrown away.
    assert len(records) == diagnostics["pages_attempted"]


async def test_a_deadline_never_cuts_a_batch_in_half():
    context = _SlowContext(_site(40), delay=0.05)
    profile = resolve_profile(APPLICATION, _settings())

    records, *_rest, diagnostics = await _crawl_with(context, profile, deadline=time.monotonic() + 0.12)

    # Every page that was started finished and was recorded: nothing half-fetched, nothing
    # failed or timed out because a deadline interrupted it.
    assert diagnostics["pages_attempted"] == len(records) == diagnostics["pages_scanned"] > 0
    assert diagnostics["pages_failed"] == 0 and diagnostics["pages_timeout"] == 0
    assert diagnostics["stop_reason"] == "deadline"


async def test_cancellation_stops_the_crawl_between_batches_and_keeps_the_evidence():
    context = _FakeContext(_site(200))
    profile = resolve_profile(APPLICATION, _settings())
    polls = {"n": 0}

    async def should_stop():
        polls["n"] += 1
        return polls["n"] > 3          # let three batches through, then cancel

    records, *_rest, diagnostics = await _crawl_with(context, profile, should_stop=should_stop)

    assert diagnostics["stop_reason"] == "cancelled"
    # Batch 1 is the seed alone; batches 2 and 3 are four concurrent pages each.
    assert diagnostics["pages_attempted"] == 9
    assert len(records) == 9, "evidence gathered before the cancel is kept"
    assert diagnostics["pages_attempted"] < 201


async def test_an_uncapped_scan_still_refuses_other_sites_and_counts_spellings_once():
    pages = {
        f"{ROOT}/": _links(
            "/a", "/a/", "/a#top", "/a?utm_source=x",           # four spellings of one page
            "https://evil.example.org/steal", "http://other.test/x",  # other sites
            "/b",
        ),
        f"{ROOT}/a": _links(), f"{ROOT}/b": _links(),
    }
    context = _FakeContext(pages)
    profile = resolve_profile(APPLICATION, _settings())

    *_rest, diagnostics = await _crawl_with(context, profile)

    assert diagnostics["pages_attempted"] == 3          # /, /a, /b
    assert all(url.startswith(ROOT) for url in context.navigations), "no off-site navigation"
    assert diagnostics["pages_skipped_duplicate"] >= 3


async def _crawl_with(context, profile, *, settings=None, deadline=None, should_stop=None):
    return await crawler._crawl_full_site(
        context, ROOT, settings or get_settings(), set(),
        profile=profile, deadline_monotonic=deadline, should_stop=should_stop,
    )


# ── The runner: one engine, profile and cancellation passed through ──────────────

async def test_the_runner_passes_the_profile_and_cancel_check_to_the_engine(monkeypatch):
    seen = {}

    async def fake_run_scan(root_url, settings=None, profile=None, should_stop=None):
        seen.update(profile=profile, should_stop=should_stop)
        return _fake_scan_result()

    monkeypatch.setattr(crawler.sys, "platform", "linux")
    monkeypatch.setattr(crawler, "run_scan", fake_run_scan)
    cancel = AsyncMock(return_value=False)

    await crawler.run_scan_isolated("https://example.com", profile=APPLICATION, should_cancel=cancel)

    assert seen["profile"].name == APPLICATION and seen["profile"].max_pages is None
    assert seen["should_stop"] is cancel


async def test_a_scan_with_no_profile_calls_the_engine_exactly_as_before(monkeypatch):
    calls = []

    async def legacy_run_scan(root_url, settings=None):       # the pre-profile signature
        calls.append((root_url, settings))
        return _fake_scan_result()

    monkeypatch.setattr(crawler.sys, "platform", "linux")
    monkeypatch.setattr(crawler, "run_scan", legacy_run_scan)

    await crawler.run_scan_isolated("https://example.com")

    assert calls == [("https://example.com", None)]


async def test_the_overall_timeout_still_aborts_a_hung_profile_scan(monkeypatch):
    async def never_returns(root_url, settings=None, profile=None, should_stop=None):
        await asyncio.sleep(60)

    monkeypatch.setattr(crawler.sys, "platform", "linux")
    monkeypatch.setattr(crawler, "run_scan", never_returns)
    tiny = scan_profiles.ScanProfile(
        name=APPLICATION, max_pages=None, crawl_deadline_seconds=0.01,
        hard_timeout_seconds=0.05, requires_verified_domain=True,
    )

    with pytest.raises(ScanTimeoutError, match="exceeded its"):
        await crawler.run_scan_isolated("https://example.com", profile=tiny)


async def test_the_windows_child_gets_the_profile_name_only_and_is_signalled_to_cancel(monkeypatch):
    """No page count or deadline crosses the process boundary -- only the NAME -- and a cancel
    reaches the child as a file, because that process has no database."""
    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        cancel_file = kw["env"][crawler.CANCEL_FILE_ENV]
        for _ in range(200):                                  # child "crawls" until told to stop
            if os.path.exists(cancel_file):
                captured["saw_cancel_file"] = True
                break
            time.sleep(0.02)
        return subprocess.CompletedProcess(cmd, 0, _fake_scan_result().model_dump_json(), "")

    monkeypatch.setattr(crawler.sys, "platform", "win32")
    monkeypatch.setattr(crawler.subprocess, "run", fake_run)
    monkeypatch.setattr(crawler, "CANCEL_POLL_SECONDS", 0.05)
    cancelled = AsyncMock(return_value=True)

    result = await crawler.run_scan_isolated(
        "https://example.com", profile=APPLICATION, should_cancel=cancelled,
    )

    assert result.root_url == "https://example.com/"
    assert captured["cmd"][-2:] == ["https://example.com", APPLICATION]
    assert not any(part.isdigit() for part in captured["cmd"][3:]), "no number is passed on argv"
    assert captured.get("saw_cancel_file") is True, "the cancel signal reached the child"


async def test_the_child_process_stops_when_the_cancel_file_appears(monkeypatch, tmp_path):
    cancel_file = tmp_path / "cancel"
    monkeypatch.setenv(crawler.CANCEL_FILE_ENV, str(cancel_file))
    seen = {}

    async def fake_run_scan(url, settings=None, profile=None, should_stop=None):
        seen["profile"] = profile
        seen["before"] = await should_stop()
        cancel_file.write_text("cancel")
        seen["after"] = await should_stop()
        return _fake_scan_result()

    monkeypatch.setattr(run_single_scan, "run_scan", fake_run_scan)
    monkeypatch.setattr(run_single_scan.sys, "stdout", MagicMock())

    await run_single_scan._main("https://example.com", APPLICATION)

    assert seen["profile"].name == APPLICATION
    assert (seen["before"], seen["after"]) == (False, True)


@pytest.mark.parametrize("extra", [["unlimited"], ["APPLICATION"], ["web", "extra"], ["100"]])
def test_the_child_entry_point_refuses_anything_but_a_known_profile_name(extra):
    """A real invocation of the child with a bad profile argument exits 2 before scanning."""
    import sys
    from pathlib import Path

    backend = Path(__file__).resolve().parent.parent
    done = subprocess.run(
        [sys.executable, "-m", "app.scanner.run_single_scan", "https://a.test", *extra],
        cwd=str(backend), capture_output=True, text=True, timeout=60,
        env={**os.environ, "DATABASE_URL": "postgresql+asyncpg://u:p@localhost/t"},
    )
    assert done.returncode == 2 and "usage" in done.stderr


# ── The API: the server picks the profile; the body cannot ──────────────────────

class _Db:
    async def commit(self):
        return None


def _wire_submit(monkeypatch, *, verified=True, existing_scan=None):
    """Replace everything `_submit_scan` touches with fakes; return what it recorded."""
    rec = SimpleNamespace(request_scan_kwargs=None, enqueued=None, audited=None, scan=None)

    async def find_by_key(db, org_id, key):
        return existing_scan

    async def count_since(db, org_id, since):
        return 0

    async def request_scan(db, **kw):
        rec.request_scan_kwargs = kw
        profile = kw["profile"]
        if profile.requires_verified_domain and not verified:
            raise ScanAuthorizationError("Domain has not passed ownership verification.")
        rec.scan = SimpleNamespace(id=uuid.uuid4(), scan_options=None, idempotency_key=None,
                                   webhook_url=None, agent_callback_url=None,
                                   agent_callback_secret_ciphertext=None)
        return rec.scan

    async def prepare(url, secret):
        return None, None

    async def enqueue(db, **kw):
        rec.enqueued = kw

    async def audit(db, **kw):
        rec.audited = kw

    monkeypatch.setattr(route.api_service, "find_by_idempotency_key", find_by_key)
    monkeypatch.setattr(route.api_service, "count_scans_since", count_since)
    monkeypatch.setattr(route.scan_service, "request_scan", request_scan)
    monkeypatch.setattr(route.agent_webhook_service, "prepare_callback_override", prepare)
    monkeypatch.setattr(route.queue, "enqueue", enqueue)
    monkeypatch.setattr(route.audit_service, "record", audit)
    monkeypatch.setattr(route, "get_settings", lambda: _settings(
        consent_web_max_pages=5, consent_app_max_pages=0, consent_app_max_seconds=1800,
        consent_api_scans_per_hour=1000,
    ))
    return rec


def _body(max_pages=None):
    return CreateScanRequest(
        website_url="https://app.example.com", authorized=True,
        scan_options=ScanOptions(max_pages=max_pages),
    )


ADMIN = CurrentUser(user_id=str(USER), org_id=str(ORG), role="admin")
MEMBER = CurrentUser(user_id=str(USER), org_id=str(ORG), role="member")
LEGACY = CurrentUser(user_id=str(USER), org_id=str(ORG), role=None)
KEY = CurrentUser(user_id=str(USER), org_id=str(ORG), role=None, key_name="ci-key")


@pytest.mark.parametrize("requested", [None, 1, 5, 6, 25, 100])
async def test_the_web_endpoint_never_exceeds_five_pages_whatever_the_caller_asks(monkeypatch, requested):
    rec = _wire_submit(monkeypatch)

    out = await route.create_web_scan(
        body=_body(requested), response=MagicMock(), idempotency_key=None, user=MEMBER, db=_Db(),
    )

    expected = 5 if requested is None else min(requested, 5)
    assert out.profile == WEB and out.max_pages == expected <= 5
    assert rec.scan.scan_options["profile"] == WEB and rec.scan.scan_options["max_pages"] == expected
    assert rec.request_scan_kwargs["profile"].name == WEB
    assert rec.enqueued["payload"]["scan_profile"] == WEB
    assert rec.audited["after"]["profile"] == WEB


async def test_the_original_scans_endpoint_is_the_web_profile_and_cannot_start_an_application_scan(monkeypatch):
    rec = _wire_submit(monkeypatch)

    out = await route.create_scan(
        body=_body(100), response=MagicMock(), idempotency_key=None, user=ADMIN, db=_Db(),
    )

    assert out.profile == WEB and out.max_pages == 5
    assert rec.request_scan_kwargs["profile"].requires_verified_domain is False
    # An administrator calling the OLD endpoint still gets the capped profile.
    assert rec.scan.scan_options["profile"] == WEB


async def test_the_application_endpoint_has_no_fixed_page_cap_and_requires_a_verified_domain(monkeypatch):
    rec = _wire_submit(monkeypatch)

    out = await route.create_application_scan(
        body=_body(None), response=MagicMock(), idempotency_key=None, user=ADMIN, db=_Db(),
    )

    assert out.profile == APPLICATION and out.max_pages is None, "null == no fixed page-count cap"
    assert out.max_seconds == 1800, "...but it is bounded by a deadline"
    assert rec.request_scan_kwargs["profile"].requires_verified_domain is True
    assert rec.scan.scan_options == {
        "profile": APPLICATION, "max_pages": None, "max_seconds": 1800,
        "scan_consent_states": True, "scan_before_consent": True,
        "scan_after_accept": True, "scan_after_reject": True,
    }
    assert rec.enqueued["payload"]["scan_profile"] == APPLICATION


async def test_an_application_caller_may_still_ask_for_fewer_pages(monkeypatch):
    _wire_submit(monkeypatch)
    out = await route.create_application_scan(
        body=_body(30), response=MagicMock(), idempotency_key=None, user=ADMIN, db=_Db(),
    )
    assert out.max_pages == 30


async def test_an_unverified_domain_is_refused_for_an_application_scan(monkeypatch):
    rec = _wire_submit(monkeypatch, verified=False)

    with pytest.raises(ScanAuthorizationError, match="ownership verification"):
        await route.create_application_scan(
            body=_body(), response=MagicMock(), idempotency_key=None, user=ADMIN, db=_Db(),
        )

    assert rec.enqueued is None, "nothing may be queued for an unverified domain"


@pytest.mark.parametrize("user", [MEMBER, LEGACY], ids=["member", "token-with-no-role"])
def test_a_person_who_is_not_an_admin_cannot_use_the_application_profile(user):
    with pytest.raises(route.ApplicationScanNotPermittedError) as caught:
        route._require_application_permission(user)
    assert caught.value.status_code == 403
    assert "scans/web" in str(caught.value), "the refusal points at the endpoint they may use"


def test_an_admin_and_a_scoped_service_key_pass_the_application_check():
    route._require_application_permission(ADMIN)
    route._require_application_permission(KEY)     # scope already proven by the dependency


async def test_a_non_admin_is_stopped_before_anything_is_checked_or_queued(monkeypatch):
    rec = _wire_submit(monkeypatch)

    with pytest.raises(route.ApplicationScanNotPermittedError):
        await route.create_application_scan(
            body=_body(), response=MagicMock(), idempotency_key=None, user=MEMBER, db=_Db(),
        )

    assert rec.request_scan_kwargs is None and rec.enqueued is None


async def test_a_body_that_names_a_profile_or_a_huge_limit_changes_nothing(monkeypatch):
    """The model has no `profile` field and ignores unknown ones: the endpoint, not the payload,
    decides."""
    rec = _wire_submit(monkeypatch)
    body = CreateScanRequest.model_validate({
        "website_url": "https://example.com", "authorized": True, "profile": "application",
        "scan_options": {"max_pages": 100, "profile": "application", "unlimited": True},
    })

    out = await route.create_web_scan(
        body=body, response=MagicMock(), idempotency_key=None, user=MEMBER, db=_Db(),
    )

    assert out.profile == WEB and out.max_pages == 5
    assert rec.scan.scan_options["profile"] == WEB


async def test_an_idempotency_key_cannot_turn_a_web_scan_into_an_application_scan(monkeypatch):
    existing = SimpleNamespace(
        id=uuid.uuid4(), scan_options={"profile": WEB, "max_pages": 5, "max_seconds": 150},
    )
    _wire_submit(monkeypatch, existing_scan=existing)
    monkeypatch.setattr(route.api_service, "get_status", AsyncMock(return_value={"status": "queued"}))

    with pytest.raises(route.ScanConflictError, match="web scan"):
        await route.create_application_scan(
            body=_body(), response=MagicMock(), idempotency_key="k-1", user=ADMIN, db=_Db(),
        )


async def test_an_idempotent_replay_on_the_same_profile_returns_the_original(monkeypatch):
    existing = SimpleNamespace(
        id=uuid.uuid4(), scan_options={"profile": WEB, "max_pages": 5, "max_seconds": 150},
    )
    rec = _wire_submit(monkeypatch, existing_scan=existing)
    monkeypatch.setattr(route.api_service, "get_status", AsyncMock(return_value={"status": "queued"}))

    out = await route.create_web_scan(
        body=_body(), response=MagicMock(), idempotency_key="k-1", user=MEMBER, db=_Db(),
    )

    assert out.idempotent_replay and out.profile == WEB and out.max_pages == 5
    assert rec.request_scan_kwargs is None


# ── Authorization at the dependency: a key needs the application scope ───────────

def test_the_application_scope_exists_and_is_mintable():
    assert integration_auth.SCOPE_CONSENT_SCAN_APPLICATION == "consent:scan:application"
    assert "consent:scan:application" in integration_auth.KNOWN_SCOPES


def _route_dependencies(path: str, method: str = "POST") -> set:
    for r in route.router.routes:
        if r.path == path and method in r.methods:
            return {d.call for d in r.dependant.dependencies}
    raise AssertionError(f"no {method} {path}")


def test_each_endpoint_is_wired_to_the_dependency_that_matches_its_profile():
    base = "/api/v1/consent-agent/scans"
    assert integration_auth.get_application_scan_caller in _route_dependencies(f"{base}/application")
    assert integration_auth.get_caller not in _route_dependencies(f"{base}/application")
    for path in (base, f"{base}/web"):
        assert integration_auth.get_caller in _route_dependencies(path)
        assert integration_auth.get_application_scan_caller not in _route_dependencies(path)


async def _auth_with_key(monkeypatch, scopes):
    full_key, _prefix, _hash = integration_auth.generate_key()
    principal = integration_auth.IntegrationPrincipal(
        org_id=str(ORG), key_id=uuid.uuid4(), name="k", scopes=tuple(scopes),
    )
    monkeypatch.setattr(integration_auth, "resolve_integration_key", AsyncMock(return_value=principal))
    monkeypatch.setattr(integration_auth, "apply_org_scope", AsyncMock())
    db = MagicMock()
    db.execute = AsyncMock(return_value=MagicMock(
        scalar_one=MagicMock(return_value=SimpleNamespace(created_by_user_id=USER)),
    ))
    creds = SimpleNamespace(credentials=full_key)
    return await integration_auth.get_application_scan_caller(
        credentials=creds, db=db, settings=get_settings(),
    )


async def test_a_key_with_only_the_web_scope_is_refused_the_application_endpoint(monkeypatch):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as caught:
        await _auth_with_key(monkeypatch, ["consent:scan"])
    assert caught.value.status_code == 403 and "consent:scan:application" in caught.value.detail


async def test_a_key_with_the_application_scope_is_accepted_and_is_never_an_admin(monkeypatch):
    user = await _auth_with_key(monkeypatch, ["consent:scan:application"])
    assert user.key_name == "k" and user.role is None


async def test_no_credentials_means_401():
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as caught:
        await integration_auth.get_application_scan_caller(
            credentials=None, db=MagicMock(), settings=get_settings(),
        )
    assert caught.value.status_code == 401


def test_over_http_a_member_token_gets_403_with_a_stable_error_code_and_no_token_gets_401():
    from fastapi.testclient import TestClient

    from app.core.tokens import issue_access_token
    from app.main import app

    client = TestClient(app)
    payload = {"website_url": "https://app.example.com", "authorized": True}
    url = "/api/v1/consent-agent/scans/application"

    assert client.post(url, json=payload).status_code == 401

    token, _ = issue_access_token(
        user_id=str(USER), org_id=str(ORG), role="member", settings=get_settings(),
    )
    denied = client.post(url, json=payload, headers={"Authorization": f"Bearer {token}"})
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "APPLICATION_SCAN_NOT_PERMITTED"


# ── Service layer: the application profile always needs a verified domain ────────

def _wire_request_scan(monkeypatch, *, verified_at, require_flag=False):
    website = SimpleNamespace(id=uuid.uuid4(), verified_at=verified_at)
    monkeypatch.setattr(scan_service.scan_repository, "count_scans_since", AsyncMock(return_value=0))
    monkeypatch.setattr(scan_service.scan_repository, "get_or_create_website", AsyncMock(return_value=website))
    created = SimpleNamespace(id=uuid.uuid4())
    monkeypatch.setattr(scan_service.scan_repository, "create_scan", AsyncMock(return_value=created))
    monkeypatch.setattr(scan_service.audit_service, "record", AsyncMock())
    monkeypatch.setattr(scan_service, "assert_safe_url", AsyncMock())
    monkeypatch.setattr(scan_service, "get_settings", lambda: _settings(scanner_require_domain_verification=require_flag))

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def passthrough(scan_id, stage, **kw):
        yield {}
    monkeypatch.setattr(scan_service, "track_stage", passthrough)
    return created


async def test_application_scans_need_a_verified_domain_even_when_the_global_flag_is_off(monkeypatch):
    _wire_request_scan(monkeypatch, verified_at=None, require_flag=False)
    application = resolve_profile(APPLICATION)

    with pytest.raises(ScanAuthorizationError, match="ownership verification"):
        await scan_service.request_scan(
            AsyncMock(), org_id=ORG, user_id=USER, url="https://app.example.com",
            authorized=True, enqueue_job=False, profile=application,
        )


async def test_application_scans_proceed_for_a_verified_domain(monkeypatch):
    from datetime import UTC, datetime

    created = _wire_request_scan(monkeypatch, verified_at=datetime.now(UTC))

    scan = await scan_service.request_scan(
        AsyncMock(), org_id=ORG, user_id=USER, url="https://app.example.com",
        authorized=True, enqueue_job=False, profile=resolve_profile(APPLICATION),
    )

    assert scan is created


async def test_web_scans_keep_the_existing_deployment_wide_verification_policy(monkeypatch):
    created = _wire_request_scan(monkeypatch, verified_at=None, require_flag=False)

    scan = await scan_service.request_scan(
        AsyncMock(), org_id=ORG, user_id=USER, url="https://example.com",
        authorized=True, enqueue_job=False, profile=resolve_profile(WEB),
    )
    assert scan is created, "an unverified domain is still scannable on the web profile by default"

    _wire_request_scan(monkeypatch, verified_at=None, require_flag=True)
    with pytest.raises(ScanAuthorizationError):
        await scan_service.request_scan(
            AsyncMock(), org_id=ORG, user_id=USER, url="https://example.com",
            authorized=True, enqueue_job=False, profile=resolve_profile(WEB),
        )


async def test_ssrf_protection_still_refuses_private_targets_for_the_application_profile(monkeypatch):
    """"Internal application" means the org's own app on a routable address: the SSRF guard is
    not relaxed for it."""
    from datetime import UTC, datetime

    _wire_request_scan(monkeypatch, verified_at=datetime.now(UTC))
    monkeypatch.setattr(scan_service, "assert_safe_url", AsyncMock(
        side_effect=ScanAuthorizationError("resolves to a disallowed address (10.0.0.5)")))
    failed = AsyncMock()
    monkeypatch.setattr(scan_service.scan_repository, "mark_scan_failed", failed)

    class _CM:
        async def __aenter__(self):
            return MagicMock(commit=AsyncMock())

        async def __aexit__(self, *a):
            return False
    monkeypatch.setattr(scan_service, "async_session_factory", lambda: _CM())

    with pytest.raises(ScanAuthorizationError, match="disallowed address"):
        await scan_service.request_scan(
            AsyncMock(), org_id=ORG, user_id=USER, url="http://10.0.0.5/app",
            authorized=True, enqueue_job=False, profile=resolve_profile(APPLICATION),
        )
    failed.assert_awaited_once()


# ── The worker: the stored profile is what runs, and a cancel keeps its evidence ─

class _Row:
    def __init__(self, options, status="queued"):
        self.scan_options = options
        self.status = status


def _wire_worker(monkeypatch, row, *, scan_result=None, run=None):
    from contextlib import asynccontextmanager

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, model, ident):
            return row

        async def commit(self):
            return None

    @asynccontextmanager
    async def passthrough(scan_id, stage, **kw):
        yield {}

    monkeypatch.setattr(scan_service, "async_session_factory", lambda: _Session())
    monkeypatch.setattr(scan_service, "track_stage", passthrough)
    monkeypatch.setattr(scan_service.scan_repository, "mark_scan_running", AsyncMock())
    monkeypatch.setattr(scan_service.lookup_repository, "load_all", AsyncMock(return_value=[]))
    monkeypatch.setattr(scan_service, "classify_scan", MagicMock(return_value=SimpleNamespace(
        cookies=[], trackers=[], third_party_services=[])))
    monkeypatch.setattr(scan_service.scan_repository, "save_scan_result", AsyncMock(return_value={}))
    runner = run or AsyncMock(return_value=scan_result or _fake_scan_result())
    monkeypatch.setattr(scan_service, "run_scan_isolated", runner)
    return runner


async def test_the_worker_runs_the_profile_stored_on_the_scan_row(monkeypatch):
    runner = _wire_worker(monkeypatch, _Row({"profile": APPLICATION, "max_pages": None}))

    await scan_service.execute_scan_and_persist(uuid.uuid4(), "https://app.example.com")

    kwargs = runner.await_args.kwargs
    assert kwargs["profile"].name == APPLICATION and kwargs["profile"].max_pages is None
    assert callable(kwargs["should_cancel"]), "cancellation is wired in"


@pytest.mark.parametrize("options", [None, {}, {"max_pages": 25}, {"profile": "garbage"}])
async def test_a_scan_with_no_valid_profile_runs_as_a_web_scan(monkeypatch, options):
    """Console scans, scheduled monitoring scans and every scan from before profiles existed."""
    runner = _wire_worker(monkeypatch, _Row(options))

    await scan_service.execute_scan_and_persist(uuid.uuid4(), "https://example.com")

    assert runner.await_args.kwargs["profile"].name == WEB
    assert runner.await_args.kwargs["profile"].max_pages == get_settings().consent_web_max_pages


async def test_a_scan_cancelled_before_it_starts_is_not_crawled_or_revived(monkeypatch):
    runner = _wire_worker(monkeypatch, _Row({"profile": APPLICATION}, status="cancelled"))
    running = scan_service.scan_repository.mark_scan_running

    await scan_service.execute_scan_and_persist(uuid.uuid4(), "https://app.example.com")

    runner.assert_not_awaited()
    running.assert_not_awaited()


async def test_a_scan_cancelled_mid_crawl_keeps_its_evidence_and_stays_cancelled(monkeypatch):
    result = _fake_scan_result()
    result.scan_diagnostics = {"stop_reason": "cancelled", "pages_attempted": 12}
    row = _Row({"profile": APPLICATION}, status="running")
    _wire_worker(monkeypatch, row, scan_result=result)
    saved = scan_service.scan_repository.save_scan_result

    await scan_service.execute_scan_and_persist(uuid.uuid4(), "https://app.example.com")

    saved.assert_awaited_once()                  # the evidence gathered before the cancel is kept
    assert row.status == "cancelled", "persisting must not quietly undo the cancel"


# ── The reaper: a long application scan is not reclaimed (and crawled twice) ─────

def _job(profile=None, age_minutes=0, job_type="scan"):
    from datetime import UTC, datetime, timedelta

    payload = {"scan_id": "x"}
    if profile:
        payload["scan_profile"] = profile
    return SimpleNamespace(
        payload=payload, locked_at=datetime.now(UTC) - timedelta(minutes=age_minutes),
        status="running", attempts=1, locked_by="w", error=None, run_after=None, job_type=job_type,
    )


def test_the_reaper_gives_application_scans_a_lease_longer_than_their_own_timeout():
    from datetime import timedelta

    web, app = queue.stale_threshold_for(_job()), queue.stale_threshold_for(_job(APPLICATION))

    assert queue.stale_threshold_for(_job(job_type="analyze")) == queue._stale_threshold(), "other jobs use the derived base"
    web_profile = resolve_profile(WEB)
    assert web > timedelta(seconds=web_profile.hard_timeout_seconds), (
        "a web scan must be allowed to run to its own ceiling before its job can be called lost, or the "
        "reaper would start a second crawl of the same site alongside it"
    )
    profile = resolve_profile(APPLICATION)
    assert app > timedelta(seconds=profile.hard_timeout_seconds), (
        "the job must be allowed to run to its own timeout before it can be called lost"
    )


async def test_a_healthy_long_application_scan_is_not_reaped_but_a_web_job_is(monkeypatch):
    healthy_app = _job(APPLICATION, age_minutes=45)       # past 10 min, well inside its lease
    slow_web = _job(None, age_minutes=20)                 # past the old 10 min, inside the web ceiling
    stuck_web = _job(None, age_minutes=60)                # past the 30 min ceiling plus its margin
    stale_analysis = _job(job_type="analyze", age_minutes=20)   # not a scan: still 10 minutes
    dead_app = _job(APPLICATION, age_minutes=60 * 26)     # past even the 24 hour ceiling plus its margin

    class _Result:
        def scalars(self):
            return SimpleNamespace(all=lambda: [healthy_app, slow_web, stuck_web, stale_analysis, dead_app])

    db = SimpleNamespace(execute=AsyncMock(return_value=_Result()), flush=AsyncMock())

    reaped = await queue.reap_stale_jobs(db)

    assert reaped == 3
    assert healthy_app.status == "running", "an in-budget application scan keeps its lease"
    assert slow_web.status == "running", "a slow web scan is not reclaimed and crawled a second time"
    assert stuck_web.status == "queued" and dead_app.status == "queued" and stale_analysis.status == "queued"


# ── A retried scan is running again: the previous attempt's failure must not still be on the row ──

async def test_a_scan_that_starts_again_after_a_failed_attempt_no_longer_shows_that_failure():
    from datetime import UTC, datetime

    from app.db.repositories import scan_repository

    scan = SimpleNamespace(
        status="failed", error="Scan of https://x/ exceeded its 330s budget and was aborted",
        completed_at=datetime.now(UTC), started_at=None,
    )
    db = SimpleNamespace(get=AsyncMock(return_value=scan), flush=AsyncMock())

    await scan_repository.mark_scan_running(db, uuid.uuid4())

    assert scan.status == "running"
    assert scan.error is None and scan.completed_at is None
    assert scan.started_at is not None
