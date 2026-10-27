"""Pre-generated reports: served only while fresh, printed once however many ask together, and never an out-of-date report."""

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from app.services import report_cache_service as rc
from app.services.report_pdf_service import RenderedReport


def scan():
    return SimpleNamespace(id=uuid.uuid4(), org_id=uuid.uuid4(), status="completed", completed_at=None, authorized_by_user_id=None)


class NoDb:
    async def rollback(self):
        return None


class ExpiringScan:
    """Behaves like an ORM row: once the session rolls back, reading any attribute needs the database again (SQLAlchemy's MissingGreenlet in a real request)."""
    def __init__(self):
        self._id = uuid.uuid4()
        self.expired = False

    @property
    def id(self):
        if self.expired:
            raise RuntimeError("MissingGreenlet: the row was expired by the rollback and was read again")
        return self._id


class ExpiringDb:
    def __init__(self, scan):
        self.scan = scan

    async def rollback(self):
        self.scan.expired = True


@pytest.fixture(autouse=True)
def clean():
    rc._inflight.clear()
    yield
    rc._inflight.clear()


class TestServedOnlyWhileFresh:
    async def test_a_stored_copy_is_used_when_its_fingerprint_matches(self, monkeypatch):
        s = scan()
        calls = []

        async def fp(db, sc, settings=None):
            return "A"

        async def stored(db, sid, variant, current):
            return RenderedReport(pdf=b"%PDF-stored", filename="r.pdf") if current == "A" else None

        async def render(*a, **k):
            calls.append(1)
            return RenderedReport(pdf=b"%PDF-new", filename="r.pdf")

        monkeypatch.setattr(rc, "fingerprint", fp)
        monkeypatch.setattr(rc, "get_stored", stored)
        monkeypatch.setattr(rc.report_pdf_service, "render_report_pdf", render)
        out, how = await rc.get_or_render(NoDb(), s, "long", token="t", user_id="u", org_id="o", role="member")
        assert (out.pdf, how, calls) == (b"%PDF-stored", "stored", [])

    async def test_a_changed_fingerprint_prints_again_and_stores_the_new_copy(self, monkeypatch):
        s = scan()
        kept = {}

        async def fp(db, sc, settings=None):
            return "B"                                   # the scan changed since the stored copy (fingerprint "A") was printed

        async def stored(db, sid, variant, current):
            return None if current == "B" else RenderedReport(pdf=b"%PDF-old", filename="r.pdf")

        async def render(*a, **k):
            return RenderedReport(pdf=b"%PDF-new", filename="r.pdf")

        async def store(sid, variant, current, rendered, settings):
            kept[(variant, current)] = rendered.pdf

        monkeypatch.setattr(rc, "fingerprint", fp)
        monkeypatch.setattr(rc, "get_stored", stored)
        monkeypatch.setattr(rc, "_store", store)
        monkeypatch.setattr(rc.report_pdf_service, "render_report_pdf", render)
        out, how = await rc.get_or_render(NoDb(), s, "full", token="t", user_id="u", org_id="o", role="member")
        assert (out.pdf, how, kept) == (b"%PDF-new", "printed", {("full", "B"): b"%PDF-new"})


class TestRowIsNotReadAfterTheRollback:
    async def test_printing_and_storing_never_touch_the_expired_row(self, monkeypatch):
        sc = ExpiringScan()
        stored_for = []

        async def fp(db, s, settings=None):
            return "A"

        async def stored(db, sid, variant, current):
            return None

        async def render(scan_id, **k):
            return RenderedReport(pdf=b"%PDF-x", filename="r.pdf")

        async def store(scan_id, variant, current, rendered, settings):
            stored_for.append(scan_id)

        monkeypatch.setattr(rc, "fingerprint", fp)
        monkeypatch.setattr(rc, "get_stored", stored)
        monkeypatch.setattr(rc, "_store", store)
        monkeypatch.setattr(rc.report_pdf_service, "render_report_pdf", render)
        _out, how = await rc.get_or_render(ExpiringDb(sc), sc, "long", token="t", user_id="u", org_id="o", role="member")
        assert how == "printed" and stored_for == [sc._id]


class TestConnectionIsReleasedBeforePrinting:
    async def test_the_request_gives_its_database_connection_back_before_the_slow_print(self, monkeypatch):
        s = scan()
        order: list[str] = []

        class Db:
            async def rollback(self):
                order.append("connection released")

        async def fp(db, sc, settings=None):
            return "A"

        async def stored(*x):
            return None

        async def render(*a, **k):
            order.append("printing")
            return RenderedReport(pdf=b"%PDF-x", filename="r.pdf")

        async def store(*x, **k):
            return None

        monkeypatch.setattr(rc, "fingerprint", fp)
        monkeypatch.setattr(rc, "get_stored", stored)
        monkeypatch.setattr(rc, "_store", store)
        monkeypatch.setattr(rc.report_pdf_service, "render_report_pdf", render)
        await rc.get_or_render(Db(), s, "long", token="t", user_id="u", org_id="o", role="member")
        assert order == ["connection released", "printing"]


class TestOnePrintForSimultaneousRequests:
    async def test_ten_requests_at_once_share_a_single_print(self, monkeypatch):
        s = scan()
        prints = []

        async def fp(db, sc, settings=None):
            return "A"

        async def stored(db, sid, variant, current):
            return None

        async def render(*a, **k):
            prints.append(1)
            await asyncio.sleep(0.2)
            return RenderedReport(pdf=b"%PDF-x", filename="r.pdf")

        async def store(*a, **k):
            return None

        monkeypatch.setattr(rc, "fingerprint", fp)
        monkeypatch.setattr(rc, "get_stored", stored)
        monkeypatch.setattr(rc, "_store", store)
        monkeypatch.setattr(rc.report_pdf_service, "render_report_pdf", render)
        results = await asyncio.gather(*[rc.get_or_render(NoDb(), s, "long", token="t", user_id="u", org_id="o", role="member") for _ in range(10)])
        assert len(prints) == 1
        assert all(r[0].pdf == b"%PDF-x" for r in results)

    async def test_different_scans_and_report_types_are_printed_separately(self, monkeypatch):
        a, b = scan(), scan()
        prints = []

        async def fp(db, sc, settings=None):
            return "A"

        async def stored(*x):
            return None

        async def render(scan_id, **k):
            prints.append((scan_id, k["variant"]))
            await asyncio.sleep(0.05)
            return RenderedReport(pdf=b"%PDF-x", filename="r.pdf")

        async def store(*x, **k):
            return None

        monkeypatch.setattr(rc, "fingerprint", fp)
        monkeypatch.setattr(rc, "get_stored", stored)
        monkeypatch.setattr(rc, "_store", store)
        monkeypatch.setattr(rc.report_pdf_service, "render_report_pdf", render)
        await asyncio.gather(*[rc.get_or_render(NoDb(), sc, v, token="t", user_id="u", org_id="o", role="member") for sc in (a, b) for v in ("long", "full")])
        assert len(set(prints)) == 4

    async def test_one_waiter_leaving_does_not_cancel_the_print_the_others_wait_for(self, monkeypatch):
        s = scan()

        async def fp(db, sc, settings=None):
            return "A"

        async def stored(*x):
            return None

        async def render(*a, **k):
            await asyncio.sleep(0.2)
            return RenderedReport(pdf=b"%PDF-x", filename="r.pdf")

        async def store(*x, **k):
            return None

        monkeypatch.setattr(rc, "fingerprint", fp)
        monkeypatch.setattr(rc, "get_stored", stored)
        monkeypatch.setattr(rc, "_store", store)
        monkeypatch.setattr(rc.report_pdf_service, "render_report_pdf", render)
        first = asyncio.ensure_future(rc.get_or_render(NoDb(), s, "long", token="t", user_id="u", org_id="o", role="member"))
        second = asyncio.ensure_future(rc.get_or_render(NoDb(), s, "long", token="t", user_id="u", org_id="o", role="member"))
        await asyncio.sleep(0.05)
        first.cancel()                                      # the first caller disconnects
        out, how = await second
        assert out.pdf == b"%PDF-x" and how == "printed"


class TestFingerprintInputs:
    def test_the_digest_changes_with_any_input_and_not_with_order_of_keys(self):
        base = ["1", "s", "completed", None, [("r", "completed", None)], [("f", "pending")], [], None, "2026-10-26", "1.5.0", {}, "sig"]
        h = rc._digest(base)
        assert h == rc._digest(list(base))
        for i, change in [(9, "1.6.1"), (8, "2026-10-27"), (11, "sig2"), (5, [("f", "approved")]), (4, [("r", "failed", None)])]:
            other = list(base)
            other[i] = change
            assert rc._digest(other) != h

    async def test_prewarm_does_nothing_for_a_scan_that_is_not_completed(self, monkeypatch):
        class Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, model, key):
                return SimpleNamespace(status="running")

            async def execute(self, *a, **k):
                return SimpleNamespace(scalar_one=lambda: 0)

        monkeypatch.setattr(rc, "async_session_factory", lambda: Session())
        assert await rc.prewarm(uuid.uuid4()) == {}


class TestWiring:
    def test_the_worker_knows_the_job_and_the_audit_node_queues_it(self):
        import inspect

        from app.agents.consent_agent.nodes import audit
        from app.jobs import worker
        from app.services import analysis_service
        assert 'job_type == "render_reports"' in inspect.getsource(worker._process_one)
        assert "queue_prewarm" in inspect.getsource(audit.write_audit_log)          # after a person's review
        assert "queue_prewarm" in inspect.getsource(analysis_service.run_analysis)  # when the analysis ends (or pauses for review)

    def test_the_download_endpoint_goes_through_the_cache(self):
        import inspect

        from app.api.v1.routes import consent_scans
        src = inspect.getsource(consent_scans.get_scan_report_pdf)
        assert "report_cache_service.get_or_render" in src and "X-Report-Source" in src


class TestQueueAcceptsTheJob:
    def test_the_latest_job_type_check_lists_every_job_type_the_worker_handles(self):
        """The queue refuses a job type its CHECK constraint does not list (it silently refused render_reports once): every type the worker handles must be in the newest migration that sets the constraint."""
        import inspect
        import re
        from pathlib import Path

        from app.jobs import worker
        handled = set(re.findall(r'job_type == "([a-z_]+)"', inspect.getsource(worker._process_one)))
        migrations = sorted(Path(__file__).resolve().parents[1].glob("migrations/*.sql"))
        newest = [m for m in migrations if "agent_jobs_job_type_check" in m.read_text(encoding="utf-8")][-1]
        allowed = set(re.findall(r"'([a-z_]+)'", newest.read_text(encoding="utf-8").split("check (job_type in (")[1].split("));")[0]))
        assert handled <= allowed, sorted(handled - allowed)


class TestPrewarmPrintsEveryVariantAfterTheConnectionIsReleased:
    async def test_all_three_reports_are_printed_in_one_session(self, monkeypatch):
        sid = uuid.uuid4()
        printed = []

        class Row:
            def __init__(self):
                self.expired = False
                self.status = "completed"
                self.org_id = uuid.uuid4()
                self.authorized_by_user_id = None

            @property
            def id(self):
                if self.expired:
                    raise RuntimeError("expired row read")
                return sid

        class Session:
            row = None

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, model, key):
                Session.row = Row()          # a fresh row, as a real session loads after a rollback
                return Session.row

            async def rollback(self):
                Session.row.expired = True

            async def execute(self, *a, **k):
                return SimpleNamespace(scalar_one=lambda: 0)

        async def fp(db, s, settings=None):
            return "A"

        async def stored(*x):
            return None

        async def render(scan_id, **k):
            printed.append(k["variant"])
            return RenderedReport(pdf=b"%PDF-x", filename="r.pdf")

        async def store(*x, **k):
            return None

        monkeypatch.setattr(rc, "async_session_factory", lambda: Session())
        monkeypatch.setattr(rc, "fingerprint", fp)
        monkeypatch.setattr(rc, "get_stored", stored)
        monkeypatch.setattr(rc, "_store", store)
        monkeypatch.setattr(rc.report_pdf_service, "render_report_pdf", render)
        out = await rc.prewarm(sid)
        assert printed == ["summary", "long", "full", "free"] and set(out.values()) == {"printed"}
