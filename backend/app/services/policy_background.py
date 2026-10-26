"""The privacy-policy reading runs beside the compliance graph, not before it.

`policy_analysis` (one LLM call, 18-28 s measured) and the graph (rules, retrieval, the compliance-reasoning LLM call) read different inputs and
write different results: nothing in the graph reads the policy result, which only lands in its own stage record. Running them one after the other
made every analysis wait for both in turn. They now run together, so the analysis takes as long as the slower of the two. The number of model calls
and their tokens are unchanged.

The one rule that used to hold by ordering is kept explicitly: a run is not marked completed until its policy reading is finished, so a client that
sees "completed" always finds the policy result (see agents/consent_agent/nodes/audit.py, which calls `wait`).
"""

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

_tasks: dict[uuid.UUID, asyncio.Task] = {}


def start(agent_run_id: uuid.UUID, work: Callable[[], Awaitable[None]]) -> asyncio.Task:
    """Begin `work` in the background for this run. `work` must never raise (the policy reading is fail-soft); anything it does raise is logged, not propagated."""
    async def guarded() -> None:
        try:
            await work()
        except Exception:  # noqa: BLE001 -- a policy reading must never fail an analysis
            logger.warning("Background policy analysis for run %s failed", agent_run_id, exc_info=True)

    task = asyncio.create_task(guarded(), name=f"policy-analysis-{agent_run_id}")
    _tasks[agent_run_id] = task
    return task


async def wait(agent_run_id: uuid.UUID) -> None:
    """Return once this run's policy reading has finished; at once when there is none (an analysis resumed in another process, or a run with no policy)."""
    task = _tasks.get(agent_run_id)
    if task is None:
        return
    try:
        await asyncio.shield(task)
    except Exception:  # noqa: BLE001 -- already logged by `guarded`
        pass


def forget(agent_run_id: uuid.UUID) -> None:
    _tasks.pop(agent_run_id, None)
