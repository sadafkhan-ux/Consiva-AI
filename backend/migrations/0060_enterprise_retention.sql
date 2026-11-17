-- 0060: retention of temporary Enterprise scan data, and a small cache of reusable analysis results.
--
-- Temporary data is the extracted text and HTML of pages that no assessment cites (pages that were not read as a policy, terms, grievance, contact or about page), and the bulky intermediate
-- results of the analysis passages. After a scan has finished and a grace period has passed, the Enterprise product clears them (backend/app/enterprise/retention.py). Nothing else is touched:
-- scans, the URL inventory, assessments, findings, scores, score history, reports, the audit trail, the browser observations (cookies, requests, forms, headers, consent tests), the text of
-- pages that were read as policies, and the policy knowledge base all stay.
--
-- ent_fact_cache keeps what an analysed passage yielded (its facts, each with the quote it came from) so that a later scan of the same passage does not call the model again. It replaces the
-- reuse that depended on the intermediate rows that are now cleared. Entries unused for ENTERPRISE_FACT_CACHE_DAYS are removed.
-- Additive only.

create table if not exists ent_fact_cache (
    id            uuid primary key default gen_random_uuid(),
    org_id        uuid not null,
    text_hash     text not null,
    extractor_key text not null,
    result        jsonb not null,
    provider      text,
    model         text,
    created_at    timestamptz not null default now(),
    last_used_at  timestamptz not null default now(),
    use_count     integer not null default 0,
    constraint ent_fact_cache_unique unique (org_id, text_hash, extractor_key)
);
create index if not exists ent_fact_cache_used_idx on ent_fact_cache (org_id, last_used_at);

alter table ent_scans
    add column if not exists text_cleared_at timestamptz,
    add column if not exists purge_summary jsonb;

do $$
begin
    if exists (select 1 from pg_proc where proname = 'current_org_id') then
        alter table ent_fact_cache enable row level security;
        alter table ent_fact_cache force row level security;
        if not exists (select 1 from pg_policies where schemaname = current_schema() and tablename = 'ent_fact_cache' and policyname = 'tenant_isolation') then
            create policy tenant_isolation on ent_fact_cache using (org_id = current_org_id()) with check (org_id = current_org_id());
        end if;
        if exists (select 1 from pg_roles where rolname = 'consiva_app') then
            grant select, insert, update, delete on ent_fact_cache to consiva_app;
        end if;
    end if;
end $$;
