-- 0050: Enterprise scanner foundation (Phase 1). Three NEW tables, prefix ent_. Additive: nothing existing is altered, read or joined.
--
-- ent_scans     one row per Enterprise scan (a separate product from the 5-page consent scan; its own table, its own status).
-- ent_frontier  the crawl queue, kept in the database so a scan survives a crash and resumes where it stopped. One row per distinct normalized URL per scan.
-- ent_evidence  raw captured content (page text, document text, ...) with a content hash, so every finding built later can point at the exact source and duplicates are not stored twice.
--
-- Row-level security follows the existing pattern (0018, 0047): ENABLED and FORCED, tenancy through the scan's org_id. ent_scans carries org_id itself;
-- the other two reach it through the scan.

create table if not exists ent_scans (
    id                 uuid primary key default gen_random_uuid(),
    org_id             uuid not null,
    website_url        text not null,
    domain             text not null,
    status             text not null default 'queued'
                       check (status in ('queued', 'running', 'completed', 'failed', 'cancelled')),
    options            jsonb not null default '{}'::jsonb,
    stop_reason        text,
    error              text,
    created_by_user_id uuid,
    created_at         timestamptz not null default now(),
    started_at         timestamptz,
    completed_at       timestamptz,
    updated_at         timestamptz not null default now(),
    -- a failed scan always says why
    constraint ent_scans_failed_has_error check (status <> 'failed' or error is not null)
);
create index if not exists ent_scans_org_idx on ent_scans (org_id, created_at desc);
create index if not exists ent_scans_status_idx on ent_scans (status);

create table if not exists ent_frontier (
    id                uuid primary key default gen_random_uuid(),
    scan_id           uuid not null references ent_scans(id) on delete cascade,
    normalized_url    text not null,
    original_url      text not null,
    depth             integer not null check (depth >= 0),
    discovery_source  text not null,
    discovery_sources text[] not null default '{}',
    status            text not null default 'pending'
                      check (status in ('pending', 'in_progress', 'done', 'failed', 'excluded')),
    attempts          integer not null default 0 check (attempts >= 0),
    last_error        text,
    status_reason     text,
    http_status       integer,
    final_url         text,
    content_type      text,
    claimed_by        text,
    claimed_at        timestamptz,
    discovered_at     timestamptz not null default now(),
    updated_at        timestamptz not null default now(),
    constraint ent_frontier_unique unique (scan_id, normalized_url),
    -- a failure and an exclusion always say why; a claimed row says who and when
    constraint ent_frontier_failed_has_error check (status <> 'failed' or last_error is not null),
    constraint ent_frontier_excluded_has_reason check (status <> 'excluded' or status_reason is not null),
    constraint ent_frontier_claim_complete check (status <> 'in_progress' or (claimed_by is not null and claimed_at is not null))
);
-- the claim query: the next pending URL of one scan, shallowest first
create index if not exists ent_frontier_claim_idx on ent_frontier (scan_id, status, depth, discovered_at);

create table if not exists ent_evidence (
    id               uuid primary key default gen_random_uuid(),
    scan_id          uuid not null references ent_scans(id) on delete cascade,
    frontier_id      uuid references ent_frontier(id) on delete set null,
    kind             text not null
                     check (kind in ('page_html', 'page_text', 'network', 'cookie', 'form', 'document_text', 'screenshot', 'header')),
    source_url       text not null,
    -- 1-based page of a document; 0 means "not a paged source" (a web page)
    page_number      integer not null default 0 check (page_number >= 0),
    content_hash     text not null,
    content_type     text,
    byte_size        integer not null check (byte_size >= 0),
    content          text not null,
    detection_method text not null,
    captured_at      timestamptz not null default now(),
    -- the same content from the same place is stored once per scan
    constraint ent_evidence_unique unique (scan_id, kind, source_url, page_number, content_hash)
);
create index if not exists ent_evidence_scan_idx on ent_evidence (scan_id, kind);
create index if not exists ent_evidence_source_idx on ent_evidence (scan_id, source_url);

alter table ent_scans enable row level security;
alter table ent_scans force row level security;
alter table ent_frontier enable row level security;
alter table ent_frontier force row level security;
alter table ent_evidence enable row level security;
alter table ent_evidence force row level security;
do $$
begin
    if to_regprocedure('current_org_id()') is null then
        raise notice 'ent_*: current_org_id() not present -- row-level security policies NOT created. Tenancy rests on the scan lookup in every query.';
        return;
    end if;
    if not exists (select 1 from pg_policies where schemaname = current_schema() and tablename = 'ent_scans' and policyname = 'tenant_isolation') then
        create policy tenant_isolation on ent_scans using (org_id = current_org_id()) with check (org_id = current_org_id());
    end if;
    if not exists (select 1 from pg_policies where schemaname = current_schema() and tablename = 'ent_frontier' and policyname = 'tenant_isolation') then
        create policy tenant_isolation on ent_frontier
            using (exists (select 1 from ent_scans s where s.id = ent_frontier.scan_id and s.org_id = current_org_id()));
    end if;
    if not exists (select 1 from pg_policies where schemaname = current_schema() and tablename = 'ent_evidence' and policyname = 'tenant_isolation') then
        create policy tenant_isolation on ent_evidence
            using (exists (select 1 from ent_scans s where s.id = ent_evidence.scan_id and s.org_id = current_org_id()));
    end if;
    if exists (select 1 from pg_roles where rolname = 'consiva_app') then
        grant select, insert, update on ent_scans, ent_frontier, ent_evidence to consiva_app;
    end if;
end $$;
