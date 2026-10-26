"""The speed-ups that cost no accuracy and no tokens: the policy reading runs beside the graph (not before it), a finished policy reading is reused when the model
would be asked exactly the same question, and a run is never marked completed before its policy reading has finished."""

import asyncio
import time
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

from app.policy import analysis
from app.services import analysis_service, policy_background


class TestPolicyBackground:
    async def test_wait_returns_at_once_when_there_is_no_policy_reading(self):
        t = time.monotonic()
        await policy_background.wait(uuid.uuid4())
        assert time.monotonic() - t < 0.05

    async def test_wait_returns_only_when_the_reading_has_finished(self):
        run = uuid.uuid4()
        done = []

        async def work():
            await asyncio.sleep(0.15)
            done.append(1)

        policy_background.start(run, work)
        await policy_background.wait(run)
        assert done == [1]
        policy_background.forget(run)

    async def test_a_failing_reading_is_swallowed_and_never_fails_the_wait(self):
        run = uuid.uuid4()

        async def work():
            raise RuntimeError("model down")

        policy_background.start(run, work)
        await policy_background.wait(run)       # does not raise
        policy_background.forget(run)

    async def test_the_audit_node_waits_for_it_before_marking_the_run_completed(self, monkeypatch):
        from app.agents.consent_agent.nodes import audit

        order: list[str] = []
        run = uuid.uuid4()

        async def work():
            await asyncio.sleep(0.1)
            order.append("policy done")

        policy_background.start(run, work)

        @asynccontextmanager
        async def stage(*a, **k):
            order.append("audit stage opened")
            yield {}

        monkeypatch.setattr(audit, "track_stage", stage)

        class Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, *a):
                return None            # no agent run row: write_audit_log returns right after its stage opens

        monkeypatch.setattr(audit, "async_session_factory", lambda: Session())
        await audit.write_audit_log(SimpleNamespace(scan_id=str(uuid.uuid4()), org_id=str(uuid.uuid4()), agent_run_id=str(run), error=None, created_finding_ids=[]))
        assert order == ["policy done", "audit stage opened"]
        policy_background.forget(run)


class TestRunAnalysisOverlaps:
    async def test_the_policy_reading_and_the_graph_run_together(self, monkeypatch):
        """Each takes 0.4 s: one after the other would be 0.8 s, together about 0.4 s."""
        ids = [uuid.uuid4() for _ in range(3)]

        class Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, model, key):
                return SimpleNamespace(status="pending", started_at=None, llm_provider=None, llm_model=None, langgraph_thread_id=None, error=None, completed_at=None)

            async def commit(self):
                return None

        @asynccontextmanager
        async def stage(*a, **k):
            yield {}

        async def policy_run(scan_id):
            await asyncio.sleep(0.4)
            return {"status": "analysed"}

        class Graph:
            async def ainvoke(self, state, config=None):
                await asyncio.sleep(0.4)
                await policy_background.wait(uuid.UUID(state["agent_run_id"]))     # what the audit node does
                return {}

        async def compiled():
            return Graph()

        async def no_prewarm(*a, **k):
            return None

        monkeypatch.setattr(analysis_service.report_cache_service, "queue_prewarm", no_prewarm)
        monkeypatch.setattr(analysis_service, "async_session_factory", lambda: Session())
        monkeypatch.setattr(analysis_service, "get_compiled_graph", compiled)
        monkeypatch.setattr("app.observability.stage_tracker.track_stage", stage)
        monkeypatch.setattr("app.policy.analysis.run", policy_run)
        t = time.monotonic()
        await analysis_service.run_analysis(ids[0], ids[1], ids[2])
        elapsed = time.monotonic() - t
        assert elapsed < 0.75, elapsed        # one after the other would take 0.8 s

    async def test_a_crashing_graph_still_lets_the_policy_reading_finish(self, monkeypatch):
        ids = [uuid.uuid4() for _ in range(3)]
        finished = []

        class Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, model, key):
                return SimpleNamespace(status="pending", started_at=None, llm_provider=None, llm_model=None, langgraph_thread_id=None, error=None, completed_at=None)

            async def commit(self):
                return None

        @asynccontextmanager
        async def stage(*a, **k):
            yield {}

        async def policy_run(scan_id):
            await asyncio.sleep(0.1)
            finished.append(1)
            return {"status": "analysed"}

        class Graph:
            async def ainvoke(self, state, config=None):
                raise RuntimeError("graph crashed")

        async def compiled():
            return Graph()

        async def no_prewarm(*a, **k):
            return None

        monkeypatch.setattr(analysis_service.report_cache_service, "queue_prewarm", no_prewarm)
        monkeypatch.setattr(analysis_service, "async_session_factory", lambda: Session())
        monkeypatch.setattr(analysis_service, "get_compiled_graph", compiled)
        monkeypatch.setattr("app.observability.stage_tracker.track_stage", stage)
        monkeypatch.setattr("app.policy.analysis.run", policy_run)
        try:
            await analysis_service.run_analysis(ids[0], ids[1], ids[2])
        except RuntimeError:
            pass
        assert finished == [1]


class TestReuse:
    def test_the_hash_changes_with_anything_the_answer_depends_on(self):
        base = ("system", "policy text", "retail", False, "claude/m1")
        h = analysis.input_hash(*base)
        assert h == analysis.input_hash(*base)
        assert h != analysis.input_hash("system2", *base[1:])
        assert h != analysis.input_hash(base[0], "policy text changed", *base[2:])
        assert h != analysis.input_hash(*base[:2], "health", *base[3:])
        assert h != analysis.input_hash(*base[:3], True, base[4])
        assert h != analysis.input_hash(*base[:4], "claude/m2")

    async def test_no_website_or_org_means_nothing_is_reused(self):
        assert await analysis.find_reusable(uuid.uuid4(), None, uuid.uuid4(), "x") is None
        assert await analysis.find_reusable(uuid.uuid4(), uuid.uuid4(), None, "x") is None
