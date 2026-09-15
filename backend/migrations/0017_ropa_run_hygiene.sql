-- Two gaps a live debug sweep found: one in Agent 2, one in the shared audit table.
--
-- ── 1. ropa_discovery_runs.status had no CHECK constraint ───────────────────
-- Every other status column in the platform is constrained -- dsr_requests.status
-- (19 values), incident_cases.status (19), consent_scans.status (4),
-- agent_jobs.status (4) -- so Agent 2 was the only place a typo'd status would be
-- stored silently instead of rejected. Agent 2 was written first and the convention
-- tightened around it afterwards.
--
-- The five values are the ones the code actually writes, traced through both ingest
-- paths rather than guessed: 'pending' at creation (ropa_repository.create_run),
-- 'discovering' while a connector reads the source, 'analyzing' while the classifier
-- runs -- that one is the evidence_push path, which a first pass over the connector
-- path alone would miss -- then 'completed' or 'failed'.
--
-- ── 2. audit_logs.agent_run_id had no index ────────────────────────────────
-- audit_logs is the largest table in the database and the only one that grows without
-- bound -- it is append-only by design and nothing prunes it. Agent 1's scan audit
-- view filters on this column (db/repositories/audit_repository.py list_for_scan:
-- `AuditLog.agent_run_id.in_(agent_run_ids)`), so that read was a sequential scan
-- over the whole audit history.
--
-- Deliberately NOT adding the other 29 unindexed foreign keys. They are integrity
-- links no query filters on -- supersedes_id, evidence_id, affected_system_id and
-- the like -- and an index nothing reads is pure write cost. This one is different
-- because there is a query behind it.
--
-- Idempotent: IF NOT EXISTS / drop-and-recreate by name, safe to re-run.

-- ── 1. The missing CHECK ────────────────────────────────────────────────────
-- Any row not matching the vocabulary is parked as 'failed' with a note rather than
-- blocking the migration. There should be none; this is here so a surprise in an
-- older environment degrades to a visible, queryable state instead of a failed deploy.
do $$
declare
    stray integer;
begin
    select count(*) into stray from ropa_discovery_runs
     where status is null or status not in ('pending', 'discovering', 'analyzing', 'completed', 'failed');

    if stray > 0 then
        raise notice 'ropa_discovery_runs: % row(s) carry an unrecognised status; marking failed', stray;
        update ropa_discovery_runs
           set status = 'failed',
               error = coalesce(error || ' | ', '')
                       || 'status ' || coalesce(status, 'NULL')
                       || ' was not a recognised value; set to failed by migration 0017'
         where status is null or status not in ('pending', 'discovering', 'analyzing', 'completed', 'failed');
    end if;
end $$;

alter table ropa_discovery_runs drop constraint if exists ropa_discovery_runs_status_check;

alter table ropa_discovery_runs add constraint ropa_discovery_runs_status_check
    check (status in ('pending', 'discovering', 'analyzing', 'completed', 'failed'));

-- ── 2. The missing index ────────────────────────────────────────────────────
-- Not CONCURRENTLY: migrate.py runs each file inside a transaction, and CREATE INDEX
-- CONCURRENTLY cannot run in one. Partial, because the column is NULL on most rows --
-- only Agent 1's LLM-analysis writes carry an agent_run_id -- so a partial index is
-- a fraction of the size and covers every query that filters on it being set.
create index if not exists ix_audit_logs_agent_run_id
    on audit_logs (agent_run_id)
    where agent_run_id is not null;
