-- 0043: pre-generated PDF reports of Consent Agent scans.
--
-- A report is the same document for everyone who may read the scan, and it takes 1.5-3 s of headless Chromium to print. The finished PDF is kept here, one
-- row per scan and report type, with a fingerprint of everything the report is drawn from (the scan, its analysis run, its findings and their review state,
-- its stages, its screenshot, today's date in India, the scoring methodology and the web app that is printed). A request is answered from the row only when
-- the fingerprint still matches; any change makes the row stale and the report is printed again, so a download is never an out-of-date report.
--
-- Additive: a new table, nothing existing is altered. Tenancy follows the scan, as scan_screenshots does.
create table if not exists report_pdfs (
    id          uuid primary key default gen_random_uuid(),
    scan_id     uuid not null references consent_scans(id) on delete cascade,
    variant     text not null check (variant in ('free', 'full', 'long')),
    fingerprint text not null,
    filename    text not null,
    pdf         bytea not null,
    size_bytes  integer not null,
    created_at  timestamptz not null default now(),
    unique (scan_id, variant)
);

create index if not exists report_pdfs_created_at_idx on report_pdfs (created_at);

alter table report_pdfs enable row level security;
do $$
begin
    if to_regprocedure('current_org_id()') is null then
        raise notice 'report_pdfs: current_org_id() not present -- row-level security policy NOT created. Tenancy rests on the scan lookup in every query.';
        return;
    end if;
    if not exists (select 1 from pg_policies where schemaname = current_schema() and tablename = 'report_pdfs' and policyname = 'tenant_isolation') then
        create policy tenant_isolation on report_pdfs
            using (exists (select 1 from consent_scans cs where cs.id = report_pdfs.scan_id and cs.org_id = current_org_id()));
    end if;
    if exists (select 1 from pg_roles where rolname = 'consiva_app') then
        grant select, insert, update, delete on report_pdfs to consiva_app;
    end if;
end $$;
