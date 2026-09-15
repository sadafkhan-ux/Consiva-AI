"""Builds and compiles the consent agent's LangGraph StateGraph (docs/architecture §E).

Uses a Postgres-backed checkpointer so a human-review pause survives a process
restart — the whole point of using LangGraph here instead of a hand-rolled state
machine. `langgraph-checkpoint-postgres`'s exact setup call has moved between minor
versions; confirm `AsyncPostgresSaver.from_conn_string()` + `.setup()` against the
version pinned in pyproject.toml on first run (see docs/architecture §P).
"""

import logging
from contextlib import AsyncExitStack

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, StateGraph

from app.agents.consent_agent.nodes.audit import write_audit_log
from app.agents.consent_agent.nodes.classify_rules import classify_rules
from app.agents.consent_agent.nodes.create_findings import create_findings
from app.agents.consent_agent.nodes.human_review_gate import human_review_gate
from app.agents.consent_agent.nodes.llm_reasoning import llm_reasoning
from app.agents.consent_agent.nodes.normalize import normalize
from app.agents.consent_agent.nodes.retrieve_rag import retrieve_rag
from app.agents.consent_agent.nodes.validate_output import validate_output
from app.agents.consent_agent.state import AgentState
from app.config import get_settings

_VALIDATION_ROUTES = {"valid": "create_findings", "retry": "llm_reasoning", "failed": "write_audit_log"}


def _route_after_validation(state: AgentState) -> str:
    return _VALIDATION_ROUTES[state.validation_status]


def build_graph() -> StateGraph:
    graph = StateGraph(AgentState)

    graph.add_node("normalize", normalize)
    graph.add_node("classify_rules", classify_rules)
    graph.add_node("retrieve_rag", retrieve_rag)
    graph.add_node("llm_reasoning", llm_reasoning)
    graph.add_node("validate_output", validate_output)
    graph.add_node("create_findings", create_findings)
    graph.add_node("human_review_gate", human_review_gate)
    graph.add_node("write_audit_log", write_audit_log)

    graph.set_entry_point("normalize")
    graph.add_edge("normalize", "classify_rules")
    graph.add_edge("classify_rules", "retrieve_rag")
    graph.add_edge("retrieve_rag", "llm_reasoning")
    graph.add_edge("llm_reasoning", "validate_output")
    graph.add_conditional_edges("validate_output", _route_after_validation)
    graph.add_edge("create_findings", "human_review_gate")
    graph.add_edge("human_review_gate", "write_audit_log")
    graph.add_edge("write_audit_log", END)

    return graph


logger = logging.getLogger(__name__)

_exit_stack: AsyncExitStack | None = None
_compiled_graph = None


async def get_compiled_graph():
    """Lazily builds the checkpointer + compiled graph once per process. Call
    `close_graph_resources()` from the FastAPI lifespan shutdown to release the pool."""
    global _exit_stack, _compiled_graph
    if _compiled_graph is not None:
        return _compiled_graph

    settings = get_settings()
    _exit_stack = AsyncExitStack()
    checkpointer = await _exit_stack.enter_async_context(
        AsyncPostgresSaver.from_conn_string(settings.psycopg_database_url)
    )
    await _ensure_checkpoint_tables(checkpointer)

    _compiled_graph = build_graph().compile(checkpointer=checkpointer)
    return _compiled_graph


async def _ensure_checkpoint_tables(checkpointer) -> None:
    """Create the checkpoint tables, or confirm somebody already did.

    `setup()` issues DDL, which the application's database role is not guaranteed to
    have: running the API as a least-privilege user (migration 0018) is the whole
    point of row-level security, and such a role cannot CREATE TABLE. On those
    deployments the tables are created by the owner -- by migrate.py, or by a first
    boot that ran privileged -- and this call is the only thing that needed DDL.

    So a permission error is tolerated ONLY after confirming the tables are actually
    there. Starting up without a working checkpointer would mean a human-review pause
    silently failing to survive a restart, which is worse than refusing to start.
    """
    try:
        await checkpointer.setup()
        return
    except Exception as exc:
        if not _is_permission_error(exc):
            raise
        logger.info(
            "No DDL rights for the checkpointer; assuming its tables already exist "
            "and verifying before continuing."
        )

    try:
        await checkpointer.aget_tuple({"configurable": {"thread_id": "__startup_probe__"}})
    except Exception as exc:
        raise RuntimeError(
            "The LangGraph checkpoint tables are missing and this database role "
            "cannot create them. Run migrations as the owner, or grant the role "
            "CREATE on the schema."
        ) from exc


def _is_permission_error(exc: BaseException) -> bool:
    """psycopg raises InsufficientPrivilege; match on the SQLSTATE rather than the
    class, so this keeps working across driver versions."""
    seen: list[BaseException] = []
    err: BaseException | None = exc
    while err is not None and err not in seen:
        seen.append(err)
        if getattr(err, "sqlstate", None) == "42501":
            return True
        err = err.__cause__
    return "permission denied" in str(exc).lower()


async def close_graph_resources() -> None:
    global _exit_stack, _compiled_graph
    if _exit_stack is not None:
        await _exit_stack.aclose()
    _exit_stack = None
    _compiled_graph = None
