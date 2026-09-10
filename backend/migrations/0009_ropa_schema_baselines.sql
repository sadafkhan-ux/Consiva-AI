-- ROPA continuous monitoring: a per-source schema baseline that later runs are
-- compared against (ROPA prompt §20).
--
-- Why a separate table rather than reusing scan_diffs: scan_diffs is keyed to
-- website_id + consent_scans and its diff logic keys on cookie/tracker identity
-- (services/diff_engine.py). A schema baseline is a different shape entirely
-- (tables/columns/types), and coupling the two would risk Agent 1's live
-- monitoring for no benefit.
--
-- Like scan_schedules.baseline_scan_id, the baseline only advances when a human
-- promotes it -- never automatically. The whole point is that someone sees what
-- changed before it becomes the new normal.
--
-- Additive only; idempotent.

create table if not exists ropa_schema_baselines (
    id                uuid primary key default gen_random_uuid(),
    org_id            uuid not null,
    source_name       text not null,
    discovery_run_id  uuid not null references ropa_discovery_runs(id),
    -- Compact fingerprint of the schema: {"table.column": "data_type"} plus the
    -- table list. Metadata only -- never row values.
    schema_snapshot   jsonb not null,
    is_current        boolean not null default true,
    promoted_by_user_id uuid,
    promoted_at       timestamptz not null default now(),
    created_at        timestamptz not null default now()
);
create index if not exists ropa_schema_baselines_org_idx on ropa_schema_baselines (org_id);
-- At most ONE current baseline per (org, source); enforced in the database so a
-- concurrent promote can't leave two "current" baselines behind.
create unique index if not exists ropa_schema_baselines_current_idx
    on ropa_schema_baselines (org_id, source_name) where is_current;

-- Detected changes, persisted so "what changed and when" is part of the
-- permanent record rather than a live-only view (same principle as scan_diffs).
create table if not exists ropa_schema_changes (
    id                uuid primary key default gen_random_uuid(),
    org_id            uuid not null,
    source_name       text not null,
    discovery_run_id  uuid not null references ropa_discovery_runs(id),
    baseline_id       uuid references ropa_schema_baselines(id),
    change_type       text not null,
    target            text not null,
    previous_value    text,
    current_value     text,
    is_material       boolean not null default false,
    review_required   boolean not null default true,
    created_at        timestamptz not null default now()
);
create index if not exists ropa_schema_changes_org_idx on ropa_schema_changes (org_id);
create index if not exists ropa_schema_changes_run_idx on ropa_schema_changes (discovery_run_id);

alter table ropa_schema_baselines enable row level security;
alter table ropa_schema_changes   enable row level security;

do $$
begin
    if not exists (select 1 from pg_policies where schemaname = current_schema() and tablename = 'ropa_schema_baselines' and policyname = 'tenant_isolation') then
        create policy tenant_isolation on ropa_schema_baselines using (org_id = current_org_id());
    end if;
    if not exists (select 1 from pg_policies where schemaname = current_schema() and tablename = 'ropa_schema_changes' and policyname = 'tenant_isolation') then
        create policy tenant_isolation on ropa_schema_changes using (org_id = current_org_id());
    end if;
end $$;
