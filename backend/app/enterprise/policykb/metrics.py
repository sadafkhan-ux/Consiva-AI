"""The policy knowledge base's dashboard numbers. Every figure is a query over stored rows; nothing is estimated and nothing is invented.

`llm_calls_saved` is the number of analysed passages whose facts were copied from an earlier scan of the same text (a model call that did not happen). `tokens` come from the passages' own
input size (characters, and characters / 4 as a labelled estimate): the model client's exact token counts are not stored per Enterprise passage, so none is shown as exact.
"""

from __future__ import annotations

import uuid

from sqlalchemy import text

from app.db.session import async_session_factory, set_org_scope


async def policy_kb_metrics(org_id: uuid.UUID, domain: str | None = None) -> dict:
    set_org_scope(org_id)
    dom_d = "and d.domain = :dom" if domain else ""
    dom_l = "and domain = :dom" if domain else ""
    p = {"o": org_id, **({"dom": domain} if domain else {})}
    async with async_session_factory() as db:
        one = lambda sql: db.scalar(text(sql), p)                                        # noqa: E731
        docs = (await db.execute(text(f"select count(*) filter (where d.status = 'active') active, count(*) filter (where d.status = 'removed') removed, count(*) filter (where d.stale and d.status = 'active') stale, count(distinct d.domain) sites from ent_policy_docs d where d.org_id = :o {dom_d}"), p)).mappings().one()
        vers = (await db.execute(text(f"select count(*) total, count(*) filter (where v.sync_status = 'pending') pending, count(*) filter (where v.sync_status = 'failed') failed, count(*) filter (where v.extraction_status = 'failed') extraction_failed, count(*) filter (where v.extraction_status = 'partial') partial from ent_policy_versions v join ent_policy_docs d on d.id = v.doc_id where v.org_id = :o {dom_d}"), p)).mappings().one()
        chunks = (await db.execute(text(f"select count(*) total, count(*) filter (where is_active) active, count(*) filter (where embedding_source = 'generated') generated, count(*) filter (where embedding_source = 'reused') reused, count(*) filter (where is_active and embedding is null) missing from ent_policy_chunks where org_id = :o {dom_l}"), p)).mappings().one()
        log = {r["action"]: r["n"] for r in (await db.execute(text(f"select action, count(*) n from ent_policy_sync_log where org_id = :o {dom_l} group by action"), p)).mappings().all()}
        last = await one(f"select max(created_at) from ent_policy_sync_log where org_id = :o {dom_l} and action in ('new', 'unchanged', 'changed', 'url_changed', 'removed')")
        retr = (await db.execute(text(f"select count(*) n, count(*) filter (where mode <> 'hybrid') degraded from ent_policy_retrievals where org_id = :o {dom_l}"), p)).mappings().one()
        reuse = (await db.execute(text(
            "select count(*) filter (where c.result->>'reused' = 'true') reused, count(*) filter (where coalesce(c.result->>'reused', 'false') <> 'true') called, "
            "coalesce(sum(c.input_chars) filter (where c.result->>'reused' = 'true'), 0) chars_saved, coalesce(sum(c.input_chars) filter (where coalesce(c.result->>'reused', 'false') <> 'true'), 0) chars_sent "
            f"from ent_analysis_chunks c join ent_scans s on s.id = c.scan_id where s.org_id = :o {dom_l.replace('domain', 's.domain')} and c.level = 'page' and c.status = 'done'"), p)).mappings().one()
    return {
        "websites_monitored": docs["sites"],
        "documents": {"active": docs["active"], "removed": docs["removed"], "stale": docs["stale"], "versions": vers["total"]},
        "sync": {"new": log.get("new", 0), "changed": log.get("changed", 0), "unchanged": log.get("unchanged", 0), "removed": log.get("removed", 0), "url_changed": log.get("url_changed", 0),
                 "extraction_failed": log.get("extraction_failed", 0) + vers["extraction_failed"], "sync_failed": log.get("sync_failed", 0), "marked_stale": log.get("marked_stale", 0),
                 "awaiting_processing": vers["pending"], "failed_versions": vers["failed"], "partial_extractions": vers["partial"], "last_successful_sync": last},
        "embeddings": {"chunks": chunks["total"], "active_chunks": chunks["active"], "generated": chunks["generated"], "reused": chunks["reused"], "missing": chunks["missing"]},
        "retrieval": {"queries": retr["n"], "keyword_only_fallbacks": retr["degraded"]},
        "llm": {"passages_sent_to_model": reuse["called"], "calls_saved": reuse["reused"], "characters_sent": int(reuse["chars_sent"]), "characters_not_sent": int(reuse["chars_saved"]),
                "tokens_sent_estimate": int(reuse["chars_sent"]) // 4, "tokens_saved_estimate": int(reuse["chars_saved"]) // 4, "token_figures_are": "estimates (characters / 4); exact counts are not stored per passage"},
    }
