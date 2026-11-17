"""History, comparison and consistency checks for the policy knowledge base.

Versions are never deleted: a newer version supersedes an older one (it stops being current and its chunks stop being searched) but both stay readable. `diff_versions` shows what changed between
two versions of one document, section by section. `verify_consistency` finds a database that disagrees with itself (a current version whose chunk count is wrong, a searchable chunk with no
embedding, a searchable chunk of a version that is not current); `repair_embeddings` re-embeds the chunks that lack one.
"""

from __future__ import annotations

import difflib
import re
import uuid

from sqlalchemy import text

from app.db.session import async_session_factory, set_org_scope
from app.enterprise.policykb import sync as kb

#: Which DPDP parameters read which kind of document, so a change to a document says what must be looked at again. Mirrors the parameter catalogue's groups, not its legal text.
PARAMETERS_BY_DOC_TYPE: dict[str, list[str]] = {
    "privacy_policy": ["N1", "N2", "N3", "N4", "N5", "N6", "N7", "N8", "C1", "C2", "C3", "C8", "G1", "G2", "G3", "G4", "G6", "K1", "X1", "X2", "X3"],
    "cookie_policy": ["C4", "C5", "C6", "C7", "C9", "X1", "X2"],
    "terms": ["N1", "N2"],
    "grievance": ["G1", "G2", "G3", "G4", "G5"],
    "contact": ["G1", "G2", "G3"],
}

_RETENTION = re.compile(r"retain|retention|delete|erase|erasure|how long|storage period|days|months|years", re.I)
_CONSENT = re.compile(r"consent|agree|opt[- ]?in|opt[- ]?out|withdraw|accept|reject", re.I)


def affected_parameters(doc_types) -> list[str]:
    return sorted({p for t in doc_types for p in PARAMETERS_BY_DOC_TYPE.get(t, [])})


async def list_versions(org_id: uuid.UUID, doc_id: uuid.UUID) -> list[dict]:
    set_org_scope(org_id)
    async with async_session_factory() as db:
        rows = (await db.execute(text(
            "select id, version_no, scan_id, original_url, final_url, title, doc_type, classification_method, classification_confidence, content_hash, text_chars, extracted_at, extraction_status, extraction_note,"
            " sync_status, sync_error, embedding_model, embedding_dim, chunk_count, is_current, superseded_at from ent_policy_versions where org_id = :o and doc_id = :d order by version_no desc"),
            {"o": org_id, "d": doc_id})).mappings().all()
    return [dict(r) for r in rows]


def _lines(t: str) -> list[str]:
    return [ln for ln in (t or "").split("\n") if ln.strip()]


async def diff_versions(org_id: uuid.UUID, doc_id: uuid.UUID, old_no: int, new_no: int) -> dict:
    """What was added, removed and changed between two versions, with the headings the lines sit under and a flag on lines about retention and consent."""
    set_org_scope(org_id)
    async with async_session_factory() as db:
        rows = (await db.execute(text("select version_no, text, content_hash, extracted_at, final_url from ent_policy_versions where org_id = :o and doc_id = :d and version_no in (:a, :b)"),
                                 {"o": org_id, "d": doc_id, "a": old_no, "b": new_no})).mappings().all()
    by = {r["version_no"]: r for r in rows}
    if old_no not in by or new_no not in by:
        raise LookupError("one of the two versions does not exist for this document")
    a, b = _lines(by[old_no]["text"]), _lines(by[new_no]["text"])
    added, removed, changed = [], [], []
    sm = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "insert":
            added += b[j1:j2]
        elif tag == "delete":
            removed += a[i1:i2]
        elif tag == "replace":
            for old, new in zip(a[i1:i2], b[j1:j2]):
                changed.append({"before": old, "after": new})
            added += b[j1 + (i2 - i1):j2] if (j2 - j1) > (i2 - i1) else []
            removed += a[i1 + (j2 - j1):i2] if (i2 - i1) > (j2 - j1) else []

    def flag(line: str) -> list[str]:
        return [n for n, rx in (("retention", _RETENTION), ("consent", _CONSENT)) if rx.search(line)]

    return {
        "from_version": old_no, "to_version": new_no, "identical": by[old_no]["content_hash"] == by[new_no]["content_hash"],
        "added": [{"text": t, "topics": flag(t)} for t in added], "removed": [{"text": t, "topics": flag(t)} for t in removed],
        "changed": [{**c, "topics": sorted(set(flag(c["before"]) + flag(c["after"])))} for c in changed],
        "counts": {"added": len(added), "removed": len(removed), "changed": len(changed)},
    }


async def verify_consistency(org_id: uuid.UUID, domain: str | None = None) -> list[dict]:
    """Every way the knowledge base disagrees with itself. An empty list means consistent. Each issue names the document and what is wrong."""
    set_org_scope(org_id)
    dom = "and d.domain = :dom" if domain else ""
    params = {"o": org_id, **({"dom": domain} if domain else {})}
    issues: list[dict] = []
    async with async_session_factory() as db:
        for kind, sql in (
            ("chunk_count_mismatch", f"select d.id doc, v.id ver, v.chunk_count expected, (select count(*) from ent_policy_chunks c where c.version_id = v.id) actual from ent_policy_docs d join ent_policy_versions v on v.id = d.current_version_id where d.org_id = :o {dom} and d.status = 'active' and v.chunk_count <> (select count(*) from ent_policy_chunks c where c.version_id = v.id)"),
            ("missing_embedding", f"select d.id doc, c.version_id ver, count(*) actual, 0 expected from ent_policy_chunks c join ent_policy_docs d on d.id = c.doc_id where c.org_id = :o {dom} and c.is_active and c.embedding is null group by d.id, c.version_id"),
            ("active_chunk_of_non_current_version", f"select d.id doc, c.version_id ver, count(*) actual, 0 expected from ent_policy_chunks c join ent_policy_docs d on d.id = c.doc_id join ent_policy_versions v on v.id = c.version_id where c.org_id = :o {dom} and c.is_active and not v.is_current group by d.id, c.version_id"),
            ("current_version_without_active_chunks", f"select d.id doc, v.id ver, v.chunk_count expected, (select count(*) from ent_policy_chunks c where c.version_id = v.id and c.is_active) actual from ent_policy_docs d join ent_policy_versions v on v.id = d.current_version_id where d.org_id = :o {dom} and d.status = 'active' and v.chunk_count > 0 and (select count(*) from ent_policy_chunks c where c.version_id = v.id and c.is_active) = 0"),
            ("pointer_mismatch", f"select d.id doc, d.current_version_id ver, 1 expected, (select count(*) from ent_policy_versions v where v.doc_id = d.id and v.is_current and v.id = d.current_version_id) actual from ent_policy_docs d where d.org_id = :o {dom} and d.status = 'active' and d.current_version_id is not null and not exists (select 1 from ent_policy_versions v where v.id = d.current_version_id and v.is_current)"),
        ):
            for r in (await db.execute(text(sql), params)).mappings().all():
                issues.append({"issue": kind, "doc_id": str(r["doc"]), "version_id": str(r["ver"]) if r["ver"] else None, "expected": r["expected"], "actual": r["actual"]})
    return issues


async def repair_embeddings(org_id: uuid.UUID, domain: str | None = None, *, embedder: kb.Embedder | None = None) -> dict:
    """Embed the searchable chunks that have no embedding (a provider outage after the chunks were stored, or a manual import). Returns how many were fixed and how many still are not."""
    set_org_scope(org_id)
    embedder = embedder or kb.default_embedder
    dom = "and domain = :dom" if domain else ""
    params = {"o": org_id, **({"dom": domain} if domain else {})}
    async with async_session_factory() as db:
        rows = (await db.execute(text(f"select id, content from ent_policy_chunks where org_id = :o {dom} and is_active and embedding is null order by created_at limit 500"), params)).mappings().all()
        if not rows:
            return {"fixed": 0, "remaining": 0}
        try:
            vectors = await kb.embed_all([r["content"] for r in rows], embedder)
        except kb.EmbeddingFailed as exc:
            return {"fixed": 0, "remaining": len(rows), "error": str(exc)}
        for r, v in zip(rows, vectors):
            await db.execute(text("update ent_policy_chunks set embedding = cast(:e as vector), embedding_source = 'generated' where id = :i and org_id = :o"), {"e": kb._vec(v), "i": r["id"], "o": org_id})
        await db.commit()
    return {"fixed": len(rows), "remaining": 0}
