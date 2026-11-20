-- 0062: Enterprise DPDP method 3.0.0 results (approved by the owner on 20 Nov 2026).
--
-- Method 3.0.0 assesses 96 parameters (one per independently assessable DPDP requirement) from the evidence and facts a scan already stored, and scores them out of the
-- applicable maximum (Pass 100%, Partial 50%, Fail 0; conditional duties that do not apply are removed from the maximum). Every calculation is a row; the latest is the current
-- method 3.0.0 result, the rest are its history. The 51-parameter score in ent_score_results is untouched and keeps its API. Additive: one new table.

create table if not exists ent_method3_results (
    id                     uuid primary key default gen_random_uuid(),
    scan_id                uuid not null references ent_scans(id) on delete cascade,
    version                text not null,
    catalogue_fingerprint  text not null,
    as_of                  date not null,
    score                  numeric(4, 1) check (score is null or (score >= 0 and score <= 100)),
    marks_earned           numeric(6, 3) not null,
    applicable_max         numeric(6, 3) not null,
    basis                  text not null,
    result                 jsonb not null,
    created_at             timestamptz not null default now()
);
create index if not exists ent_method3_results_scan_idx on ent_method3_results (scan_id, created_at desc);

alter table ent_method3_results enable row level security;
alter table ent_method3_results force row level security;

do $$
begin
    if to_regprocedure('current_org_id()') is null then
        raise notice 'ent_method3_results: current_org_id() not present -- row-level security policy NOT created. Tenancy rests on the scan lookup in every query.';
    elsif not exists (select 1 from pg_policies where schemaname = current_schema() and tablename = 'ent_method3_results' and policyname = 'tenant_isolation') then
        execute 'create policy tenant_isolation on ent_method3_results using (exists (select 1 from ent_scans s where s.id = ent_method3_results.scan_id and s.org_id = current_org_id()))';
    end if;
    if exists (select 1 from pg_roles where rolname = 'consiva_app') then
        grant select, insert on ent_method3_results to consiva_app;
    end if;
end $$;
