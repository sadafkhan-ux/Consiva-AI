"""The analysis pipeline's only door to the database. A small interface (`AnalysisRepo`) so the pipeline is tested without one, and `SqlRepo`, which opens a short session per call.

Chunk bookkeeping (`reconcile`) is pure and shared: a chunk already `done` for the same evidence, the same position and the same size is kept (so a re-run after a crash continues); anything
else is (re)written as `pending`; chunks left over from a differently sized earlier plan are deleted.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Protocol

from app.enterprise.scoring.engine import Assessment

LEVELS = ("page", "category", "cross_page", "mapping", "final")
Key = tuple[str, str, int]          # (level, source_url, chunk_index)


@dataclass
class EvRow:
    id: str
    kind: str
    source_url: str
    page_number: int
    frontier_id: str | None
    detection_method: str
    content: str


@dataclass
class FrontierRow:
    id: str
    url: str
    final_url: str | None
    status: str
    content_type: str | None


@dataclass
class ChunkRow:
    level: str
    source_url: str
    chunk_index: int
    chunk_count: int = 1
    evidence_id: str | None = None
    status: str = "pending"
    provider: str | None = None
    model: str | None = None
    input_chars: int | None = None
    attempts: int = 0
    error: str | None = None
    result: dict | None = field(default=None)

    @property
    def key(self) -> Key:
        return (self.level, self.source_url, self.chunk_index)


def reconcile(existing: list[ChunkRow], planned: list[ChunkRow]) -> tuple[list[ChunkRow], list[Key]]:
    """(rows to write, keys to delete). A `done` row that matches its plan is kept as it is."""
    have = {c.key: c for c in existing}
    write: list[ChunkRow] = []
    for p in planned:
        old = have.get(p.key)
        if old is not None and old.status == "done" and (old.chunk_count, old.evidence_id, old.input_chars) == (p.chunk_count, p.evidence_id, p.input_chars):
            continue
        write.append(replace(p, status="pending", result=None, error=None, attempts=0, provider=None, model=None))
    wanted = {p.key for p in planned}
    planned_pairs = {(p.level, p.source_url) for p in planned}
    stale = [k for k in have if (k[0], k[1]) in planned_pairs and k not in wanted]
    return write, stale


class AnalysisRepo(Protocol):
    async def set_scan_status(self, scan_id: uuid.UUID, status: str, *, started: bool = False, completed: bool = False, error: str | None = None) -> None: ...
    async def load_evidence(self, scan_id: uuid.UUID) -> list[EvRow]: ...
    async def load_frontier(self, scan_id: uuid.UUID) -> list[FrontierRow]: ...
    async def load_chunks(self, scan_id: uuid.UUID) -> list[ChunkRow]: ...
    async def upsert_chunks(self, scan_id: uuid.UUID, rows: list[ChunkRow]) -> None: ...
    async def delete_chunks(self, scan_id: uuid.UUID, keys: list[Key]) -> None: ...
    async def update_chunk(self, scan_id: uuid.UUID, key: Key, **fields: Any) -> None: ...
    async def set_frontier_status(self, scan_id: uuid.UUID, statuses: dict[str, str]) -> None: ...
    async def save_assessments(self, scan_id: uuid.UUID, assessments: list[Assessment], *, assessor: str, provider: str | None, model: str | None) -> int: ...


class SqlRepo:
    """The real thing. Every method opens its own short session so concurrent chunk tasks never share one."""

    async def _session(self):
        from app.db.session import async_session_factory
        return async_session_factory()

    async def set_scan_status(self, scan_id, status, *, started=False, completed=False, error=None) -> None:
        from sqlalchemy import update

        from app.enterprise.models import EntScan
        vals: dict[str, Any] = {"analysis_status": status, "analysis_error": error}
        now = datetime.now(timezone.utc)
        if started:
            vals["analysis_started_at"], vals["analysis_completed_at"] = now, None
        if completed:
            vals["analysis_completed_at"] = now
        async with await self._session() as db:
            await db.execute(update(EntScan).where(EntScan.id == scan_id).values(**vals))
            await db.commit()

    async def load_evidence(self, scan_id) -> list[EvRow]:
        from sqlalchemy import text
        sql = text("select id, kind, source_url, page_number, frontier_id, detection_method, content from ent_evidence where scan_id = :s "
                   "and kind in ('page_text', 'page_html', 'document_text', 'form', 'cookie', 'network', 'header', 'consent') order by captured_at, id")
        async with await self._session() as db:
            rows = (await db.execute(sql, {"s": scan_id})).all()
        return [EvRow(str(r.id), r.kind, r.source_url, r.page_number, None if r.frontier_id is None else str(r.frontier_id), r.detection_method, r.content) for r in rows]

    async def load_frontier(self, scan_id) -> list[FrontierRow]:
        from sqlalchemy import text
        async with await self._session() as db:
            rows = (await db.execute(text("select id, original_url, final_url, status, content_type from ent_frontier where scan_id = :s"), {"s": scan_id})).all()
        return [FrontierRow(str(r.id), r.original_url, r.final_url, r.status, r.content_type) for r in rows]

    async def load_chunks(self, scan_id) -> list[ChunkRow]:
        from sqlalchemy import select

        from app.enterprise.models import EntAnalysisChunk as C
        async with await self._session() as db:
            rows = (await db.scalars(select(C).where(C.scan_id == scan_id))).all()
        return [ChunkRow(r.level, r.source_url, r.chunk_index, r.chunk_count, None if r.evidence_id is None else str(r.evidence_id), r.status, r.provider, r.model, r.input_chars,
                         r.attempts, r.error, r.result) for r in rows]

    async def upsert_chunks(self, scan_id, rows: list[ChunkRow]) -> None:
        from sqlalchemy import func
        from sqlalchemy.dialects.postgresql import insert

        from app.enterprise.models import EntAnalysisChunk as C
        if not rows:
            return
        async with await self._session() as db:
            for r in rows:
                vals = dict(scan_id=scan_id, level=r.level, source_url=r.source_url, chunk_index=r.chunk_index, chunk_count=r.chunk_count,
                            evidence_id=None if r.evidence_id is None else uuid.UUID(r.evidence_id), status=r.status, provider=r.provider, model=r.model,
                            input_chars=r.input_chars, attempts=r.attempts, error=r.error, result=r.result)
                stmt = insert(C).values(**vals)
                update = {k: stmt.excluded[k] for k in ("chunk_count", "evidence_id", "status", "provider", "model", "input_chars", "attempts", "error", "result")}
                await db.execute(stmt.on_conflict_do_update(constraint="ent_analysis_chunks_unique", set_={**update, "updated_at": func.now()}))
            await db.commit()

    async def delete_chunks(self, scan_id, keys: list[Key]) -> None:
        from sqlalchemy import delete

        from app.enterprise.models import EntAnalysisChunk as C
        if not keys:
            return
        async with await self._session() as db:
            for level, url, idx in keys:
                await db.execute(delete(C).where(C.scan_id == scan_id, C.level == level, C.source_url == url, C.chunk_index == idx))
            await db.commit()

    async def update_chunk(self, scan_id, key: Key, **fields) -> None:
        from sqlalchemy import func, update

        from app.enterprise.models import EntAnalysisChunk as C
        level, url, idx = key
        async with await self._session() as db:
            await db.execute(update(C).where(C.scan_id == scan_id, C.level == level, C.source_url == url, C.chunk_index == idx).values(**fields, updated_at=func.now()))
            await db.commit()

    async def set_frontier_status(self, scan_id, statuses: dict[str, str]) -> None:
        from sqlalchemy import update

        from app.enterprise.models import EntFrontier as F
        async with await self._session() as db:
            for fid, status in statuses.items():
                await db.execute(update(F).where(F.scan_id == scan_id, F.id == uuid.UUID(fid)).values(analysis_status=status))
            await db.commit()

    async def set_frontier_tracking(self, scan_id, tracking: dict[str, dict]) -> None:
        """Keep, on each page's frontier row, how it was classified and how much of it was analysed."""
        from sqlalchemy import update

        from app.enterprise.models import EntFrontier as F
        async with await self._session() as db:
            for fid, t in tracking.items():
                await db.execute(update(F).where(F.scan_id == scan_id, F.id == uuid.UUID(fid)).values(
                    page_category=t["category"], category_reason=t["reason"], chunks_created=t["created"], chunks_analysed=t["analysed"], analysis_note=t["note"]))
            await db.commit()

    async def save_assessments(self, scan_id, assessments, *, assessor, provider, model) -> int:
        from app.enterprise import store
        async with await self._session() as db:
            n = await store.save_assessments(db, scan_id, assessments, assessor=assessor, provider=provider, model=model)
            await db.commit()
        return n
