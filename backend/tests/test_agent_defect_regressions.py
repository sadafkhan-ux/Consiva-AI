"""Regressions for defects found in a line-by-line review of the Consent Agent.

Each test here failed against the code as it stood, and names the specific thing that
was wrong rather than restating what the module is supposed to do -- so a future edit
that reintroduces the defect fails on the defect, not on a paraphrase of the design.
"""

from __future__ import annotations

# ── Consent agent: the graph is built once per process ───────────────────────


async def test_concurrent_callers_share_one_compiled_graph(monkeypatch):
    """The worker runs `worker_concurrency` jobs concurrently and never builds the
    graph at startup, so two analyze jobs starting together both walked through the
    `is not None` check and each built their own checkpointer pool. The second
    overwrote `_exit_stack`, leaking the first for the life of the process."""
    import asyncio

    from app.agents.consent_agent import graph as graph_module

    builds = 0
    closed: list[object] = []

    class _FakeCheckpointer:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            closed.append(self)
            return False

    def _fake_from_conn_string(_dsn):
        return _FakeCheckpointer()

    async def _fake_ensure(_checkpointer):
        # The real one awaits DDL; the yield here is what makes the race reachable.
        await asyncio.sleep(0)

    def _fake_build_graph():
        nonlocal builds
        builds += 1

        class _Compilable:
            def compile(self, *, checkpointer):
                return object()

        return _Compilable()

    monkeypatch.setattr(
        graph_module.AsyncPostgresSaver, "from_conn_string", staticmethod(_fake_from_conn_string)
    )
    monkeypatch.setattr(graph_module, "_ensure_checkpoint_tables", _fake_ensure)
    monkeypatch.setattr(graph_module, "build_graph", _fake_build_graph)
    monkeypatch.setattr(graph_module, "_compiled_graph", None)
    monkeypatch.setattr(graph_module, "_exit_stack", None)

    try:
        graphs = await asyncio.gather(*(graph_module.get_compiled_graph() for _ in range(4)))

        assert builds == 1, f"{builds} graphs were built for one process"
        assert len(set(map(id, graphs))) == 1, "concurrent callers got different graphs"
    finally:
        await graph_module.close_graph_resources()

    assert len(closed) == 1, (
        f"{len(closed)} checkpointer(s) closed -- a pool was built and then leaked"
    )
