-- 0040: the homepage preview of each Consent Agent scan.
--
-- At the start of every scan the crawler photographs the first screen of the homepage it just loaded, in the same browser and the same
-- load as the pre-consent crawl (see app/scanner/crawler.py, _capture_homepage_screenshot). The picture is kept here, one row per scan,
-- so the dashboard and every PDF made later show the SAME real homepage, and a reader can see at a glance that the right site was scanned.
--
-- One row per scan, always: when no usable picture could be taken the row says why (status + reason code) and holds no image, so a report can
-- say so plainly instead of guessing. Statuses: 'ok' (image present); 'blocked' (the site answered the scanner with an error or challenge page,
-- which is deliberately NOT kept: a block page is not the client's website); 'timeout'; 'failed'.
--
-- Additive: a new table, nothing existing is altered. Tenancy follows the scan (as agent_run_stages does), not a copied org_id.
create table if not exists scan_screenshots (
    id          uuid primary key default gen_random_uuid(),
    scan_id     uuid not null unique references consent_scans(id) on delete cascade,
    status      text not null check (status in ('ok', 'blocked', 'timeout', 'failed')),
    reason      text,
    mime        text,
    width       integer,
    height      integer,
    http_status integer,
    page_url    text,
    page_title  text,
    image       bytea,
    captured_at timestamptz not null default now(),
    -- An 'ok' row carries its image and its size; any other status carries no image.
    check ((status = 'ok' and image is not null and width is not null and height is not null) or (status <> 'ok' and image is null))
);

alter table scan_screenshots enable row level security;
do $$
begin
    if to_regprocedure('current_org_id()') is null then
        raise notice 'scan_screenshots: current_org_id() not present -- row-level security policy NOT created. Tenancy rests on the scan lookup in every query.';
        return;
    end if;
    if not exists (select 1 from pg_policies where schemaname = current_schema() and tablename = 'scan_screenshots' and policyname = 'tenant_isolation') then
        create policy tenant_isolation on scan_screenshots
            using (exists (select 1 from consent_scans cs where cs.id = scan_screenshots.scan_id and cs.org_id = current_org_id()));
    end if;
    if exists (select 1 from pg_roles where rolname = 'consiva_app') then
        grant select, insert, update on scan_screenshots to consiva_app;
    end if;
end $$;
