"""Release approval of a report (services/report_approval_service.py): who may approve, and who may download a report that has not been approved."""
from __future__ import annotations

import uuid

import pytest

from app.config import Settings
from app.core.exceptions import ReportNotApprovedError
from app.services import report_approval_service as ra


def settings(required: bool) -> Settings:
    return Settings(report_require_approval=required)


def state(name: str) -> ra.ApprovalState:
    return ra.ApprovalState(name, current_version="scorecard-3.3.0")


def use(monkeypatch, name: str) -> None:
    async def get_state(db, scan_id):
        return state(name)

    monkeypatch.setattr(ra, "get_state", get_state)


async def test_with_the_requirement_off_every_report_is_released_as_before(monkeypatch):
    use(monkeypatch, "not_approved")
    assert await ra.require_release(None, uuid.uuid4(), "member", settings(False)) == "released"


async def test_an_approved_report_is_released_to_a_customer(monkeypatch):
    use(monkeypatch, "approved")
    assert await ra.require_release(None, uuid.uuid4(), "member", settings(True)) == "released"


@pytest.mark.parametrize("name,words", [("not_approved", "not been approved"), ("stale", "earlier version"), ("revoked", "withdrawn")])
async def test_an_unapproved_report_is_refused_to_a_customer_and_says_why(monkeypatch, name, words):
    use(monkeypatch, name)
    with pytest.raises(ReportNotApprovedError, match=words):
        await ra.require_release(None, uuid.uuid4(), "member", settings(True))
    with pytest.raises(ReportNotApprovedError):
        await ra.require_release(None, uuid.uuid4(), None, settings(True))


async def test_an_administrator_can_always_download_a_draft_to_review_it(monkeypatch):
    use(monkeypatch, "not_approved")
    assert await ra.require_release(None, uuid.uuid4(), "admin", settings(True)) == "draft"


def test_only_an_administrator_is_a_reviewer():
    assert ra.is_reviewer("admin") and not ra.is_reviewer("member") and not ra.is_reviewer(None)


async def test_a_report_printed_while_the_web_app_was_unreachable_is_served_once_but_not_stored(monkeypatch):
    from types import SimpleNamespace

    from app.services import report_cache_service as rc
    from app.services.report_pdf_service import RenderedReport

    kept: list = []

    async def fp(db, sc, settings=None):
        return "A"

    async def stored(db, sid, variant, current):
        return None

    async def render(*a, **k):
        return RenderedReport(pdf=b"%PDF-new", filename="r.pdf")

    async def store(*a, **k):
        kept.append(1)

    async def unknown(settings=None):
        return "unknown"

    class NoDb:
        async def rollback(self):
            return None

    rc._inflight.clear()
    for name, fn in (("fingerprint", fp), ("get_stored", stored), ("_store", store), ("frontend_signature", unknown)):
        monkeypatch.setattr(rc, name, fn)
    monkeypatch.setattr(rc.report_pdf_service, "render_report_pdf", render)
    scan = SimpleNamespace(id=uuid.uuid4(), org_id=uuid.uuid4(), status="completed", completed_at=None, authorized_by_user_id=None)
    out, how = await rc.get_or_render(NoDb(), scan, "full", token="t", user_id="u", org_id="o", role="member")
    assert (out.pdf, how, kept) == (b"%PDF-new", "printed", [])
    rc._inflight.clear()


def test_the_version_snapshot_names_what_this_process_runs():
    from app.core import versions

    s = versions.snapshot()
    assert s["scorecard"].startswith("3.") and s["scoring_engine"] == "scorecard" and s["report_require_approval"] is False
    assert "scorecard 3." in versions.describe()
