-- Consistency fix, found by a full live-schema audit: migration 0005 added actions,
-- scan_schedules and scan_diffs but never enabled RLS or added a tenant_isolation
-- policy on them, unlike every other org-scoped table in this schema (websites,
-- consent_scans, agent_jobs, audit_logs, etc. all have both). Confirmed live via
-- pg_class.relrowsecurity: these 3 tables were the only org-scoped tables with RLS
-- disabled.
--
-- Enforcement in this application happens at the query layer regardless (the backend
-- connects with a direct Postgres/service-role connection, so auth.jwt() is never
-- populated and RLS never actually applies here -- see scan_repository.get_scan's own
-- docstring) -- so this is a defense-in-depth / consistency fix, not a behavior
-- change: nothing this application does differently before or after.
--
-- Additive only; idempotent (guarded the same way 0002 guards its own policies).

alter table actions enable row level security;
alter table scan_schedules enable row level security;
alter table scan_diffs enable row level security;

do $$
begin
    if not exists (select 1 from pg_policies where schemaname = current_schema() and tablename = 'actions' and policyname = 'tenant_isolation') then
        create policy tenant_isolation on actions
            using (exists (select 1 from consent_findings f
                           join consent_scans cs on cs.id = f.scan_id
                           where f.id = actions.finding_id and cs.org_id = current_org_id()));
    end if;
    if not exists (select 1 from pg_policies where schemaname = current_schema() and tablename = 'scan_schedules' and policyname = 'tenant_isolation') then
        create policy tenant_isolation on scan_schedules
            using (org_id = current_org_id());
    end if;
    if not exists (select 1 from pg_policies where schemaname = current_schema() and tablename = 'scan_diffs' and policyname = 'tenant_isolation') then
        create policy tenant_isolation on scan_diffs
            using (org_id = current_org_id());
    end if;
end $$;
