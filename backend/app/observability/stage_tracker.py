"""Stage-level timing/status recording for the demo UI's live pipeline view and real
(not fabricated) latency measurement. One call site per pipeline stage:

    async with track_stage(scan_id, "rag_retrieval") as meta:
        chunks = await retrieve(...)
        meta["chunks_retrieved"] = len(chunks)

Opens its own short-lived DB sessions per write (start, then finish) rather than
holding one connection across the whole stage — stages can be slow (browser crawls,
LLM calls) and shouldn't pin a pool connection for their entire duration.

Deliberately best-effort: a failure to WRITE a stage record (DB hiccup, or no DB at
all — e.g. plain unit tests) never breaks the actual pipeline logic being tracked.
Only exceptions raised by the wrapped code itself propagate.
"""

import logging
import time
import uuid
from contextlib import asynccontextmanager

from app.db.repositories import stage_repository
from app.db.session import async_session_factory

logger = logging.getLogger(__name__)


async def _start(scan_id: uuid.UUID, stage: str, agent_run_id: uuid.UUID | None) -> uuid.UUID | None:
    try:
        async with async_session_factory() as db:
            row = await stage_repository.start_stage(db, scan_id=scan_id, agent_run_id=agent_run_id, stage=stage)
            await db.commit()
            return row.id
    except Exception:
        logger.warning("Could not record stage start for %s/%s", scan_id, stage, exc_info=True)
        return None


async def _finish(stage_id: uuid.UUID | None, *, ok: bool, duration_ms: int, metadata: dict, error: str) -> None:
    if stage_id is None:
        return
    try:
        async with async_session_factory() as db:
            if ok:
                await stage_repository.complete_stage(db, stage_id, duration_ms=duration_ms, metadata=metadata)
            else:
                await stage_repository.fail_stage(db, stage_id, duration_ms=duration_ms, error=error)
            await db.commit()
    except Exception:
        logger.warning("Could not record stage finish for stage_id=%s", stage_id, exc_info=True)


@asynccontextmanager
async def track_stage(scan_id: uuid.UUID, stage: str, *, agent_run_id: uuid.UUID | None = None):
    stage_id = await _start(scan_id, stage, agent_run_id)
    start = time.monotonic()
    metadata: dict = {}
    try:
        yield metadata
    except Exception as exc:
        duration_ms = int((time.monotonic() - start) * 1000)
        await _finish(stage_id, ok=False, duration_ms=duration_ms, metadata=metadata, error=str(exc))
        raise
    else:
        duration_ms = int((time.monotonic() - start) * 1000)
        await _finish(stage_id, ok=True, duration_ms=duration_ms, metadata=metadata, error="")
