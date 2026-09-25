-- 0022: the three things the integration API needs that the scan table cannot already express.
--
-- Deliberately columns on consent_scans rather than a new table. A scan requested
-- through the API is the SAME scan a scan requested through the console is -- same
-- pipeline, same evidence, same findings, same row-level security. Giving the API its
-- own scan table would fork the one thing this integration layer exists not to fork.
--
--   idempotency_key  so a retried POST returns the existing scan instead of paying for
--                    a second browser crawl. Scoped per organisation, not globally:
--                    two tenants choosing the same key is a coincidence, not a
--                    collision, and a global unique index would leak one tenant's key
--                    space into another's.
--
--   webhook_url      where to POST when the scan finishes. Nullable -- polling stays
--                    the default and webhooks are opt-in.
--
--   scan_options     what the caller asked for, as sent. Stored rather than applied and
--                    forgotten so a result can be read back against the configuration
--                    that produced it; the effective values are clamped server-side and
--                    the clamped set is what gets recorded here.
--
-- All three are nullable/defaulted and read by nothing that already exists, so every
-- current code path behaves exactly as it did.
alter table consent_scans
    add column if not exists idempotency_key text,
    add column if not exists webhook_url      text,
    add column if not exists scan_options     jsonb not null default '{}'::jsonb;

-- Partial, so the overwhelming majority of scans (no key) cost nothing and are not
-- forced to collide on NULL.
create unique index if not exists consent_scans_org_idempotency_key_uidx
    on consent_scans (org_id, idempotency_key)
    where idempotency_key is not null;

-- Webhook attempts, kept out of audit_logs on purpose: audit_logs records what a
-- PERSON did to a tenant's data, and a delivery attempt to a third-party endpoint is
-- neither. Retry scheduling itself is the existing job queue's (agent_jobs.attempts +
-- run_after backoff); this table is the durable record of what was sent, what came
-- back, and how often it failed, which the queue row does not keep once the job is
-- reaped.
create table if not exists webhook_deliveries (
    id              uuid primary key default gen_random_uuid(),
    org_id          uuid not null references organizations(id) on delete cascade,
    scan_id         uuid not null references consent_scans(id) on delete cascade,
    event           text not null,
    target_url      text not null,
    attempt         integer not null default 1,
    status          text not null check (status in ('pending','delivered','failed')),
    response_status integer,
    error           text,
    created_at      timestamptz not null default now(),
    delivered_at    timestamptz
);

create index if not exists webhook_deliveries_scan_id_idx on webhook_deliveries (scan_id);
create index if not exists webhook_deliveries_org_id_idx on webhook_deliveries (org_id);

-- Same tenant isolation as every other table here: migration 0018 forced RLS across
-- the schema, and a table added afterwards has to opt in explicitly or it is the one
-- place cross-tenant reads still work.
alter table webhook_deliveries enable row level security;
alter table webhook_deliveries force row level security;

drop policy if exists webhook_deliveries_org_isolation on webhook_deliveries;
create policy webhook_deliveries_org_isolation on webhook_deliveries
    using (org_id = current_org_id())
    with check (org_id = current_org_id());

grant select, insert, update on webhook_deliveries to consiva_app;
