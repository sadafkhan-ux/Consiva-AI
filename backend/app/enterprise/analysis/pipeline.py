"""The text-analysis pipeline for one Enterprise scan.

    run_text_analysis(scan_id, org_id, llm=None, as_of=None) -> AnalysisOutcome

Order of work: mark the scan `running`; load the stored evidence; classify every page (deterministically, and by one small model question for an ambiguous page); split each relevant page,
document page and form set into chunks and WRITE EVERY CHUNK as `pending` before any model call; read the chunks (bounded concurrency, each updated to done or failed afterwards); group the
facts, run the few interpretations and the cross-page comparison (each also a persisted unit); map onto the parameters; save the assessments; record per page whether it was analysed; and set
the scan's analysis status.

Resumable: a chunk already `done` for the same evidence, position and size is not read again, so a run after a crash continues. If any page chunk had to be re-planned, the later levels are
recomputed. Nothing is truncated: every chunk of every relevant source is either done, failed or pending, and the outcome says how many of each.

A failure of the model is a failed chunk and, at most, a parameter that is NOT ASSESSED. It never becomes a finding about the site.

Scan analysis status: `completed` when every unit is done; `partial` when some failed or are still pending; `failed` when units were planned and none is done, or when there was no text to read.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from pydantic import BaseModel

from app.enterprise.analysis import consolidate as co
from app.enterprise.analysis import pages as pg
from app.enterprise.analysis import sources as sr
from app.enterprise.analysis.chunking import Chunk, chunk_text
from app.enterprise.analysis.constants import CONCURRENCY
from app.enterprise.analysis.extract import extract_chunk
from app.enterprise.analysis.llm import Answer, LLMCallable, LLMFailure, call_structured, default_llm
from app.enterprise.analysis.repo import AnalysisRepo, ChunkRow, Key, SqlRepo, reconcile
from app.enterprise.scoring.engine import Assessment

logger = logging.getLogger(__name__)

RULE_ASSESSOR = "rule:text"
LLM_ASSESSOR = "llm"


class PageKind(BaseModel):
    category: Literal["privacy_policy", "cookie_policy", "terms", "contact", "grievance", "about", "blog_article", "product", "other"]
    confidence: Literal["high", "medium", "low"]


@dataclass
class AnalysisOutcome:
    status: str = "not_started"
    chunks_done: int = 0
    chunks_failed: int = 0
    chunks_pending: int = 0
    chunks_skipped: int = 0
    facts_kept: int = 0
    facts_discarded: int = 0                    # facts whose quote was not found in the text: never used
    parameters_assessed: int = 0
    assessments: list[Assessment] = field(default_factory=list)
    observations: dict = field(default_factory=dict)
    conflicts: list[dict] = field(default_factory=list)
    pages_by_category: dict[str, int] = field(default_factory=dict)
    provider: str | None = None
    model: str | None = None
    error: str | None = None


@dataclass
class _Work:
    row: ChunkRow
    src: sr.Src
    chunk: Chunk


def _plan(loaded: sr.Loaded) -> list[_Work]:
    """Every chunk of every relevant source, with its chunk row. Chunk numbering runs across all the pages of one document, so (url, index) is unique."""
    work: list[_Work] = []
    by_url: dict[str, list[sr.Src]] = {}
    for s in loaded.texts:
        if s.category in pg.ANALYSED:
            by_url.setdefault(s.url, []).append(s)
    for url, srcs in sorted(by_url.items()):
        pieces = [(s, c) for s in sorted(srcs, key=lambda x: x.page_number) for c in chunk_text(s.text)]
        for i, (s, c) in enumerate(pieces):
            work.append(_Work(ChunkRow("page", url, i, len(pieces), s.evidence_id, "pending", None, None, len(c.text)), s, c))
    for s in loaded.forms:
        for c in chunk_text(s.text):
            work.append(_Work(ChunkRow("page", f"{s.url}#forms", c.index, c.count, s.evidence_id, "pending", None, None, len(c.text)), s, c))
    return work


async def run_text_analysis(scan_id: uuid.UUID, org_id: uuid.UUID, *, llm: LLMCallable | None = None, as_of: date | None = None, repo: AnalysisRepo | None = None) -> AnalysisOutcome:
    if repo is None:
        from app.db.session import set_org_scope
        set_org_scope(org_id)
        repo = SqlRepo()
    llm = llm or default_llm(scan_id=None)       # no scan id for usage tracking: its table references the 5-page scans; the call is still logged to the usage file
    as_of = as_of or date.today()
    outcome = AnalysisOutcome(status="running")
    await repo.set_scan_status(scan_id, "running", started=True)
    try:
        await _run(scan_id, repo, llm, as_of, outcome)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception("enterprise scan %s: the text analysis stopped on an unexpected error", scan_id)
        outcome.status, outcome.error = "failed", f"{type(exc).__name__}: {str(exc)[:300]}"
        await repo.set_scan_status(scan_id, "failed", completed=True, error=outcome.error)
    return outcome


async def _run(scan_id: uuid.UUID, repo: AnalysisRepo, llm: LLMCallable, as_of: date, out: AnalysisOutcome) -> None:
    loaded = sr.build(await repo.load_evidence(scan_id), await repo.load_frontier(scan_id))
    sem = asyncio.Semaphore(CONCURRENCY)
    extra_keys: list[Key] = []                      # mapping / cross-page units created in this run

    async def unit(row: ChunkRow, fn: Callable[[], Awaitable[tuple[dict, Answer | None]]], existing: dict[Key, ChunkRow]) -> dict | None:
        """One persisted unit of work. A `done` row is reused; otherwise it is written pending, run, and updated. Returns the result, or None when it failed."""
        old = existing.get(row.key)
        if old is not None and old.status == "done" and old.result is not None:
            return old.result
        if old is None or old.status != "pending":
            await repo.upsert_chunks(scan_id, [row])
        async with sem:
            try:
                result, ans = await fn()
            except LLMFailure as exc:
                await repo.update_chunk(scan_id, row.key, status="failed", error=str(exc)[:500], attempts=exc.attempts, provider=exc.provider, model=exc.model)
                return None
            except Exception as exc:                  # an unexpected bug inside one unit is that unit's failure
                logger.exception("enterprise scan %s: unit %s failed", scan_id, row.key)
                await repo.update_chunk(scan_id, row.key, status="failed", error=f"{type(exc).__name__}: {str(exc)[:300]}")
                return None
            await repo.update_chunk(scan_id, row.key, status="done", result=result, error=None, attempts=ans.attempts if ans else 0, provider=ans.provider if ans else None,
                                    model=ans.model if ans else None)
            return result

    # ---- 1. ambiguous pages: one small question each ----
    existing = {c.key: c for c in await repo.load_chunks(scan_id)}

    async def settle(page: sr.Page) -> None:
        facts = pg.PageFacts(page.url, page.title, page.headings, next((s.text for s in loaded.texts if s.url == page.url), ""))
        row = ChunkRow("category", page.url, 0, 1, next((s.evidence_id for s in loaded.texts if s.url == page.url), None), "pending", None, None, len(facts.text[:1500]))

        async def fn() -> tuple[dict, Answer]:
            ans = await call_structured(llm, pg.AMBIGUOUS_SYSTEM, pg.ambiguous_prompt(facts, page.candidates), PageKind)
            return ans.parsed.model_dump(), ans            # type: ignore[attr-defined]

        res = await unit(row, fn, existing)
        if res and res["confidence"] != "low":
            sr.apply_category(loaded, page.url, res["category"], f"the page was ambiguous; the model read it as {res['category']} ({res['confidence']} confidence)")
        elif res is None:
            page.reasons = page.reasons + ("the model could not settle the page; it was not treated as a policy",)

    await asyncio.gather(*(settle(p) for p in list(loaded.pages.values()) if p.unresolved and not p.is_document))
    for p in loaded.pages.values():
        if p.unresolved and p.is_document:
            p.unresolved = True                           # a document that cannot be settled stays unresolved; there is no model question for it beyond the text
    unresolved = sorted(p.url for p in loaded.pages.values() if p.unresolved)
    out.pages_by_category = dict(Counter(p.category for p in loaded.pages.values()))

    # ---- 2. plan and persist every chunk before any model call ----
    work = _plan(loaded)
    planned_rows = [w.row for w in work]
    existing = {c.key: c for c in await repo.load_chunks(scan_id)}
    to_write, stale = reconcile(list(existing.values()), planned_rows)
    if to_write or stale:
        later = [k for k in existing if k[0] in ("mapping", "cross_page", "final")]
        await repo.delete_chunks(scan_id, stale + later)            # a re-planned page chunk makes the later levels stale
        for k in later:
            existing.pop(k, None)
        await repo.upsert_chunks(scan_id, to_write)
        existing = {c.key: c for c in await repo.load_chunks(scan_id)}
    else:
        pass

    # ---- 3. level 1: read the chunks ----
    async def read(w: _Work) -> None:
        async def fn() -> tuple[dict, Answer]:
            ex = await extract_chunk(llm, url=w.src.url, kind=w.src.kind, chunk_text=w.chunk.text, index=w.row.chunk_index, count=w.row.chunk_count)
            return {"facts": ex.facts, "raw": ex.raw, "discarded": ex.discarded, "evidence_id": w.src.evidence_id, "page_number": w.src.page_number}, ex.answer

        await unit(w.row, fn, existing)

    await asyncio.gather(*(read(w) for w in work))
    chunks_now = {c.key: c for c in await repo.load_chunks(scan_id)}

    # ---- 4. facts and what was not read ----
    facts: list[dict] = []
    unread: dict[str, int] = {}
    seen: set[tuple] = set()
    for w in work:
        c = chunks_now.get(w.row.key)
        cat = "forms" if w.src.kind == "form_text" else w.src.category
        if c is None or c.status != "done":
            unread[cat] = unread.get(cat, 0) + 1
            continue
        for f in (c.result or {}).get("facts", []):
            k = (f["topic"], f["quote"], w.src.evidence_id)
            if k in seen:
                continue
            seen.add(k)
            facts.append({**f, "id": len(facts), "evidence_id": w.src.evidence_id, "url": w.src.url, "page_number": w.src.page_number, "category": cat if cat != "forms" else w.src.category,
                          "kind": w.src.kind})
    ctx = co.Ctx(loaded, facts, unread, as_of, unresolved=unresolved)

    # ---- 5. levels 2 to 4: interpret, compare, map ----
    existing = {c.key: c for c in await repo.load_chunks(scan_id)}

    async def run_mapping(name: str, idx: int, count: int, fn: Callable[[], Awaitable[tuple[dict, Answer | None]]]) -> dict | None:
        level = "cross_page" if name == "conflicts" else "mapping"
        row = ChunkRow(level, name, idx, count, None, "pending", None, None, None)
        extra_keys.append(row.key)
        return await unit(row, fn, existing)

    await co.interpret(ctx, llm, run_mapping)
    out.conflicts = await co.find_conflicts(ctx, llm, run_mapping)
    out.observations = co.observations(ctx)
    assessments = co.assess_all(ctx)
    out.assessments = assessments

    # ---- 6. status of every unit, and who answered ----
    final = {c.key: c for c in await repo.load_chunks(scan_id)}
    counted = [final[w.row.key] for w in work if w.row.key in final] + [final[k] for k in extra_keys if k in final] + [
        final[("category", p.url, 0)] for p in loaded.pages.values() if ("category", p.url, 0) in final]
    out.chunks_done = sum(1 for c in counted if c.status == "done")
    out.chunks_failed = sum(1 for c in counted if c.status == "failed")
    out.chunks_skipped = sum(1 for c in counted if c.status == "skipped")
    out.chunks_pending = sum(1 for c in counted if c.status == "pending") + max(0, len(work) - sum(1 for w in work if w.row.key in final))
    out.facts_kept = sum(len((c.result or {}).get("facts", [])) for c in counted if c.level == "page" and c.status == "done")
    out.facts_discarded = sum((c.result or {}).get("discarded", 0) for c in counted if c.level == "page" and c.status == "done")
    pm = Counter((c.provider, c.model) for c in counted if c.status == "done" and c.provider)
    if pm:
        (out.provider, out.model), _ = pm.most_common(1)[0]

    # ---- 7. save the assessments ----
    by_llm = [a for a in assessments if a.parameter_id in co.LLM_PARAMS]
    by_rule = [a for a in assessments if a.parameter_id not in co.LLM_PARAMS]
    written = await repo.save_assessments(scan_id, by_llm, assessor=LLM_ASSESSOR, provider=out.provider, model=out.model)
    written += await repo.save_assessments(scan_id, by_rule, assessor=RULE_ASSESSOR, provider=None, model=None)
    out.parameters_assessed = len(assessments)
    await repo.upsert_chunks(scan_id, [ChunkRow("final", "*", 0, 1, None, "done", out.provider, out.model, None, 0, None, {
        "states": {a.parameter_id: a.state for a in assessments}, "observations": out.observations, "conflicts": out.conflicts, "assessments_written": written})])

    # ---- 8. what happened to each page and document ----
    statuses = _frontier_statuses(loaded, final, work)
    await repo.set_frontier_status(scan_id, statuses)
    setter = getattr(repo, "set_frontier_tracking", None)          # a repository without per-page tracking (a test double) simply does not record it
    if setter is not None:
        await setter(scan_id, _page_tracking(loaded, final, work))

    # ---- 9. the scan's own status ----
    planned = len(work) + len(extra_keys)
    if not loaded.texts and not loaded.forms:
        out.status, out.error = "failed", "no page or document text was stored for this scan, so there was nothing to read"
    elif planned and out.chunks_done == 0 and (out.chunks_failed or out.chunks_pending):
        out.status = "failed"
        out.error = _errors(counted) or "no unit of work could be completed"
    elif out.chunks_failed or out.chunks_pending:
        out.status, out.error = "partial", _errors(counted)
    else:
        out.status = "completed"
    await repo.set_scan_status(scan_id, out.status, completed=True, error=out.error)


def _errors(counted: list[ChunkRow]) -> str | None:
    bad = [c for c in counted if c.status == "failed"]
    if not bad:
        return None
    first = "; ".join(f"{c.source_url} #{c.chunk_index}: {(c.error or '')[:120]}" for c in bad[:3])
    return f"{len(bad)} unit(s) of analysis failed: {first}"[:900]


def _page_tracking(loaded: sr.Loaded, chunks: dict[Key, ChunkRow], work: list[_Work]) -> dict[str, dict]:
    """Per page: how it was classified and why, how many passages were created and analysed, and, when the page was not read in full or not read at all, the reason in words. This is what
    lets a reader find exactly where content was skipped; before it, `not_applicable` covered an article, an empty page and a page the model never saw alike."""
    out: dict[str, dict] = {}
    for page in loaded.pages.values():
        mine = [w for w in work if w.src.url == page.url]
        done = sum(1 for w in mine if chunks.get(w.row.key) is not None and chunks[w.row.key].status == "done")
        failed = [chunks[w.row.key] for w in mine if chunks.get(w.row.key) is not None and chunks[w.row.key].status == "failed"]
        why = ""
        if not mine:
            if page.unresolved:
                why = "not read: the page could not be classified as a notice or something else, so it was left out of the analysis"
            elif page.category not in pg.ANALYSED:
                why = f"not sent to the model: classified as {page.category.replace('_', ' ')} ({page.reasons[0] if page.reasons else 'no reason recorded'})"
            else:
                why = "no text was stored for this page"
        elif failed:
            why = f"{len(failed)} of {len(mine)} passages could not be analysed" + (f" ({failed[0].error[:120]})" if failed[0].error else "")
        elif done < len(mine):
            why = f"{len(mine) - done} of {len(mine)} passages were not analysed yet"
        info = {"category": page.category, "reason": "; ".join(page.reasons)[:600], "created": len(mine), "analysed": done, "note": why or None}
        for fid in page.frontier_ids:
            out[fid] = info
    return out


def _frontier_statuses(loaded: sr.Loaded, chunks: dict[Key, ChunkRow], work: list[_Work]) -> dict[str, str]:
    """done: every chunk of the page's own text and forms finished. failed: any did not. not_applicable: nothing on the page needed reading (an asset, an excluded page, an article)."""
    own: dict[str, list[ChunkRow]] = {}
    for w in work:
        c = chunks.get(w.row.key)
        url = w.src.url
        if c is not None:
            own.setdefault(url, []).append(c)
            for covered in loaded.form_covers.get(url, []) if w.src.kind == "form_text" else []:
                own.setdefault(covered, []).append(c)
    by_url = {}
    for url, cs in own.items():
        by_url[url] = "done" if all(c.status == "done" for c in cs) else "failed"
    statuses: dict[str, str] = {}
    for fr in loaded.frontier:
        st = by_url.get(fr.final_url or "") or by_url.get(fr.url)
        statuses[fr.id] = st or "not_applicable"
    for page in loaded.pages.values():
        for fid in page.frontier_ids:
            if page.url in by_url:
                statuses[fid] = by_url[page.url]
    return statuses
