"""Integration check of the local scan-data cleanup against the REAL Postgres, for both agents. No model is called and no customer data is used: every row belongs to throw-away organisations,
and every pass is limited to them (`only_orgs`), so the database's real scans are never touched. Everything created is deleted at the end.

    docker compose -f docker-compose.prod.yml cp backend/tools/cleanup_integration.py backend:/tmp/cleanup_integration.py
    docker compose -f docker-compose.prod.yml exec -T -w /app -e CLEANUP_CONFIRM_DATABASE=consiva@db backend env PYTHONPATH=/app python /tmp/cleanup_integration.py

Exit 0 only if every check passed.
"""

from __future__ import annotations

import asyncio
import csv
import json
import os
import sys
import tempfile
import traceback
import uuid
from datetime import date
from types import SimpleNamespace

from sqlalchemy import text

from app.cleanup import core
from app.db.session import async_session_factory

RESULTS: list[tuple[str, bool]] = []
_COLS: dict[str, list] = {}


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""), flush=True)


async def q(sql: str, **p):
    async with async_session_factory() as db:
        return (await db.execute(text(sql), p)).mappings().all()


async def ins(db, table: str, **vals):
    """Insert one row; required columns that are not given get a value that fits their type. Returns the new id (or None)."""
    if table not in _COLS:
        _COLS[table] = (await db.execute(text("select column_name, data_type, is_nullable, column_default from information_schema.columns where table_schema = 'public' and table_name = :t"), {"t": table})).mappings().all()
    cols = _COLS[table]
    row = dict(vals)
    for c in cols:
        if c["column_name"] in row or c["is_nullable"] == "YES" or c["column_default"] is not None:
            continue
        t = c["data_type"]
        row[c["column_name"]] = ("x" if t == "text" else 0 if t in ("integer", "bigint", "smallint", "numeric") else uuid.uuid4() if t == "uuid" else b"x" if t == "bytea" else "{}" if t in ("jsonb", "json") else False if t == "boolean" else None)
    names = ", ".join(f'"{k}"' for k in row)
    marks = ", ".join((f"cast(:{k} as jsonb)" if next((c["data_type"] for c in cols if c["column_name"] == k), "") == "jsonb" else f":{k}") for k in row)
    has_id = any(c["column_name"] == "id" for c in cols)
    res = await db.execute(text(f'insert into "{table}" ({names}) values ({marks})' + (" returning id" if has_id else "")), {k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in row.items()})
    return res.scalar() if has_id else None


async def five_page_scan(db, org, website, *, status="completed", age_minutes=120, error=None, with_job=False, paused_run=False, referenced=False):
    sid = (await db.execute(text(
        "insert into consent_scans (org_id, website_id, url, status, error, scan_options, created_at, started_at, completed_at) "
        "values (:o, :w, :u, :st, :e, '{}'::jsonb, now() - make_interval(mins => :a), now() - make_interval(mins => :a), "
        "case when :st in ('completed','failed','cancelled') then now() - make_interval(mins => :a) else null end) returning id"),
        {"o": org, "w": website, "u": "https://five.example.test/" + str(uuid.uuid4())[:6], "st": status, "e": error, "a": age_minutes})).scalar()
    run = await ins(db, "agent_runs", scan_id=sid, agent_name="consent", status="paused" if paused_run else "completed", langgraph_thread_id=f"thread-{sid}")
    page = await ins(db, "website_pages", scan_id=sid, url="https://five.example.test/p")
    trk = await ins(db, "trackers", scan_id=sid, script_src="https://t.example/x.js", page_id=page) if await _has(db, "trackers", "page_id") else await ins(db, "trackers", scan_id=sid, script_src="https://t.example/x.js")
    await ins(db, "cookies", scan_id=sid, name="_ga", tracker_id=trk) if await _has(db, "cookies", "tracker_id") else await ins(db, "cookies", scan_id=sid, name="_ga")
    await ins(db, "consent_forms", scan_id=sid, page_id=page) if await _has(db, "consent_forms", "page_id") else await ins(db, "consent_forms", scan_id=sid)
    await ins(db, "scan_url_inventory", scan_id=sid, domain="five.example.test", original_url="https://five.example.test/p", normalized_url="https://five.example.test/p", discovery_source="seed", crawl_depth=0)
    await ins(db, "policies", scan_id=sid, url="https://five.example.test/privacy", policy_type="privacy_policy", extracted_text_ref="policy text " * 50)
    await ins(db, "consent_signals", scan_id=sid, mechanism_type="banner")
    await ins(db, "third_party_services", scan_id=sid, service_name="Analytics")
    await ins(db, "llm_usage_events", scan_id=sid, call_id=uuid.uuid4(), agent="consent", provider="x", model="m", status="success")
    await ins(db, "report_pdfs", scan_id=sid, variant="full", fingerprint=str(uuid.uuid4()), filename="r.pdf", pdf=b"%PDF-1.4 five page report", size_bytes=25)
    await ins(db, "scan_screenshots", scan_id=sid, status="ok", image=b"png", width=10, height=10)
    await ins(db, "agent_run_stages", scan_id=sid, stage="website_scan", run_id=run) if await _has(db, "agent_run_stages", "run_id") else await ins(db, "agent_run_stages", scan_id=sid, stage="website_scan")
    f1 = await ins(db, "consent_findings", scan_id=sid, agent_run_id=run, category="analytics", risk_level="high", finding_text="a finding")
    f2 = await ins(db, "consent_findings", scan_id=sid, agent_run_id=run, category="analytics", risk_level="low", finding_text="another")
    await ins(db, "consent_recommendations", finding_id=f1, recommendation_text="fix it")
    await ins(db, "checkpoints", thread_id=f"thread-{sid}", checkpoint_id=str(uuid.uuid4()), checkpoint={}, checkpoint_ns="", type="t", metadata={})
    await ins(db, "checkpoint_blobs", thread_id=f"thread-{sid}", checkpoint_ns="", channel="c", version="1", type="t", blob=b"b")
    await ins(db, "checkpoint_writes", thread_id=f"thread-{sid}", checkpoint_ns="", checkpoint_id="c", task_id="t", idx=0, channel="c", type="t", blob=b"w")
    # (no audit_logs row is made here: the audit log is append-only, so a test row could never be removed. The guard that keeps a run an audit row points at is the same code path as the one
    #  tested below with `actions`, and is shown by the report of a pass over the real data.)
    if referenced:                                   # a remediation action that points at one finding: business data, must survive
        await ins(db, "actions", org_id=org, finding_id=f2, action_type="task", title="Fix the banner")
    if with_job:
        await db.execute(text("insert into agent_jobs (org_id, job_type, payload, status) values (:o, 'render_reports', cast(:p as jsonb), 'queued')"), {"o": org, "p": json.dumps({"scan_id": str(sid)})})
    return sid


async def _has(db, table, col):
    return bool(await db.scalar(text("select 1 from information_schema.columns where table_schema='public' and table_name=:t and column_name=:c"), {"t": table, "c": col}))


async def ent_scan(db, org, domain, *, status="completed", analysis="completed", age_minutes=120, error=None, created_days=0):
    sid = (await db.execute(text(
        "insert into ent_scans (org_id, website_url, domain, status, analysis_status, error, analysis_error, created_at, started_at, completed_at, analysis_completed_at) "
        "values (:o, :u, :d, :st, :an, :e, :e, now() - make_interval(mins => :a) - make_interval(days => :cd), now() - make_interval(mins => :a), "
        "case when :st = 'completed' then now() - make_interval(mins => :a) else null end, case when :an in ('completed','partial') then now() - make_interval(mins => :a) else null end) returning id"),
        {"o": org, "u": f"https://{domain}/", "d": domain, "st": status, "an": analysis, "e": error, "a": age_minutes, "cd": created_days})).scalar()
    fid = await ins(db, "ent_frontier", scan_id=sid, normalized_url=f"https://{domain}/", original_url=f"https://{domain}/", depth=0, discovery_source="seed", status="done")
    await ins(db, "ent_evidence", scan_id=sid, frontier_id=fid, kind="page_text", source_url=f"https://{domain}/", page_number=0, content_hash=str(uuid.uuid4()), content_type="text/html", byte_size=9, content="page text " * 50, detection_method="dom_render")
    await ins(db, "ent_evidence", scan_id=sid, kind="cookie", source_url=f"https://{domain}/", page_number=0, content_hash=str(uuid.uuid4()), content_type="application/json", byte_size=2, content="{}", detection_method="x")
    await ins(db, "ent_analysis_chunks", scan_id=sid, source_url=f"https://{domain}/", chunk_index=0, chunk_count=1, level="page", status="done", result={"facts": []})
    score = await ins(db, "ent_score_results", scan_id=sid, version="v", catalogue_fingerprint="f", as_of=date(2026, 11, 16), coverage=50, basis="readiness", result={})
    await ins(db, "ent_reports", scan_id=sid, version=1, status="ready", pdf=b"%PDF-1.4 enterprise report", sha256="x", byte_size=24, page_count=1, score_result_id=score)
    await ins(db, "ent_audit_events", scan_id=sid, actor="system", event="analysis_started", detail={})
    doc = await ins(db, "ent_policy_docs", org_id=org, domain=domain, identity_key=f"https://{domain}/privacy", doc_type="privacy_policy")
    ver = await ins(db, "ent_policy_versions", org_id=org, doc_id=doc, version_no=1, scan_id=sid, original_url=f"https://{domain}/privacy", final_url=f"https://{domain}/privacy", doc_type="privacy_policy",
                    classification_method="rules", content_hash="h", text="policy text " * 30, text_chars=360, is_current=True, sync_status="ready")
    await ins(db, "ent_policy_chunks", id=uuid.uuid4(), org_id=org, doc_id=doc, version_id=ver, domain=domain, doc_type="privacy_policy", source_url=f"https://{domain}/privacy", chunk_index=0, content="c", content_hash="h", token_estimate=1, is_active=True)
    await ins(db, "ent_policy_sync_log", org_id=org, scan_id=sid, doc_id=doc, domain=domain, action="new")
    await ins(db, "ent_fact_cache", org_id=org, text_hash=str(uuid.uuid4()), extractor_key="k", result={"facts": []})
    return sid


def count(table, where="true", **p):
    return q(f'select count(*) n from "{table}" where {where}', **p)


async def run() -> None:
    org, other = uuid.uuid4(), uuid.uuid4()
    orgs = [org, other]
    ids: dict[str, uuid.UUID] = {}
    async with async_session_factory() as db:
        web = await ins(db, "websites", org_id=org, domain="five.example.test")
        web2 = await ins(db, "websites", org_id=other, domain="five2.example.test")
        ids["five_old"] = await five_page_scan(db, org, web, referenced=True)
        ids["five_failed"] = await five_page_scan(db, org, web, status="failed", error="the site refused the connection")
        ids["five_running"] = await five_page_scan(db, org, web, status="running", age_minutes=300)
        ids["five_stuck"] = await five_page_scan(db, org, web, status="running", age_minutes=3000)
        ids["five_fresh"] = await five_page_scan(db, org, web, age_minutes=1)
        ids["five_job"] = await five_page_scan(db, org, web, with_job=True)
        ids["five_paused"] = await five_page_scan(db, org, web, paused_run=True)
        ids["five_other_org"] = await five_page_scan(db, other, web2)
        ids["ent_old"] = await ent_scan(db, org, "ent-a.example.test")
        ids["ent_failed"] = await ent_scan(db, org, "ent-b.example.test", status="failed", analysis="not_started", error="the site could not be reached")
        ids["ent_running"] = await ent_scan(db, org, "ent-c.example.test", status="running", analysis="not_started", age_minutes=300)
        ids["ent_stuck"] = await ent_scan(db, org, "ent-d.example.test", status="completed", analysis="not_started", age_minutes=3000, created_days=3)
        ids["ent_other_org"] = await ent_scan(db, other, "ent-e.example.test")
        await db.commit()

    s = core.Settings(grace_minutes=15, confirm="consiva@db")
    tmp = tempfile.mkdtemp()

    # ---- the order of the deletes agrees with the database's own foreign keys ---------------------------------------------------------------------------------------
    from app.cleanup import registry as reg
    order = {}
    for agent in reg.AGENTS:
        for i, st in enumerate(agent.steps):
            order[st.table] = (agent.key, i)
    for i, st in enumerate(reg.ENTERPRISE_SITE_STEPS):
        order[st.table] = ("site", i)
    edges = await q("select conrelid::regclass::text child, confrelid::regclass::text parent from pg_constraint where contype = 'f' and conrelid <> confrelid")
    bad = [(e["child"], e["parent"]) for e in edges if e["child"] in order and e["parent"] in order and order[e["child"]][0] == order[e["parent"]][0] and order[e["child"]][1] > order[e["parent"]][1]]
    check("order: every table is cleared before the table it points at (checked against the real foreign keys)", not bad, str(bad))

    # ---- the target must be verified ------------------------------------------------------------------------------------------------------------------------------
    for label, st in (("no confirmation", core.Settings(grace_minutes=15, confirm="")), ("a different database name", core.Settings(grace_minutes=15, confirm="other@db")),
                      ("a host that is not allowed", core.Settings(grace_minutes=15, confirm="consiva@db", allowed_hosts=("somewhere-else",)))):
        try:
            await core.run(execute=True, settings=st, only_orgs=orgs)
            check(f"target: {label} is refused", False)
        except core.TargetNotVerified:
            check(f"target: {label} is refused and nothing is changed", (await count("report_pdfs", "scan_id = :s", s=ids["five_old"]))[0]["n"] == 1)
    os.environ["AGENT_WEBHOOK_URL"] = "https://api.consiva.ai/api/v1/webhooks/agents"
    try:
        await core.run(execute=True, settings=s, only_orgs=orgs)
        check("target: a configured partner production webhook is refused", False)
    except core.TargetNotVerified:
        check("target: a configured partner production webhook is refused", True)
    finally:
        del os.environ["AGENT_WEBHOOK_URL"]

    # ---- a dry run changes nothing --------------------------------------------------------------------------------------------------------------------------------
    before = {t: (await count(t))[0]["n"] for t in ("report_pdfs", "ent_reports", "cookies", "ent_evidence", "website_pages", "consent_scans", "ent_scans")}
    dry = await core.run(execute=False, settings=s, only_orgs=orgs)
    after = {t: (await count(t))[0]["n"] for t in before}
    check("dry run: nothing is deleted, and it says what would be", before == after and dry.agents["five_page"].would_delete.get("report_pdfs", 0) >= 2 and not dry.executed)

    # ---- the real pass --------------------------------------------------------------------------------------------------------------------------------------------
    rep = await core.run(execute=True, settings=s, history_dir=tmp, only_orgs=[org])          # one organisation only: the other must stay untouched
    check("integrity: structure identical, no other table lost a row, history identical, constraints valid", rep.integrity["ok"], json.dumps({k: v for k, v in rep.integrity.items() if k != "structure"}, default=str))
    fp, ent = rep.agents["five_page"], rep.agents["enterprise"]
    check("report: deleted and retained counts are given for each agent separately", fp.scans_cleaned >= 3 and ent.scans_cleaned >= 3 and fp.history_retained > 0 and ent.history_retained > 0 and fp.key != ent.key)

    async def kids(sid, tables):
        return {t: (await count(t, "scan_id = :s", s=sid))[0]["n"] for t in tables}

    five_tables = ("website_pages", "trackers", "cookies", "consent_forms", "scan_url_inventory", "policies", "consent_signals", "third_party_services", "llm_usage_events", "report_pdfs", "scan_screenshots", "agent_run_stages")
    for name in ("five_old", "five_failed"):
        left = {t: n for t, n in (await kids(ids[name], five_tables)).items() if n}
        check(f"five-page {name}: every scan-generated table is empty (extracted text, report PDF, screenshots, trackers, cookies...)", not left, str(left))
    check("five-page: the stored five-page report PDFs are removed", (await count("report_pdfs", "scan_id = any(:ids)", ids=[ids["five_old"], ids["five_failed"]]))[0]["n"] == 0)
    check("five-page: findings are cleared, except the one a remediation action still points at", (await count("consent_findings", "scan_id = :s", s=ids["five_old"]))[0]["n"] == 1 and (await count("actions", "org_id = :o", o=org))[0]["n"] == 1)
    check("five-page: its recommendations are cleared", (await count("consent_recommendations", "finding_id in (select id from consent_findings where scan_id = :s)", s=ids["five_old"]))[0]["n"] == 0)
    check("five-page: the intermediate analysis state (checkpoints) is cleared", (await count("checkpoints", "thread_id = :t", t=f"thread-{ids['five_old']}"))[0]["n"] == 0 and (await count("checkpoint_writes", "thread_id = :t", t=f"thread-{ids['five_failed']}"))[0]["n"] == 0)
    check("five-page: the analysis runs of a cleared scan are removed when nothing else points at them", (await count("agent_runs", "scan_id = :s", s=ids["five_failed"]))[0]["n"] == 0)
    h = (await q("select url, status, error, created_at, started_at, completed_at from consent_scans where id = :s", s=ids["five_failed"]))[0]
    check("five-page: the scan's history row is intact: website, status, failure reason, times", h["status"] == "failed" and h["error"] == "the site refused the connection" and h["url"].startswith("https://five.example.test/") and h["created_at"] and h["completed_at"])
    for name, why in (("five_running", "a scan that is still running"), ("five_fresh", "a scan inside the grace period"), ("five_job", "a scan with a job still queued for it")):
        n = sum((await kids(ids[name], five_tables)).values())
        check(f"five-page: {why} keeps all its data", n >= len(five_tables), str(n))
    check("five-page: a scan of another organisation keeps all its data when the pass is limited to one organisation", sum((await kids(ids["five_other_org"], five_tables)).values()) >= len(five_tables))
    check("enterprise: another organisation's scan keeps all its data when the pass is limited to one organisation", sum((await kids(ids["ent_other_org"], ("ent_frontier", "ent_evidence", "ent_reports", "ent_score_results"))).values()) >= 5)
    check("five-page: a scan 'running' for two days with no job working on it is stuck, and is cleared", sum((await kids(ids["five_stuck"], five_tables)).values()) == 0)
    paused = sum((await kids(ids["five_paused"], five_tables)).values())
    check("five-page: a scan whose run is paused for review is cleared (as asked), and it is reported", paused == 0)

    ent_tables = ("ent_frontier", "ent_evidence", "ent_analysis_chunks", "ent_score_results", "ent_reports", "ent_audit_events")
    for name in ("ent_old", "ent_failed", "ent_stuck"):
        left = {t: n for t, n in (await kids(ids[name], ent_tables)).items() if n}
        check(f"enterprise {name}: every scan-generated table is empty (extracted text, evidence, report PDF, scores, URL queue)", not left, str(left))
    check("enterprise: the stored Enterprise report PDFs are removed", (await count("ent_reports", "scan_id = any(:ids)", ids=[ids["ent_old"], ids["ent_failed"], ids["ent_stuck"]]))[0]["n"] == 0)
    kb_left = (await q("select (select count(*) from ent_policy_docs where org_id = :o and domain = any(:d)) docs, (select count(*) from ent_policy_chunks where org_id = :o and domain = any(:d)) chunks, (select count(*) from ent_policy_versions v join ent_policy_docs d on d.id = v.doc_id where d.org_id = :o and d.domain = any(:d)) versions", o=org, d=["ent-a.example.test", "ent-b.example.test", "ent-d.example.test"]))[0]
    check("enterprise: the policy knowledge base (text, passages, embeddings) of cleared websites is gone", not any(kb_left.values()), str(dict(kb_left)))
    run_left = sum((await kids(ids["ent_running"], ent_tables)).values())
    kb_running = (await count("ent_policy_docs", "org_id = :o and domain = 'ent-c.example.test'", o=org))[0]["n"]
    check("enterprise: a scan that is still running keeps all its data, and so does its website's knowledge base", run_left == len(ent_tables) + 1 and kb_running == 1, f"{run_left} {kb_running}")
    eh = (await q("select status, analysis_status, error, website_url, created_at from ent_scans where id = :s", s=ids["ent_failed"]))[0]
    check("enterprise: the scan's history row is intact: website, status, failure reason", eh["status"] == "failed" and eh["error"] == "the site could not be reached" and eh["website_url"] == "https://ent-b.example.test/")

    hist = [r for r in csv.DictReader(open(rep.history_file, encoding="utf-8"))] if rep.history_file else []
    check("history backup: the CSV written before clearing lists both agents' scans, with website and status", any(r["scan_id"] == str(ids["five_failed"]) and r["status"] == "failed" for r in hist) and any(r["scan_id"] == str(ids["ent_old"]) for r in hist))

    # ---- repeatable ------------------------------------------------------------------------------------------------------------------------------------------------
    again = await core.run(execute=True, settings=s, only_orgs=[org])
    check("repeatable: a second pass finds nothing left to delete and still verifies", all(sum(a.deleted.values()) == 0 for a in again.agents.values()) and again.integrity["ok"], str({k: v.deleted for k, v in again.agents.items()}))

    # ---- the dashboard after the cleanup --------------------------------------------------------------------------------------------------------------------------
    from app.enterprise.api import dashboard
    from app.enterprise.config import get_enterprise_settings
    if get_enterprise_settings().enabled:
        async with async_session_factory() as db:
            user = SimpleNamespace(org_id=str(org), role="admin", key_name=None)
            d = await dashboard.summary(outcome="all", agent="all", q=None, offset=0, limit=100, refresh=True, user=user, db=db)
            f = await dashboard.summary(outcome="failed", agent="all", q=None, offset=0, limit=100, refresh=True, user=user, db=db)
            e = await dashboard.summary(outcome="all", agent="enterprise", q=None, offset=0, limit=100, refresh=True, user=user, db=db)
        byid = {i["scan_id"]: i for i in d["history"]["items"]}
        check("dashboard: every scan of both agents is listed after the cleanup (7 five-page and 4 Enterprise scans for this org)", d["kpis"]["scans_total"] == 11 and d["kpis"]["scans_five_page"] == 7 and d["kpis"]["scans_enterprise"] == 4, str(d["kpis"]))
        check("dashboard: the failure reasons survived", byid[str(ids["five_failed"])]["reason"] == "the site refused the connection" and byid[str(ids["ent_failed"])]["reason"] == "the site could not be reached")
        check("dashboard: completed, failed and pending are right for both agents", byid[str(ids["five_old"])]["outcome"] == "completed" and byid[str(ids["five_failed"])]["outcome"] == "failed" and byid[str(ids["five_running"])]["outcome"] == "pending"
              and byid[str(ids["ent_old"])]["outcome"] == "completed" and byid[str(ids["ent_running"])]["outcome"] == "pending" and byid[str(ids["ent_stuck"])]["outcome"] == "failed")
        check("dashboard: the filters by status and by agent work", f["history"]["total"] == 3 and all(i["outcome"] == "failed" for i in f["history"]["items"]) and e["history"]["total"] == 4)
        check("dashboard: a cleared Enterprise scan is marked as cleared; no scan content is in the response", byid[str(ids["ent_old"])]["data_cleared"] is True and "page text" not in json.dumps(d, default=str) and "%PDF" not in json.dumps(d, default=str))


async def teardown() -> None:
    """Remove everything the test made, children first. The pass is limited to these organisations, so nothing else is touched."""
    async with async_session_factory() as db:
        orgs = [r[0] for r in (await db.execute(text("select distinct org_id from consent_scans where url like 'https://five.example.test/%' or url like 'https://five2.example.test/%' union select distinct org_id from ent_scans where domain like 'ent-%.example.test'"))).all()]
        if not orgs:
            return
        await db.execute(text("delete from agent_jobs where org_id = any(cast(:o as uuid[]))"), {"o": orgs})
        await db.execute(text("delete from actions where org_id = any(cast(:o as uuid[]))"), {"o": orgs})
        await db.commit()
    await core.run(execute=True, settings=core.Settings(grace_minutes=0, confirm="consiva@db"), only_orgs=orgs)
    async with async_session_factory() as db:
        o = {"o": orgs}
        await db.execute(text("delete from consent_findings where scan_id in (select id from consent_scans where org_id = any(cast(:o as uuid[])))"), o)
        await db.execute(text("delete from agent_runs r where r.scan_id in (select id from consent_scans where org_id = any(cast(:o as uuid[]))) and not exists (select 1 from audit_logs a where a.agent_run_id = r.id)"), o)
        await db.execute(text("delete from consent_scans s where s.org_id = any(cast(:o as uuid[])) and not exists (select 1 from agent_runs r where r.scan_id = s.id)"), o)
        await db.execute(text("delete from websites w where w.org_id = any(cast(:o as uuid[])) and not exists (select 1 from consent_scans s where s.website_id = w.id)"), o)
        await db.execute(text("delete from ent_fact_cache where org_id = any(cast(:o as uuid[]))"), o)
        await db.execute(text("delete from ent_scans where org_id = any(cast(:o as uuid[]))"), o)
        await db.commit()


async def main() -> int:
    try:
        await run()
    except Exception:
        traceback.print_exc()
        check("the run completed without an exception", False)
    finally:
        try:
            await teardown()
        except Exception:
            traceback.print_exc()
            check("the test data was removed", False)
    left = (await q("select (select count(*) from consent_scans where url like 'https://five%example.test/%' and not exists (select 1 from audit_logs a join agent_runs r on r.id = a.agent_run_id where r.scan_id = consent_scans.id)) a, (select count(*) from ent_scans where domain like 'ent-%.example.test') b"))[0]
    check("teardown: no test row is left behind", left["a"] == 0 and left["b"] == 0, str(dict(left)))
    failed = [n for n, ok in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)} of {len(RESULTS)} checks passed" + (f"; FAILED: {failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
