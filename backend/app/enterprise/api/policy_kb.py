"""Enterprise policy knowledge base endpoints. Prefix /api/v1/enterprise/policy-kb (router.py).

Everything is scoped to the caller's organisation: a document, version or website of another organisation is "not found", and a search can only return the caller's own current chunks.
"""

from __future__ import annotations

import functools
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConsivaError, NotFoundError
from app.core.integration_auth import get_caller
from app.core.security import CurrentUser
from app.db.session import get_db
from app.enterprise import errors
from app.enterprise.config import get_enterprise_settings
from app.enterprise.policykb import history, metrics, retrieve
from app.monitoring import events as error_events

router = APIRouter(prefix="/policy-kb", tags=["Enterprise policy knowledge base"])


def guarded(operation: str):
    """A failure of the knowledge base is never answered with an empty list or a bare 500. It is recorded as an error event (an administrator is emailed once), and the caller gets a 503 that says
    what failed and carries the error id, so the screen can show a clear error instead of a knowledge base that looks empty. A call that works clears an earlier failure."""
    def deco(fn):
        @functools.wraps(fn)
        async def inner(*args, **kwargs):
            try:
                result = await fn(*args, **kwargs)
            except (HTTPException, ConsivaError):
                raise
            except Exception as exc:
                rec = await error_events.record_error(agent="enterprise", module=f"policy_kb.{operation}", exc=exc, service="policy_kb", upstream=False)
                ident = rec["error_id"] if rec else None
                category = rec["category"] if rec else "rag_failure"
                raise HTTPException(status_code=503, detail=f"The policy knowledge base could not answer ({category.replace('_', ' ')})." + (f" Error {ident} was recorded and the administrator was notified." if ident else ""),
                                    headers={"X-Error-ID": ident or ""}) from exc
            await error_events.record_success("policy_kb", agent="enterprise")
            return result
        return inner
    return deco


def _require_enabled() -> None:
    if not get_enterprise_settings().enabled:
        raise errors.EnterpriseDisabled("Not found")


@router.get("/metrics", summary="Policy knowledge base figures, from stored data")
@guarded("get_metrics")
async def get_metrics(domain: str | None = Query(default=None, max_length=253), user: CurrentUser = Depends(get_caller)) -> dict:
    _require_enabled()
    return await metrics.policy_kb_metrics(uuid.UUID(user.org_id), domain)


@router.get("/documents", summary="The policy documents kept for the organisation's websites")
@guarded("documents")
async def documents(domain: str | None = Query(default=None, max_length=253), status: str | None = Query(default=None, pattern="^(active|removed)$"),
                    user: CurrentUser = Depends(get_caller), db: AsyncSession = Depends(get_db)) -> dict:
    _require_enabled()
    where, params = ["d.org_id = :o"], {"o": uuid.UUID(user.org_id)}
    if domain:
        where.append("d.domain = :dom")
        params["dom"] = domain
    if status:
        where.append("d.status = :st")
        params["st"] = status
    rows = (await db.execute(text(
        "select d.id, d.domain, d.identity_key as url, d.doc_type, d.status, d.stale, d.stale_reason, d.url_history, d.first_seen_at, d.last_seen_at, d.last_verified_at, "
        "v.version_no as current_version, v.title, v.content_hash, v.text_chars, v.chunk_count, v.extraction_status, v.extraction_note, v.sync_status, v.embedding_model, v.classification_method, "
        "v.classification_confidence, v.extracted_at, (select count(*) from ent_policy_versions x where x.doc_id = d.id) as versions "
        f"from ent_policy_docs d left join ent_policy_versions v on v.id = d.current_version_id where {' and '.join(where)} order by d.domain, d.doc_type, d.identity_key"), params)).mappings().all()
    return {"items": [dict(r) for r in rows], "total": len(rows)}


async def _doc(db: AsyncSession, doc_id: uuid.UUID, user: CurrentUser) -> uuid.UUID:
    _require_enabled()
    found = await db.scalar(text("select id from ent_policy_docs where id = :d and org_id = :o"), {"d": doc_id, "o": uuid.UUID(user.org_id)})
    if found is None:
        raise NotFoundError(f"Policy document {doc_id} not found")
    return found


@router.get("/documents/{doc_id}/versions", summary="Every version of a document, newest first")
@guarded("versions")
async def versions(doc_id: uuid.UUID, user: CurrentUser = Depends(get_caller), db: AsyncSession = Depends(get_db)) -> dict:
    await _doc(db, doc_id, user)
    items = await history.list_versions(uuid.UUID(user.org_id), doc_id)
    return {"items": items, "total": len(items)}


@router.get("/documents/{doc_id}/diff", summary="What changed between two versions of a document")
@guarded("diff")
async def diff(doc_id: uuid.UUID, from_version: int = Query(ge=1), to_version: int = Query(ge=1), user: CurrentUser = Depends(get_caller), db: AsyncSession = Depends(get_db)) -> dict:
    await _doc(db, doc_id, user)
    try:
        d = await history.diff_versions(uuid.UUID(user.org_id), doc_id, from_version, to_version)
    except LookupError as exc:
        raise NotFoundError(str(exc)) from exc
    doc_type = await db.scalar(text("select doc_type from ent_policy_docs where id = :d"), {"d": doc_id})
    return {**d, "parameters_to_reassess": history.affected_parameters([doc_type])}


@router.get("/documents/{doc_id}/text", summary="The complete stored text of a version (the current one by default)")
@guarded("version_text")
async def version_text(doc_id: uuid.UUID, version: int | None = Query(default=None, ge=1), user: CurrentUser = Depends(get_caller), db: AsyncSession = Depends(get_db)) -> dict:
    await _doc(db, doc_id, user)
    q = ("select version_no, text, content_hash, final_url, extracted_at, extraction_status, extraction_note, is_current from ent_policy_versions where doc_id = :d and org_id = :o and "
         + ("version_no = :n" if version else "is_current"))
    row = (await db.execute(text(q), {"d": doc_id, "o": uuid.UUID(user.org_id), "n": version})).mappings().first()
    if row is None:
        raise NotFoundError("That version does not exist")
    return dict(row)


class SearchRequest(BaseModel):
    domain: str = Field(min_length=1, max_length=253)
    query: str = Field(min_length=2, max_length=500)
    doc_types: list[str] | None = None
    top_k: int = Field(default=6, ge=1, le=20)


@router.post("/search", summary="Hybrid (keyword and semantic) search over a website's current policy text")
@guarded("search")
async def search(body: SearchRequest, user: CurrentUser = Depends(get_caller)) -> dict:
    _require_enabled()
    return await retrieve.retrieve(uuid.UUID(user.org_id), body.domain, body.query, doc_types=body.doc_types, top_k=body.top_k)


@router.get("/consistency", summary="Checks that the knowledge base agrees with itself")
@guarded("consistency")
async def consistency(domain: str | None = Query(default=None, max_length=253), user: CurrentUser = Depends(get_caller)) -> dict:
    _require_enabled()
    issues = await history.verify_consistency(uuid.UUID(user.org_id), domain)
    return {"consistent": not issues, "issues": issues}


@router.post("/repair-embeddings", summary="Embed searchable chunks that have no embedding")
@guarded("repair")
async def repair(domain: str | None = Query(default=None, max_length=253), user: CurrentUser = Depends(get_caller)) -> dict:
    _require_enabled()
    return await history.repair_embeddings(uuid.UUID(user.org_id), domain)


@router.get("/sync-log", summary="What each synchronisation did")
@guarded("sync_log")
async def sync_log(domain: str | None = Query(default=None, max_length=253), limit: int = Query(50, ge=1, le=200), user: CurrentUser = Depends(get_caller), db: AsyncSession = Depends(get_db)) -> dict:
    _require_enabled()
    where, params = ["org_id = :o"], {"o": uuid.UUID(user.org_id), "n": limit}
    if domain:
        where.append("domain = :dom")
        params["dom"] = domain
    rows = (await db.execute(text(f"select id, scan_id, doc_id, version_id, domain, action, detail, created_at from ent_policy_sync_log where {' and '.join(where)} order by created_at desc limit :n"), params)).mappings().all()
    return {"items": [dict(r) for r in rows], "total": len(rows)}
