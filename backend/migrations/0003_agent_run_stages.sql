-- Stage-level timing/status for the demo UI + latency measurement (verification pass).
-- Keyed by scan_id (not agent_run_id): the pipeline the demo UI shows as one continuous
-- run actually spans two independently-retryable backend steps — scan (evidence
-- gathering) and analyze (agent reasoning) — see docs/architecture §D. scan_id is the
-- one identifier stable across both, known from the very first API call, so it's the
-- natural key for a unified timeline. agent_run_id is recorded too, nullable, for the
-- stages that happen during analysis, so a stage row can still be traced to the
-- specific agent_run that produced it.
create table if not exists agent_run_stages (
    id              uuid primary key default gen_random_uuid(),
    scan_id         uuid not null references consent_scans(id) on delete cascade,
    agent_run_id    uuid references agent_runs(id) on delete cascade,
    stage           text not null
                      check (stage in (
                          'url_validation','website_scan','data_structuring','classification',
                          'rules_check','rag_retrieval','llm_analysis','output_validation',
                          'findings_generated','audit_saved'
                      )),
    status          text not null default 'pending'
                      check (status in ('pending','running','completed','failed')),
    started_at      timestamptz,
    completed_at    timestamptz,
    duration_ms     integer,
    error           text,
    metadata        jsonb not null default '{}',
    created_at      timestamptz not null default now()
);
create index if not exists agent_run_stages_scan_id_idx on agent_run_stages (scan_id);
create index if not exists agent_run_stages_agent_run_id_idx on agent_run_stages (agent_run_id);

alter table agent_run_stages enable row level security;
do $$
begin
    if not exists (select 1 from pg_policies where schemaname = current_schema() and tablename='agent_run_stages' and policyname='tenant_isolation') then
        create policy tenant_isolation on agent_run_stages
            using (exists (select 1 from consent_scans cs
                           where cs.id = agent_run_stages.scan_id and cs.org_id = current_org_id()));
    end if;
end $$;
