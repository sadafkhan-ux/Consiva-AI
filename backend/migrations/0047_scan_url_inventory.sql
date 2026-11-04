-- 0047: the URL inventory, and row-level security forced on four tables that enabled it without forcing it.
--
-- PART 1: scan_url_inventory. One row per distinct normalized URL per scan: where it was found, how deep, what state it is in and why,
-- and what the page said when it was loaded. It is the record later modules (page extraction, cookies, tokens, storage, network) attach to,
-- through scan_id and website_page_id. Discovered does not mean scanned: a URL the crawl never reached stays PENDING.
-- See docs/URL_INVENTORY_DESIGN.md. Additive: nothing existing is altered by this part.
create table if not exists scan_url_inventory (
    id                uuid primary key default gen_random_uuid(),
    scan_id           uuid not null references consent_scans(id) on delete cascade,
    website_page_id   uuid references website_pages(id) on delete set null,
    domain            text not null,
    original_url      text not null,
    normalized_url    text not null,
    final_url         text,
    page_title        text,
    http_status       integer,
    content_type      text,
    discovery_source  text not null,
    discovery_sources text[] not null default '{}',
    crawl_depth       integer not null check (crawl_depth >= 0),
    scan_status       text not null default 'PENDING'
                      check (scan_status in ('PENDING', 'IN_PROGRESS', 'COMPLETED', 'FAILED', 'PARTIALLY_SCANNED', 'EXCLUDED')),
    status_reason     text,
    failure_reason    text,
    discovered_at     timestamptz not null default now(),
    scan_started_at   timestamptz,
    scan_completed_at timestamptz,
    last_updated_at   timestamptz not null default now(),
    constraint scan_url_inventory_unique unique (scan_id, normalized_url),
    -- a failure always says why, and an exclusion always says why
    constraint scan_url_inventory_failed_has_reason check (scan_status <> 'FAILED' or failure_reason is not null),
    constraint scan_url_inventory_excluded_has_reason check (scan_status <> 'EXCLUDED' or status_reason is not null)
);
create index if not exists scan_url_inventory_status_idx on scan_url_inventory (scan_id, scan_status);
create index if not exists scan_url_inventory_depth_idx on scan_url_inventory (scan_id, crawl_depth);
create index if not exists scan_url_inventory_http_idx on scan_url_inventory (scan_id, http_status);

alter table scan_url_inventory enable row level security;
alter table scan_url_inventory force row level security;
do $$
begin
    if to_regprocedure('current_org_id()') is null then
        raise notice 'scan_url_inventory: current_org_id() not present -- row-level security policy NOT created. Tenancy rests on the scan lookup in every query.';
        return;
    end if;
    if not exists (select 1 from pg_policies where schemaname = current_schema() and tablename = 'scan_url_inventory' and policyname = 'tenant_isolation') then
        create policy tenant_isolation on scan_url_inventory
            using (exists (select 1 from consent_scans cs where cs.id = scan_url_inventory.scan_id and cs.org_id = current_org_id()));
    end if;
    if exists (select 1 from pg_roles where rolname = 'consiva_app') then
        grant select, insert, update on scan_url_inventory to consiva_app;
    end if;
end $$;

-- PART 2: FORCE row-level security where it was enabled and never forced.
-- Root cause of the failing test_rls_is_forced_on_every_tenant_table: migrations 0035 (dsr_evidence_pushes), 0040 (scan_screenshots),
-- 0041 (llm_usage_events) and 0043 (report_pdfs) each ENABLE row level security and create a policy, but none of them FORCEs it. 0018 forced it
-- only for the tables that existed then and warned that "a table added afterwards has to opt in explicitly"; these four did not. An owner role
-- bypasses its own policies unless the table is forced, so the policies on these four were inert for the owner.
-- No behaviour changes while the application connects as a superuser (a superuser bypasses row-level security regardless); it matters at the
-- cutover to the least-privilege role (0018 / cutover_rls.py), where these four would otherwise stay open to the owner. Idempotent.
alter table dsr_evidence_pushes force row level security;
alter table scan_screenshots force row level security;
alter table llm_usage_events force row level security;
alter table report_pdfs force row level security;
