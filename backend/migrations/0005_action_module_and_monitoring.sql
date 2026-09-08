-- Action Module (Build Plan Component 8) + Continuous Monitoring / diff engine
-- (Component 9) -- the two remaining components from the master reference document.
-- Additive only; idempotent (IF NOT EXISTS / ON CONFLICT-safe re-run), same convention
-- as every other migration in this project.

-- ── Action Module ────────────────────────────────────────────────────────────────
-- One row per tracked follow-up from an approved finding: a task (assignable, no
-- user/team table exists yet so assignee is a free-text label), a notification
-- record (real, queryable -- NOT a fabricated "email sent" claim; actual outbound
-- delivery needs a real provider wired in later), or a consent-config change that
-- must pass through staged -> live as two distinct, separately-audited steps.
create table if not exists actions (
    id                uuid primary key default gen_random_uuid(),
    org_id            uuid not null,
    finding_id        uuid not null references consent_findings(id),
    action_type       text not null check (action_type in ('task', 'notification', 'config_change')),
    title             text not null,
    description       text,
    assignee_label    text,               -- free-text ("web team", "vendor: Acme") -- no user/team table exists
    config_payload    jsonb,              -- config_change only: the actual proposed change
    status            text not null default 'open'
        check (status in ('open', 'in_progress', 'staged', 'live', 'done', 'cancelled')),
    staged_at         timestamptz,
    deployed_at       timestamptz,
    created_by_user_id uuid,
    created_at        timestamptz not null default now(),
    updated_at        timestamptz not null default now()
);
create index if not exists actions_org_id_idx on actions (org_id);
create index if not exists actions_finding_id_idx on actions (finding_id);

-- ── Continuous Monitoring ────────────────────────────────────────────────────────
-- One row per website under a recurring re-scan schedule. baseline_scan_id is the
-- human-approved reference point every future scan is diffed against; it only
-- advances when a reviewer explicitly promotes a new scan to baseline (never
-- automatically -- the whole point is a human sees what changed).
create table if not exists scan_schedules (
    id                     uuid primary key default gen_random_uuid(),
    org_id                 uuid not null,
    website_id             uuid not null references websites(id),
    interval_hours         integer not null check (interval_hours > 0),
    enabled                boolean not null default true,
    baseline_scan_id       uuid references consent_scans(id),
    last_triggered_scan_id uuid references consent_scans(id),
    next_run_at            timestamptz not null default now(),
    created_at             timestamptz not null default now(),
    updated_at             timestamptz not null default now(),
    unique (website_id)
);
create index if not exists scan_schedules_org_id_idx on scan_schedules (org_id);
create index if not exists scan_schedules_due_idx on scan_schedules (enabled, next_run_at);

-- One row per (baseline, new) comparison actually performed -- persisted (not just
-- computed on the fly) so "what changed and when" is itself part of the permanent,
-- reconstructable record, not just a live-only view.
create table if not exists scan_diffs (
    id                 uuid primary key default gen_random_uuid(),
    org_id             uuid not null,
    website_id         uuid not null references websites(id),
    baseline_scan_id   uuid not null references consent_scans(id),
    new_scan_id        uuid not null references consent_scans(id),
    added              jsonb not null default '{}',
    removed            jsonb not null default '{}',
    changed            jsonb not null default '{}',
    has_material_change boolean not null default false,
    created_at         timestamptz not null default now()
);
create index if not exists scan_diffs_org_id_idx on scan_diffs (org_id);
create index if not exists scan_diffs_website_id_idx on scan_diffs (website_id);
