-- 0041: one row per LLM call made for a Consent Agent scan, as the provider reported it.
--
-- Before this, the only per-call record was a flat file (logs/llm_usage.jsonl) inside each container, lost on every rebuild, with no stage, no
-- request id, no cache or reasoning tokens, no duration, and nothing at all for a call that failed (a refusal or an exhausted retry loop is still
-- billed). The stage records (agent_run_stages.metadata) held the usage of the analysis call only.
--
-- A row is written by app/llm/usage_tracking.py after each provider call group (the primary provider and, if it failed, the fallback are separate rows).
-- `call_id` is derived from the call group and the provider, so a repeated callback for the same call cannot be counted twice: the second insert
-- does nothing. `reasoning_tokens` is the part of `output_tokens` the provider says was reasoning (already inside output_tokens; never add it again);
-- NULL means the provider did not report it. `cost_usd` is NULL when no price is known for the model, never 0.
--
-- Additive: a new table, nothing existing is altered. Tenancy follows the scan (as agent_run_stages does), not a copied org_id.
create table if not exists llm_usage_events (
    id                 uuid primary key default gen_random_uuid(),
    call_id            uuid not null unique,
    scan_id            uuid not null references consent_scans(id) on delete cascade,
    agent_run_id       uuid,
    agent              text not null,
    stage              text,
    provider           text not null,
    model              text not null,
    request_ids        text[] not null default '{}',
    attempts           integer not null default 0,
    input_tokens       integer not null default 0,
    output_tokens      integer not null default 0,
    cache_read_tokens  integer not null default 0,
    cache_write_tokens integer not null default 0,
    reasoning_tokens   integer,
    status             text not null check (status in ('success', 'failed')),
    error_type         text,
    fallback_triggered boolean not null default false,
    duration_ms        integer,
    cost_usd           numeric(12, 6),
    currency           text not null default 'USD',
    price_source       text,
    created_at         timestamptz not null default now()
);

create index if not exists llm_usage_events_scan_id_idx on llm_usage_events (scan_id);
create index if not exists llm_usage_events_created_at_idx on llm_usage_events (created_at);

alter table llm_usage_events enable row level security;
do $$
begin
    if to_regprocedure('current_org_id()') is null then
        raise notice 'llm_usage_events: current_org_id() not present -- row-level security policy NOT created. Tenancy rests on the scan lookup in every query.';
        return;
    end if;
    if not exists (select 1 from pg_policies where schemaname = current_schema() and tablename = 'llm_usage_events' and policyname = 'tenant_isolation') then
        create policy tenant_isolation on llm_usage_events
            using (exists (select 1 from consent_scans cs where cs.id = llm_usage_events.scan_id and cs.org_id = current_org_id()));
    end if;
    if exists (select 1 from pg_roles where rolname = 'consiva_app') then
        grant select, insert on llm_usage_events to consiva_app;
    end if;
end $$;
