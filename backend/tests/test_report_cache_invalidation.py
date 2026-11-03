"""A stored report must never outlive the web app that drew it: a new build changes the bundle names in its index page, which changes the signature the stored copy is checked against."""

import asyncio

import httpx
import pytest

import app.services.report_cache_service as rc
from app.config import get_settings


REAL_CLIENT = httpx.AsyncClient       # the genuine class, taken once, so a second patch does not wrap the first one


def _serve(monkeypatch, body: bytes | None):
    real = REAL_CLIENT

    def handler(req):
        if body is None:
            raise httpx.ConnectError("down")
        return httpx.Response(200, content=body)

    monkeypatch.setattr(rc.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(rc, "_frontend_signature", None)


def test_a_new_build_of_the_web_app_gives_a_different_signature(monkeypatch):
    settings = get_settings()
    _serve(monkeypatch, b'<script src="/assets/index-AAAA1111.js"></script>')
    old = asyncio.run(rc.frontend_signature(settings))
    _serve(monkeypatch, b'<script src="/assets/index-BBBB2222.js"></script>')
    new = asyncio.run(rc.frontend_signature(settings))
    assert old != new and "unknown" not in (old, new)


def test_the_same_build_gives_the_same_signature(monkeypatch):
    settings = get_settings()
    _serve(monkeypatch, b'<script src="/assets/index-AAAA1111.js"></script>')
    a = asyncio.run(rc.frontend_signature(settings))
    monkeypatch.setattr(rc, "_frontend_signature", None)
    b = asyncio.run(rc.frontend_signature(settings))
    assert a == b


def test_an_unreachable_web_app_is_unknown_never_a_value_that_could_match_a_stored_copy(monkeypatch):
    settings = get_settings()
    _serve(monkeypatch, b'<script src="/assets/index-AAAA1111.js"></script>')
    good = asyncio.run(rc.frontend_signature(settings))
    _serve(monkeypatch, None)
    assert asyncio.run(rc.frontend_signature(settings)) == "unknown" != good


def test_every_input_that_changes_a_report_is_in_the_fingerprint():
    """The fingerprint is a digest of the render version, the scan and its status and completion time, every analysis run, every finding and its status, every stage, the screenshot,
    the day in India, the scoring methodology and weights, and the web app's signature. A change to any of them gives a different digest."""
    base = ["1", "scan", "completed", "t0", [("run", "done", "t1")], [("f", "approved")], [("policy_analysis", "completed", "t2", "run")], None, "2026-11-03", "1.6.1", {}, "sigA"]
    seen = {rc._digest(base)}
    for i in range(len(base)):
        changed = list(base)
        changed[i] = [("changed",)] if isinstance(base[i], (list, dict, type(None))) else str(base[i]) + "x"
        d = rc._digest(changed)
        assert d not in seen, f"input {i} does not change the digest"
        seen.add(d)
