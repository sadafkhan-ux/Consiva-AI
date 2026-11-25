"""Local clean-up of finished scans, for both scanning agents.

What it does, for every scan that has finished (and is older than the grace period, with no job still working on it):
  1. verifies that it is talking to the intended local database (otherwise it stops and changes nothing);
  2. writes the scan history (and nothing else) to a CSV, if asked, before deleting anything;
  3. deletes the ROWS of the scan-generated tables listed in registry.py: extracted text and HTML, evidence, passages, findings, assessments, scores, stored reports, checkpoints, embeddings;
  4. keeps the scan's own row: id, website address, status, error, created / started / completed times;
  5. checks that the database structure is identical, that no other table lost a row, that the history is identical, and that every constraint is valid.

It never drops or alters a table or a column, never touches a table that is not in registry.py, and keeps any row that a business table (an approval, a remediation action, the audit log) still
points at. It does nothing but report unless `execute` is true. It is safe to run again: a second run finds nothing left to delete.
"""

from __future__ import annotations

import asyncio
import csv
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.cleanup import registry as reg
from app.db.session import async_session_factory

logger = logging.getLogger(__name__)

LOCK_KEY = 7_203_041
BATCH = 100


class TargetNotVerified(RuntimeError):
    """The database this process is connected to is not the one the clean-up has been told it may change."""


@dataclass
class Settings:
    enabled: bool = True
    grace_minutes: int = 1440            # 24 hours (owner, 25 Nov 2026)
    allowed_hosts: tuple[str, ...] = ("db", "localhost", "127.0.0.1")
    allowed_databases: tuple[str, ...] = ("consiva",)
    confirm: str = ""                     # must equal "<database>@<host>"
    respect_paused_reviews: bool = False
    keep_enterprise: bool = True          # owner's decision, 25 Nov 2026: Enterprise results (score, PDF, evidence) are kept, never cleared automatically; CLEANUP_KEEP_ENTERPRISE=false clears them again

    @classmethod
    def from_env(cls) -> "Settings":
        def csv_(name, default):
            return tuple(x.strip() for x in os.environ.get(name, default).split(",") if x.strip())
        return cls(enabled=os.environ.get("CLEANUP_ENABLED", "true").lower() in ("1", "true", "yes"), grace_minutes=int(os.environ.get("CLEANUP_GRACE_MINUTES", "1440")),
                   allowed_hosts=csv_("CLEANUP_ALLOWED_DB_HOSTS", "db,localhost,127.0.0.1"), allowed_databases=csv_("CLEANUP_ALLOWED_DB_NAMES", "consiva"),
                   confirm=os.environ.get("CLEANUP_CONFIRM_DATABASE", ""), respect_paused_reviews=os.environ.get("CLEANUP_RESPECT_PAUSED_REVIEWS", "false").lower() in ("1", "true", "yes"),
                   keep_enterprise=os.environ.get("CLEANUP_KEEP_ENTERPRISE", "true").lower() in ("1", "true", "yes"))


@dataclass
class AgentReport:
    key: str
    label: str
    scans_eligible: int = 0
    scans_cleaned: int = 0
    deleted: dict[str, int] = field(default_factory=dict)
    would_delete: dict[str, int] = field(default_factory=dict)
    kept_because_referenced: dict[str, str] = field(default_factory=dict)
    history_retained: int = 0
    history_unchanged: bool | None = None


@dataclass
class Report:
    executed: bool
    target: dict
    agents: dict[str, AgentReport]
    integrity: dict = field(default_factory=dict)
    history_file: str | None = None
    seconds: float = 0.0
    problems: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        from dataclasses import asdict
        return asdict(self)


# ---- the target ---------------------------------------------------------------------------------------------------------------------------------------------------

def _database_url() -> str:
    from app.config import get_settings
    return get_settings().database_url


async def verify_target(db, settings: Settings) -> dict:
    """Stop unless this is the database the clean-up was told it may change: an allowed host and name, a matching explicit confirmation, a primary server, and no partner production webhook
    configured (a deployment that notifies the partner's production API is not a scratch copy). Nothing is checked from the data, because data can be copied; this checks where we are."""
    u = urlparse(_database_url().replace("postgresql+asyncpg://", "postgresql://", 1))
    host, name = u.hostname or "", (u.path or "/").lstrip("/")
    server_db = await db.scalar(text("select current_database()"))
    problems = []
    if host not in settings.allowed_hosts:
        problems.append(f"the database host {host!r} is not in CLEANUP_ALLOWED_DB_HOSTS {settings.allowed_hosts}")
    if name not in settings.allowed_databases or server_db not in settings.allowed_databases:
        problems.append(f"the database {server_db!r} is not in CLEANUP_ALLOWED_DB_NAMES {settings.allowed_databases}")
    if settings.confirm != f"{server_db}@{host}":
        problems.append(f"CLEANUP_CONFIRM_DATABASE must be exactly {server_db}@{host!s} to confirm this is the intended local database (it is {settings.confirm!r})")
    if await db.scalar(text("select pg_is_in_recovery()")):
        problems.append("the server is a standby replica")
    hook = os.environ.get("AGENT_WEBHOOK_URL", "")
    if hook and "uatapi" not in hook and os.environ.get("CLEANUP_ALLOW_PRODUCTION_WEBHOOK", "").lower() not in ("1", "true", "yes"):
        problems.append("AGENT_WEBHOOK_URL points at a partner endpoint: this deployment may be a production one (set CLEANUP_ALLOW_PRODUCTION_WEBHOOK=yes only after verifying it is not)")
    if problems:
        raise TargetNotVerified("; ".join(problems))
    return {"host": host, "database": server_db, "server_address": str(await db.scalar(text("select coalesce(inet_server_addr()::text, 'local socket')"))), "version": (await db.scalar(text("show server_version")))}


# ---- fingerprints and integrity ------------------------------------------------------------------------------------------------------------------------------------

_STRUCTURE = text("""
    select md5(coalesce(string_agg(concat_ws('|', c.table_name, c.column_name, c.data_type, c.is_nullable, coalesce(c.column_default, '')), ',' order by c.table_name, c.column_name), '')) as columns,
           (select count(*) from pg_constraint k join pg_namespace n on n.oid = k.connamespace where n.nspname = 'public') as constraints,
           (select count(*) from pg_indexes where schemaname = 'public') as indexes,
           (select count(*) from information_schema.tables where table_schema = 'public' and table_type = 'BASE TABLE') as tables
    from information_schema.columns c where c.table_schema = 'public'""")

_HISTORY = {
    "five_page": text("select count(*) as n, md5(coalesce(string_agg(concat_ws('|', id, org_id, url, status, coalesce(error, ''), created_at, started_at, completed_at), ',' order by id), '')) as h from consent_scans"),
    "enterprise": text("select count(*) as n, md5(coalesce(string_agg(concat_ws('|', id, org_id, website_url, status, analysis_status, coalesce(error, ''), coalesce(stop_reason, ''), coalesce(analysis_error, ''), created_at, started_at, completed_at), ',' order by id), '')) as h from ent_scans"),
}


async def _structure(db) -> dict:
    return dict((await db.execute(_STRUCTURE)).mappings().one())


async def _history(db, key: str) -> dict:
    return dict((await db.execute(_HISTORY[key])).mappings().one())


async def _table_counts(db) -> dict[str, int]:
    names = [r[0] for r in (await db.execute(text("select table_name from information_schema.tables where table_schema = 'public' and table_type = 'BASE TABLE' order by 1"))).all()]
    out = {}
    for n in names:
        out[n] = int(await db.scalar(text(f'select count(*) from "{n}"')))
    return out


# ---- which scans, which rows ---------------------------------------------------------------------------------------------------------------------------------------

_JOB_ACTIVE = "exists (select 1 from agent_jobs j where j.status <> all(cast(:jobs_done as text[])) and j.payload::text like '%' || s.id::text || '%')"

_FIVE_PAGE_ELIGIBLE = f"""
    select s.id, s.org_id from consent_scans s
    where (s.status = any(cast(:terminal as text[])) or s.created_at <= now() - interval '1 day')      -- finished, or stuck: not finished a day later with no job working on it
      and coalesce(s.completed_at, s.started_at, s.created_at) <= now() - make_interval(mins => :grace)
      and not {_JOB_ACTIVE}
      and not exists (select 1 from agent_runs r where r.scan_id = s.id and r.status in ('pending', 'running'))
      {{paused}}
    order by coalesce(s.completed_at, s.created_at)"""

_ENTERPRISE_ELIGIBLE = f"""
    select s.id, s.org_id, s.domain from ent_scans s
    where (s.status in ('failed', 'cancelled')
           or (s.status = 'completed' and (s.analysis_status in ('completed', 'partial', 'failed')
                                           or (s.analysis_status in ('not_started', 'running') and s.created_at <= now() - interval '1 day')))
           or (s.status in ('queued', 'running') and s.created_at <= now() - interval '1 day'))               -- the last: stuck, with no job working on it
      and coalesce(s.analysis_completed_at, s.completed_at, s.started_at, s.created_at) <= now() - make_interval(mins => :grace)
      and not {_JOB_ACTIVE}
    order by coalesce(s.analysis_completed_at, s.completed_at, s.created_at)"""

_ENTERPRISE_ACTIVE = f"""
    select distinct s.org_id, s.domain from ent_scans s
    where s.status in ('queued', 'running') or s.analysis_status = 'running' or (s.status = 'completed' and s.analysis_status = 'not_started' and s.created_at > now() - interval '1 day')
       or {_JOB_ACTIVE}"""


async def _referencing_guards(db, table: str, pending: set[str], after_children: bool) -> tuple[str, list[str]]:
    """SQL that keeps every row of `table` that a table OUTSIDE the clean-up still points at (an approval, an action, the audit log), and, once the children have really been deleted (`after_children`), every row
    that a child row KEPT by its own guard still points at. Returns the extra where-clause and the names of the tables behind it."""
    rows = (await db.execute(text("""
        select c.conrelid::regclass::text as child, a.attname as child_col, af.attname as parent_col
        from pg_constraint c join pg_attribute a on a.attrelid = c.conrelid and a.attnum = c.conkey[1] join pg_attribute af on af.attrelid = c.confrelid and af.attnum = c.confkey[1]
        where c.contype = 'f' and c.confrelid = cast(:t as regclass) and array_length(c.conkey, 1) = 1 and c.conrelid <> c.confrelid"""), {"t": table})).mappings().all()
    clauses, behind = [], []
    for r in rows:
        child = r["child"].replace('"', "")
        if child in pending and not after_children:
            continue
        clauses.append(f'"{table}"."{r["parent_col"]}" not in (select "{r["child_col"]}" from "{child}" where "{r["child_col"]}" is not null)')
        if child not in pending:
            behind.append(child)
    return (" and " + " and ".join(clauses)) if clauses else "", behind


async def _run_step(db, step: reg.Step, params: dict, pending: set[str], execute: bool) -> tuple[int, list[str]]:
    guard, behind = await _referencing_guards(db, step.table, pending, execute)
    where = f"({step.where}){guard}"
    if not execute:
        return int(await db.scalar(text(f'select count(*) from "{step.table}" where {where}'), params)), behind
    try:
        async with db.begin_nested():                          # a savepoint: a row that is still referenced must not undo the rest of the batch
            res = await db.execute(text(f'delete from "{step.table}" where {where}'), params)
            return res.rowcount or 0, behind
    except DBAPIError as exc:
        if "foreign key" in str(exc).lower() or "violates" in str(exc).lower():
            logger.warning("cleanup: %s kept: rows are still referenced (%s)", step.table, str(exc.orig)[:160] if getattr(exc, "orig", None) else "")
            return 0, behind + ["(a foreign key still references some rows)"]
        raise


async def _export_history(db, directory: Path, copies: bool = True) -> str:
    """Write the scan history (and nothing else) to a CSV before anything is deleted. With `copies` it is a new timestamped file; without (the timer) it overwrites scan_history_latest.csv, so a
    pass every few minutes does not fill the folder."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (f"scan_history_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.csv" if copies else "scan_history_latest.csv")
    rows = (await db.execute(text(
        "select 'five_page' as agent, id::text as scan_id, org_id::text as org_id, url, status, coalesce(error, '') as reason, created_at, started_at, completed_at from consent_scans "
        "union all select 'enterprise', id::text, org_id::text, website_url, status || '/' || analysis_status, coalesce(analysis_error, error, stop_reason, ''), created_at, started_at, coalesce(analysis_completed_at, completed_at) from ent_scans "
        "order by 1, 7"))).all()
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["agent", "scan_id", "org_id", "website_url", "status", "reason", "created_at", "started_at", "completed_at"])
        w.writerows([[str(c) if c is not None else "" for c in r] for r in rows])
    return str(path)


async def run(*, execute: bool = False, settings: Settings | None = None, agents: tuple[str, ...] = ("five_page", "enterprise"), history_dir: str | None = None,
              only_scan: uuid.UUID | None = None, only_orgs: list[uuid.UUID] | None = None, history_copies: bool = True) -> Report:
    """One clean-up pass. With execute false nothing is changed: the report says what would be."""
    settings = settings or Settings.from_env()
    if settings.keep_enterprise and agents != ("enterprise",):
        # Enterprise results are kept (owner's decision, 25 Nov 2026): the automatic passes (the cleanup service and the pass after each Enterprise scan) clear the 5-page agent only.
        # Only an explicit `--agent enterprise` run still clears Enterprise scans. The 5-page agent's cleanup is unchanged.
        agents = tuple(a for a in agents if a != "enterprise")
    t0 = time.monotonic()
    async with async_session_factory() as lock_db:
        target = await verify_target(lock_db, settings)
        if not await lock_db.scalar(text("select pg_try_advisory_lock(:k)"), {"k": LOCK_KEY}):
            raise RuntimeError("another clean-up is already running")
        try:
            return await _run_locked(execute, settings, agents, history_dir, only_scan, target, t0, only_orgs, history_copies)
        finally:
            await lock_db.execute(text("select pg_advisory_unlock(:k)"), {"k": LOCK_KEY})


async def _run_locked(execute, settings, agents, history_dir, only_scan, target, t0, only_orgs=None, history_copies=True) -> Report:
    report = Report(executed=execute, target=target, agents={})
    async with async_session_factory() as db:
        structure_before = await _structure(db)
        counts_before = await _table_counts(db)
        history_before = {k: await _history(db, k) for k in _HISTORY}
        base = {"terminal": list(reg.FIVE_PAGE_TERMINAL), "grace": settings.grace_minutes, "jobs_done": list(reg.JOBS_DONE)}
        paused = "" if not settings.respect_paused_reviews else "and not exists (select 1 from agent_runs r where r.scan_id = s.id and r.status = 'paused')"
        scans: dict[str, list] = {}
        if "five_page" in agents:
            scans["five_page"] = list((await db.execute(text(_FIVE_PAGE_ELIGIBLE.format(paused=paused)), base)).mappings().all())
        if "enterprise" in agents:
            scans["enterprise"] = list((await db.execute(text(_ENTERPRISE_ELIGIBLE), {"grace": settings.grace_minutes, "jobs_done": list(reg.JOBS_DONE)})).mappings().all())
        if only_scan is not None:
            scans = {k: [r for r in v if r["id"] == only_scan] for k, v in scans.items()}
        if only_orgs is not None:                          # used by the tests, to keep a pass to throw-away organisations
            scans = {k: [r for r in v if r["org_id"] in only_orgs] for k, v in scans.items()}
        active_pairs = {(r["org_id"], r["domain"]) for r in (await db.execute(text(_ENTERPRISE_ACTIVE), {"jobs_done": list(reg.JOBS_DONE)})).mappings().all()} if "enterprise" in agents else set()
        five_active = int(await db.scalar(text("select count(*) from consent_scans where status <> all(cast(:t as text[]))"), {"t": list(reg.FIVE_PAGE_TERMINAL)})) if "five_page" in agents else 0

        if history_dir and execute and any(scans.get(k) for k in scans):                  # only when something is about to be cleared
            report.history_file = await _export_history(db, Path(history_dir), history_copies)
        pending_tables = reg.all_cleared_tables()
        for agent in reg.AGENTS:
            if agent.key not in agents:
                continue
            ar = report.agents[agent.key] = AgentReport(agent.key, agent.label)
            rows = scans.get(agent.key, [])
            ar.scans_eligible = len(rows)
            ids_all = [r["id"] for r in rows]
            for i in range(0, len(ids_all), BATCH):
                ids = ids_all[i:i + BATCH]
                for step in agent.steps:
                    n, behind = await _run_step(db, step, {"ids": ids}, pending_tables, execute)
                    (ar.deleted if execute else ar.would_delete)[step.table] = (ar.deleted if execute else ar.would_delete).get(step.table, 0) + n
                    if behind:
                        ar.kept_because_referenced[step.table] = ", ".join(sorted(set(behind)))
                if execute and agent.key == "enterprise":
                    await db.execute(text("update ent_scans set text_cleared_at = coalesce(text_cleared_at, now()), purge_summary = cast(:p as jsonb) where id = any(cast(:ids as uuid[]))"),
                                     {"ids": ids, "p": json.dumps({"cleared_by": reg.FULL_CLEAR_MARK, "kept": "scan id, website, status, times"})})
                if execute:
                    await db.commit()
                ar.scans_cleaned += len(ids) if execute else 0
            if agent.key == "enterprise":
                # websites of the cleared scans, and websites whose scans no longer exist at all (a knowledge base left behind has no scan to come back for)
                orphan = [(r["org_id"], r["domain"]) for r in (await db.execute(text(
                    "select distinct d.org_id, d.domain from ent_policy_docs d where not exists (select 1 from ent_scans s where s.org_id = d.org_id and s.domain = d.domain)"))).mappings().all()
                    if only_orgs is None or r["org_id"] in only_orgs]
                pairs = sorted(({(r["org_id"], r["domain"]) for r in rows} | set(orphan)) - active_pairs, key=str)
                orgs_with_active = {o for o, _ in active_pairs}
                orgs = sorted({r["org_id"] for r in rows} - orgs_with_active, key=str)
                for step in reg.ENTERPRISE_SITE_STEPS:
                    if pairs:
                        n, behind = await _run_step(db, step, {"orgs": [p[0] for p in pairs], "domains": [p[1] for p in pairs]}, pending_tables, execute)
                        (ar.deleted if execute else ar.would_delete)[step.table] = n
                for step in reg.ENTERPRISE_ORG_STEPS:
                    if orgs and rows:
                        n, behind = await _run_step(db, step, {"orgs": orgs}, pending_tables, execute)
                        (ar.deleted if execute else ar.would_delete)[step.table] = n
                if execute:
                    await db.commit()
            if agent.key == "five_page" and rows and five_active == 0 and only_orgs is None:
                for step in reg.FIVE_PAGE_GLOBAL_STEPS:
                    n, _ = await _run_step(db, step, {}, pending_tables, execute)
                    (ar.deleted if execute else ar.would_delete)[step.table] = n
                if execute:
                    await db.commit()

        # ---- integrity -------------------------------------------------------------------------------------------------------------------------------------------
        structure_after = await _structure(db)
        counts_after = await _table_counts(db)
        history_after = {k: await _history(db, k) for k in _HISTORY}
        cleared = reg.all_cleared_tables()
        lost = {t: counts_before[t] - counts_after.get(t, 0) for t in counts_before if t not in cleared and counts_after.get(t, 0) < counts_before[t]}
        invalid = int(await db.scalar(text("select count(*) from pg_constraint where not convalidated")))
        for k, ar in report.agents.items():
            ar.history_retained = history_after[k]["n"]
            ar.history_unchanged = history_after[k] == history_before[k]
        report.integrity = {
            "structure_unchanged": structure_after == structure_before, "structure": structure_after,
            "other_tables_lost_rows": lost, "invalid_constraints": invalid,
            "history_unchanged": {k: history_after[k] == history_before[k] for k in report.agents},
        }
        ok = report.integrity["structure_unchanged"] and not lost and invalid == 0 and all(report.integrity["history_unchanged"].values())
        report.integrity["ok"] = bool(ok)
        if not ok:
            report.problems.append("the integrity check failed: see the integrity section")
    report.seconds = round(time.monotonic() - t0, 1)
    return report


async def loop(interval_seconds: int, **kw) -> None:
    """Run a pass every `interval_seconds`, for ever. A failed pass is logged and the loop goes on."""
    while True:
        try:
            r = await run(execute=True, history_copies=False, **kw)
            done = {k: (v.scans_cleaned, sum(v.deleted.values())) for k, v in r.agents.items()}
            if any(n for _, n in done.values()) or r.problems:                 # a pass that found nothing to delete is not worth a line
                logger.info("cleanup pass: %s (scans in scope, rows deleted) problems=%s history=%s", done, r.problems, r.history_file)
        except TargetNotVerified as exc:
            logger.error("cleanup stopped: the target database was not verified: %s", exc)
            await asyncio.sleep(max(interval_seconds, 300))
        except Exception:
            logger.exception("cleanup pass failed")
        await asyncio.sleep(interval_seconds)


async def after_scan() -> None:
    """Called when an Enterprise scan's analysis has finished: one pass over everything that has finished long enough ago. Never raises, and never runs against a database that was not verified.
    (The `cleanup` service does the same on a timer, and is what clears five-page scans, whose code is not touched.)"""
    settings = Settings.from_env()
    if not settings.enabled:
        return
    try:
        await run(execute=True, settings=settings)
    except TargetNotVerified as exc:
        logger.warning("cleanup skipped: the target database was not verified (%s)", str(exc)[:200])
    except Exception:
        logger.exception("cleanup after a scan failed")
