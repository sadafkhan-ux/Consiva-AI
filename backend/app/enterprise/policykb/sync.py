"""Keep each website's policy knowledge base in step with what a scan found.

For one finished scan: take the pages the analysis read as policies (privacy, cookie, terms, grievance, contact), clean them, hash them, and compare with the stored documents of that website.

| What the scan found                         | What happens                                                                                        |
|---------------------------------------------|-----------------------------------------------------------------------------------------------------|
| same document, same content hash            | nothing is re-chunked or re-embedded; the document is re-verified (last_verified_at)                |
| same document, different content            | a new version; chunks that did not change reuse their stored embedding; the old version is kept   |
| a document that was not there before        | extracted, chunked, embedded, stored as version 1                                                   |
| a known document at a new address           | the document keeps its identity; the address is added to its history; no new version if unchanged  |
| a known document that answered 404 or 410   | marked removed; versions and chunks kept as history, chunks no longer searched                      |
| a known document that could not be fetched  | the last verified version stays current and the document is marked stale, with the reason          |
| the whole site could not be read            | nothing is overwritten; every active document is marked stale                                       |

A new version becomes current only after its chunks exist and every embedding was produced or reused, and then in one transaction: a failure at any earlier step leaves the last verified version
exactly as it was. A processing failure is never recorded as a compliance finding.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from sqlalchemy import text

from app.db.session import async_session_factory, set_org_scope
from app.enterprise.analysis import pages as pg
from app.enterprise.coverage import LIMIT_STOPS
from app.enterprise.policykb import sections as sec
from app.scanner.url_normalize import normalize_url

logger = logging.getLogger(__name__)

#: The classifier's categories that are kept as policy documents, and the name each is stored under.
KB_TYPES = {pg.PRIVACY: "privacy_policy", pg.COOKIE: "cookie_policy", pg.TERMS: "terms", pg.GRIEVANCE: "grievance", pg.CONTACT: "contact"}

EMBED_BATCH = 32
EMBED_ATTEMPTS = 3
EMBED_TIMEOUT_S = 45.0
EMBED_BACKOFF_S = (1.0, 3.0)
_NAMESPACE = uuid.UUID("6f1b2a56-6d5e-4f0a-9f43-0c1a8c1f9d10")

Embedder = Callable[[list[str]], Awaitable[list[list[float]]]]


class EmbeddingFailed(Exception):
    pass


def chunk_id(version_id: uuid.UUID, index: int) -> uuid.UUID:
    """Stable: the same version and position always give the same id."""
    return uuid.uuid5(_NAMESPACE, f"{version_id}:{index}")


def embedding_model_name() -> str:
    from app.config import get_settings
    return get_settings().nvidia_embed_model


def embedding_dimension() -> int:
    from app.config import get_settings
    return get_settings().nvidia_embed_dimensions


async def default_embedder(texts: list[str]) -> list[list[float]]:
    """The embedding model the 5-page product's knowledge base already uses (passage side), called through the existing client and its hedged retries."""
    from app.llm.client import NvidiaLLMClient
    client = NvidiaLLMClient()
    return await client.embed(texts, input_type="passage")


async def default_query_embedder(texts: list[str]) -> list[list[float]]:
    from app.llm.client import NvidiaLLMClient
    client = NvidiaLLMClient()
    return await client.embed(texts, input_type="query")


async def embed_all(texts: list[str], embedder: Embedder) -> list[list[float]]:
    """Embed in batches, retrying each batch; one batch that cannot be embedded fails the whole call (the caller then keeps the last verified version)."""
    out: list[list[float]] = []
    for i in range(0, len(texts), EMBED_BATCH):
        batch = texts[i:i + EMBED_BATCH]
        last: Exception | None = None
        for attempt in range(EMBED_ATTEMPTS):
            try:
                vectors = await asyncio.wait_for(embedder(batch), timeout=EMBED_TIMEOUT_S)
                if len(vectors) != len(batch):
                    raise EmbeddingFailed(f"the embedding model returned {len(vectors)} vectors for {len(batch)} passages")
                dim = embedding_dimension()
                if any(len(v) != dim for v in vectors):
                    raise EmbeddingFailed(f"the embedding model returned vectors that are not {dim}-dimensional")
                out.extend(vectors)
                break
            except Exception as exc:                              # a timeout, a provider error and a wrong shape are all the same to the caller: not embedded
                last = exc
                if attempt < EMBED_ATTEMPTS - 1:
                    await asyncio.sleep(EMBED_BACKOFF_S[min(attempt, len(EMBED_BACKOFF_S) - 1)])
        else:
            raise EmbeddingFailed(f"{type(last).__name__}: {str(last)[:200]}")
    return out


def _vec(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.7g}" for x in v) + "]"


@dataclass
class SyncResult:
    domain: str = ""
    counts: dict[str, int] = field(default_factory=lambda: {"new": 0, "unchanged": 0, "changed": 0, "removed": 0, "url_changed": 0, "extraction_failed": 0, "sync_failed": 0, "marked_stale": 0})
    embeddings_generated: int = 0
    embeddings_reused: int = 0
    documents: list[dict] = field(default_factory=list)
    changed_types: set[str] = field(default_factory=set)
    errors: list[str] = field(default_factory=list)


async def _log(db, org_id, scan_id, domain, action: str, doc_id=None, version_id=None, **detail) -> None:
    await db.execute(text("insert into ent_policy_sync_log (org_id, scan_id, doc_id, version_id, domain, action, detail) values (:o, :s, :d, :v, :dom, :a, cast(:det as jsonb))"),
                     {"o": org_id, "s": scan_id, "d": doc_id, "v": version_id, "dom": domain, "a": action, "det": json.dumps(detail, default=str)})


def _identity(url: str) -> str:
    return normalize_url(url)


async def _create_version(db, *, org_id, scan_id, domain, doc_id, doc_type: str, original_url: str, final_url: str, title: str | None, clean: sec.CleanDoc, content_hash: str,
                          method: str, confidence: str | None, evidence_ids: list, extraction_status: str, note: str | None, embedder: Embedder, result: SyncResult) -> uuid.UUID:
    """Create the next version of a document. The version row is written first as pending; its chunks are built and embedded; then, in one transaction, the chunks are stored and the version
    becomes current. On any failure the version is marked failed and the previous current version is untouched."""
    version_no = (await db.scalar(text("select coalesce(max(version_no), 0) + 1 from ent_policy_versions where doc_id = :d"), {"d": doc_id}))
    version_id = await db.scalar(text(
        "insert into ent_policy_versions (org_id, doc_id, version_no, scan_id, original_url, final_url, title, doc_type, classification_method, classification_confidence, content_hash, text, text_chars,"
        " extraction_status, extraction_note, sync_status, source_evidence_ids) values (:o, :d, :n, :s, :ou, :fu, :t, :dt, :m, :c, :h, :x, :xc, :es, :en, 'pending', :ev) returning id"),
        {"o": org_id, "d": doc_id, "n": version_no, "s": scan_id, "ou": original_url, "fu": final_url, "t": title, "dt": doc_type, "m": method, "c": confidence, "h": content_hash, "x": clean.text,
         "xc": len(clean.text), "es": extraction_status, "en": note, "ev": [uuid.UUID(str(e)) for e in evidence_ids]})
    await db.commit()                                                 # the pending version is visible (and counted as awaiting) while it is processed

    chunks = sec.chunk_document(clean)
    try:
        if not chunks:
            raise EmbeddingFailed("the cleaned text produced no chunks")
        known = {r.content_hash: r.embedding for r in (await db.execute(text(
            "select distinct on (content_hash) content_hash, embedding::text as embedding from ent_policy_chunks where org_id = :o and doc_id = :d and embedding is not null order by content_hash, created_at desc"),
            {"o": org_id, "d": doc_id})).all()}
        to_embed = [c for c in chunks if c.content_hash not in known]
        vectors = await embed_all([c.content for c in to_embed], embedder) if to_embed else []
        fresh = {c.content_hash: _vec(v) for c, v in zip(to_embed, vectors)}
    except Exception as exc:
        await db.rollback()
        await db.execute(text("update ent_policy_versions set sync_status = 'failed', sync_error = :e where id = :v"), {"e": str(exc)[:500], "v": version_id})
        await db.commit()
        raise

    model, dim = embedding_model_name(), embedding_dimension()
    # one transaction: store chunks, retire the old current version and its chunks, make this one current
    for c in chunks:
        vec = known.get(c.content_hash) or fresh[c.content_hash]
        await db.execute(text(
            "insert into ent_policy_chunks (id, org_id, doc_id, version_id, domain, doc_type, source_url, chunk_index, section_heading, content, content_hash, token_estimate, embedding, embedding_source, is_active)"
            " values (:id, :o, :d, :v, :dom, :dt, :u, :i, :h, :c, :ch, :te, cast(:e as vector), :src, false)"),
            {"id": chunk_id(version_id, c.index), "o": org_id, "d": doc_id, "v": version_id, "dom": domain, "dt": doc_type, "u": final_url, "i": c.index, "h": c.heading, "c": c.content, "ch": c.content_hash,
             "te": c.token_estimate, "e": vec, "src": "reused" if c.content_hash in known else "generated"})
    await db.execute(text("update ent_policy_chunks set is_active = false where doc_id = :d and is_active"), {"d": doc_id})
    await db.execute(text("update ent_policy_versions set is_current = false, superseded_at = now() where doc_id = :d and is_current"), {"d": doc_id})
    await db.execute(text("update ent_policy_chunks set is_active = true where version_id = :v"), {"v": version_id})
    await db.execute(text("update ent_policy_versions set is_current = true, sync_status = 'ready', sync_error = null, chunk_count = :n, embedding_model = :m, embedding_dim = :dim where id = :v"),
                     {"n": len(chunks), "m": model, "dim": dim, "v": version_id})
    await db.execute(text("update ent_policy_docs set current_version_id = :v, doc_type = :dt, status = 'active', stale = false, stale_reason = null, last_seen_at = now(), last_verified_at = now() where id = :d"),
                     {"v": version_id, "dt": doc_type, "d": doc_id})
    await db.commit()
    result.embeddings_generated += len(to_embed)
    result.embeddings_reused += len(chunks) - len(to_embed)
    return version_id


async def _mark_stale(db, org_id, scan_id, domain, doc_id, reason: str, result: SyncResult, action: str = "marked_stale") -> None:
    await db.execute(text("update ent_policy_docs set stale = true, stale_reason = :r where id = :d"), {"r": reason[:400], "d": doc_id})
    await _log(db, org_id, scan_id, domain, action, doc_id=doc_id, reason=reason[:400])
    result.counts[action if action in result.counts else "marked_stale"] += 1


async def sync_scan(scan_id: uuid.UUID, org_id: uuid.UUID, *, embedder: Embedder | None = None) -> SyncResult:
    """Bring the website's policy knowledge base up to date from this scan. Safe to run again: a second run over the same scan finds everything unchanged."""
    embedder = embedder or default_embedder
    set_org_scope(org_id)
    result = SyncResult()
    async with async_session_factory() as db:
        scan = (await db.execute(text("select domain, status, stop_reason from ent_scans where id = :s and org_id = :o"), {"s": scan_id, "o": org_id})).mappings().first()
        if scan is None:
            raise LookupError(f"Enterprise scan {scan_id} not found for this organisation")
        domain = result.domain = scan["domain"]
        rows = (await db.execute(text(
            "select id, original_url, final_url, status, http_status, page_category, page_category_strength, category_reason, page_title, evidence_note, last_error, status_reason, also_categories "
            "from ent_frontier where scan_id = :s"), {"s": scan_id})).mappings().all()
        done = [r for r in rows if r["status"] == "done"]
        active = (await db.execute(text(
            "select d.id, d.identity_key, d.doc_type, d.status, d.url_history, v.content_hash, v.id as version_id from ent_policy_docs d left join ent_policy_versions v on v.id = d.current_version_id "
            "where d.org_id = :o and d.domain = :dom and d.status = 'active'"), {"o": org_id, "dom": domain})).mappings().all()
        known = {r["identity_key"]: r for r in active}

        if not done:                                                  # the website could not be read: nothing may be overwritten
            for r in active:
                await _mark_stale(db, org_id, scan_id, domain, r["id"], "the website could not be read in the latest scan, so this document was not re-verified", result)
            await db.commit()
            return result

        seen: set[str] = set()
        # One unit of work per policy: a page's own category, and each policy it holds as a section (a cookie policy inside the privacy page) under the page's address plus "#kind".
        items: list[tuple] = []
        for r in done:
            base = _identity(r["final_url"] or r["original_url"])
            if r["page_category"] in KB_TYPES:
                items.append((r, base, KB_TYPES[r["page_category"]], None))
            for k in (r["also_categories"] or []):
                if k in KB_TYPES:
                    items.append((r, f"{base}#{k}", KB_TYPES[k], k))
        item_ids = {i[1] for i in items}
        for r, identity, doc_type, embedded_kind in items:
            if identity in seen:
                continue
            seen.add(identity)
            try:
                ev = (await db.execute(text(
                    "select id, kind, page_number, content from ent_evidence where scan_id = :s and frontier_id = :f and kind in ('page_text', 'page_html') order by kind, page_number"),
                    {"s": scan_id, "f": r["id"]})).mappings().all()
                html = "".join(e["content"] for e in ev if e["kind"] == "page_html")
                page_text = "\n".join(e["content"] for e in ev if e["kind"] == "page_text")
                clean = sec.clean_page(html, page_text)
                if embedded_kind is not None:
                    from app.enterprise.analysis import embedded as emb
                    mine = next((e for e in emb.detect(html, page_text, r["page_category"], doc=clean) if e.kind == embedded_kind), None)
                    if mine is None:
                        raise ValueError(f"the {embedded_kind.replace('_', ' ')} section is no longer found in the page")
                    clean = sec.CleanDoc(sec.normalise_text(mine.text), [sec.Section(mine.heading, 2, [ln for ln in mine.text.splitlines() if ln.strip()])], "html", f"a section of {r['final_url'] or r['original_url']}")
                if not clean.text.strip():
                    raise ValueError(clean.note or "no text could be extracted")
                content_hash = sec.sha256(clean.text)
                note = "; ".join(x for x in (clean.note, r["evidence_note"]) if x) or None
                status = "partial" if (clean.method == "text" and clean.note and "rendered text was used" in clean.note) or (r["evidence_note"] and "not opened" in r["evidence_note"]) else "complete"
                strength = r["page_category_strength"]
                method = "rules+model" if "model read it" in (r["category_reason"] or "") else "rules"
                existing = known.get(identity)
                if existing is None:
                    # a known document at a new address: the same content, from a document no longer seen at its old address
                    moved = next((k for k, d in known.items() if k not in item_ids and d["content_hash"] == content_hash), None)
                    if moved is not None:
                        d = known.pop(moved)
                        history = list(d["url_history"] or []) + [{"from": moved, "to": identity, "scan_id": str(scan_id)}]
                        await db.execute(text("update ent_policy_docs set identity_key = :k, url_history = cast(:h as jsonb), last_seen_at = now(), last_verified_at = now(), stale = false, stale_reason = null where id = :d"),
                                         {"k": identity, "h": json.dumps(history), "d": d["id"]})
                        await _log(db, org_id, scan_id, domain, "url_changed", doc_id=d["id"], old=moved, new=identity)
                        await db.commit()
                        result.counts["url_changed"] += 1
                        result.documents.append({"url": identity, "type": doc_type, "action": "url_changed"})
                        continue
                    doc_id = await db.scalar(text("insert into ent_policy_docs (org_id, domain, identity_key, doc_type, last_verified_at) values (:o, :dom, :k, :t, null) "
                                                  "on conflict (org_id, domain, identity_key) do update set doc_type = excluded.doc_type, status = 'active' returning id"),
                                             {"o": org_id, "dom": domain, "k": identity, "t": doc_type})
                    version_id = await _create_version(db, org_id=org_id, scan_id=scan_id, domain=domain, doc_id=doc_id, doc_type=doc_type, original_url=r["original_url"], final_url=r["final_url"] or r["original_url"],
                                                       title=r["page_title"], clean=clean, content_hash=content_hash, method=method, confidence=strength, evidence_ids=[e["id"] for e in ev],
                                                       extraction_status=status, note=note, embedder=embedder, result=result)
                    await _log(db, org_id, scan_id, domain, "new", doc_id=doc_id, version_id=version_id, url=identity, doc_type=doc_type, chunks=len(sec.chunk_document(clean)))
                    await db.commit()
                    result.counts["new"] += 1
                    result.changed_types.add(doc_type)
                    result.documents.append({"url": identity, "type": doc_type, "action": "new"})
                    continue
                known.pop(identity)
                if existing["content_hash"] == content_hash:
                    await db.execute(text("update ent_policy_docs set doc_type = :t, last_seen_at = now(), last_verified_at = now(), stale = false, stale_reason = null where id = :d"), {"t": doc_type, "d": existing["id"]})
                    await _log(db, org_id, scan_id, domain, "unchanged", doc_id=existing["id"], version_id=existing["version_id"], url=identity)
                    await db.commit()
                    result.counts["unchanged"] += 1
                    result.documents.append({"url": identity, "type": doc_type, "action": "unchanged"})
                    continue
                version_id = await _create_version(db, org_id=org_id, scan_id=scan_id, domain=domain, doc_id=existing["id"], doc_type=doc_type, original_url=r["original_url"],
                                                   final_url=r["final_url"] or r["original_url"], title=r["page_title"], clean=clean, content_hash=content_hash, method=method, confidence=strength,
                                                   evidence_ids=[e["id"] for e in ev], extraction_status=status, note=note, embedder=embedder, result=result)
                await _log(db, org_id, scan_id, domain, "changed", doc_id=existing["id"], version_id=version_id, url=identity, doc_type=doc_type, previous_hash=existing["content_hash"], new_hash=content_hash)
                await db.commit()
                result.counts["changed"] += 1
                result.changed_types.add(doc_type)
                result.documents.append({"url": identity, "type": doc_type, "action": "changed"})
            except Exception as exc:                                  # one document's failure never stops the others, and never touches its last verified version
                await db.rollback()
                existing = known.pop(identity, None) or next((d for d in active if d["identity_key"] == identity), None)
                msg = f"{type(exc).__name__}: {str(exc)[:200]}"
                result.errors.append(f"{identity}: {msg}")
                is_embed = isinstance(exc, EmbeddingFailed)
                from app.monitoring import events as error_events
                await error_events.record_error(agent="enterprise", module="enterprise.policykb.sync", exc=exc, service="embedding_api" if is_embed else "policy_kb", scan_id=scan_id, website_url=identity)
                if existing is not None:
                    await _mark_stale(db, org_id, scan_id, domain, existing["id"], f"the latest version could not be {'embedded' if is_embed else 'extracted'}: {msg}; the last verified version is kept",
                                      result, action="sync_failed" if is_embed else "extraction_failed")
                else:
                    await _log(db, org_id, scan_id, domain, "sync_failed" if is_embed else "extraction_failed", url=identity, error=msg)
                    result.counts["sync_failed" if is_embed else "extraction_failed"] += 1
                await db.commit()

        # documents the scan did not produce a page for: say why, and never delete
        by_url = {_identity(x["original_url"]): x for x in rows}
        by_url.update({_identity(x["final_url"]): x for x in rows if x["final_url"]})
        incomplete = scan["stop_reason"] in LIMIT_STOPS
        for identity, d in list(known.items()):
            fr = by_url.get(identity.split("#")[0])
            if fr is not None and fr["status"] == "failed" and fr["http_status"] in (404, 410):
                await db.execute(text("update ent_policy_docs set status = 'removed', last_seen_at = last_seen_at where id = :d"), {"d": d["id"]})
                await db.execute(text("update ent_policy_chunks set is_active = false where doc_id = :d"), {"d": d["id"]})
                await db.execute(text("update ent_policy_versions set is_current = false, superseded_at = now() where doc_id = :d and is_current"), {"d": d["id"]})
                await _log(db, org_id, scan_id, domain, "removed", doc_id=d["id"], url=identity, http_status=fr["http_status"])
                result.counts["removed"] += 1
                result.changed_types.add(d["doc_type"])
                result.documents.append({"url": identity, "type": d["doc_type"], "action": "removed"})
            elif fr is not None and fr["status"] in ("failed", "pending", "in_progress", "excluded"):
                why = fr["last_error"] or fr["status_reason"] or fr["status"]
                await _mark_stale(db, org_id, scan_id, domain, d["id"], f"the page could not be read in the latest scan ({str(why)[:150]}); the last verified version is kept", result, action="extraction_failed")
            elif fr is not None and fr["status"] == "done":
                await _mark_stale(db, org_id, scan_id, domain, d["id"], f"the page was read but is no longer classified as {d['doc_type'].replace('_', ' ')}; the last verified version is kept", result)
            else:
                await _mark_stale(db, org_id, scan_id, domain, d["id"], "the page was not found in the latest scan" + (" (the scan stopped at a limit)" if incomplete else "") + "; the last verified version is kept", result)
        await db.commit()
    if not result.errors:
        from app.monitoring import events as error_events
        await error_events.record_success("embedding_api", agent="enterprise")
        await error_events.record_success("policy_kb", agent="enterprise")
    return result
