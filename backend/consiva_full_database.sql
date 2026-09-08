-- Consiva — complete, consolidated database schema (build artifact).
--
-- This file is a generated concatenation of every migration in migrations/, in
-- order, for the single use case of bootstrapping a brand-new, empty database in
-- one shot (e.g. a fresh Supabase project). It is NOT hand-written/maintained
-- separately from migrations/ -- migrate.py (which tracks applied migrations
-- individually via a schema_migrations table) is the source of truth and the
-- correct tool for any database that already has some migrations applied.
--
-- Regenerate after any change under migrations/ by re-concatenating those files in
-- filename order with a '-- Source: migrations/<file>' header per file (see the
-- project's migration tooling notes / migrate.py --status).
--
-- Fully idempotent: safe to run against a fresh empty database (first run) and
-- safe to re-run again afterwards (second run is a true no-op) -- verified via a
-- real isolated-schema test, twice, against a live Supabase Postgres instance.
--
-- Deliberately excludes 4 tables this project does not own: checkpoints,
-- checkpoint_blobs, checkpoint_writes, checkpoint_migrations. Those are created and
-- managed entirely by LangGraph's AsyncPostgresSaver.setup() (called once on app
-- startup, app/agents/consent_agent/graph.py) -- including them here would risk
-- drifting from that library's own internal schema versioning.

-- Source: migrations/0001_init.sql
-- Consent Agent — initial schema (docs/architecture §G).
-- Run via `supabase db push` / `psql -f`. No prior schema exists (greenfield), so this
-- is additive only.
--
-- Tenancy note: `org_id` columns are NOT foreign-keyed to an `organizations` table —
-- that table is a platform-level concern outside the Consent Agent's scope and is
-- assumed to exist (or be added) separately. See docs/architecture §P.
--
-- RLS note: policies below assume the JWT carries a top-level `org_id` claim (see
-- app/core/security.py's placeholder auth). Adjust `current_org_id()` once the real
-- auth/claims model is confirmed.
--
-- Idempotency retrofit (schema-consolidation pass): originally written as a true
-- greenfield "run once" script with no guards. CREATE TABLE/INDEX below now use
-- IF NOT EXISTS and CREATE POLICY is guarded via pg_policies, matching the convention
-- every later migration in this project already follows -- no table, column, type,
-- constraint or policy semantics changed, only re-run safety added.

create extension if not exists pgcrypto;
create extension if not exists vector;

create or replace function current_org_id() returns uuid
language sql stable
as $$
  select nullif(auth.jwt() ->> 'org_id', '')::uuid
$$;

-- ── Websites ────────────────────────────────────────────────────────────────
create table if not exists websites (
    id           uuid primary key default gen_random_uuid(),
    org_id       uuid not null,
    domain       text not null,
    verified_at  timestamptz,
    created_at   timestamptz not null default now()
);
create index if not exists websites_org_id_idx on websites (org_id);

-- ── Scans ───────────────────────────────────────────────────────────────────
create table if not exists consent_scans (
    id                      uuid primary key default gen_random_uuid(),
    org_id                  uuid not null,
    website_id              uuid not null references websites(id),
    url                     text not null,
    status                  text not null default 'pending'
                              check (status in ('pending','running','completed','failed')),
    scanner_version         text,
    authorized_by_user_id   uuid,
    error                   text,
    started_at              timestamptz,
    completed_at            timestamptz,
    created_at              timestamptz not null default now()
);
create index if not exists consent_scans_org_id_idx on consent_scans (org_id);
create index if not exists consent_scans_website_id_idx on consent_scans (website_id);

create table if not exists website_pages (
    id              uuid primary key default gen_random_uuid(),
    scan_id         uuid not null references consent_scans(id) on delete cascade,
    url             text not null,
    title           text,
    http_status     integer,
    discovered_via  text
);
create index if not exists website_pages_scan_id_idx on website_pages (scan_id);

create table if not exists trackers (
    id          uuid primary key default gen_random_uuid(),
    scan_id     uuid not null references consent_scans(id) on delete cascade,
    page_id     uuid references website_pages(id),
    script_src  text not null,
    vendor      text,
    category    text
);
create index if not exists trackers_scan_id_idx on trackers (scan_id);

create table if not exists cookies (
    id                  uuid primary key default gen_random_uuid(),
    scan_id             uuid not null references consent_scans(id) on delete cascade,
    name                text not null,
    domain              text,
    path                text,
    expiry              timestamptz,
    is_first_party      boolean,
    category            text,
    vendor              text,
    set_by_tracker_id   uuid references trackers(id)
);
create index if not exists cookies_scan_id_idx on cookies (scan_id);

create table if not exists consent_forms (
    id              uuid primary key default gen_random_uuid(),
    scan_id         uuid not null references consent_scans(id) on delete cascade,
    page_id         uuid references website_pages(id),
    selector        text,
    fields          jsonb not null default '[]',
    purpose_guess   text,
    submit_url      text
);
create index if not exists consent_forms_scan_id_idx on consent_forms (scan_id);

create table if not exists third_party_services (
    id                  uuid primary key default gen_random_uuid(),
    scan_id             uuid not null references consent_scans(id) on delete cascade,
    service_name        text not null,
    category            text,
    domains             jsonb not null default '[]',
    detection_method    text
);
create index if not exists third_party_services_scan_id_idx on third_party_services (scan_id);

create table if not exists policies (
    id                  uuid primary key default gen_random_uuid(),
    scan_id             uuid not null references consent_scans(id) on delete cascade,
    url                 text not null,
    policy_type         text not null
                          check (policy_type in ('privacy_policy','cookie_policy','terms','other')),
    extracted_text_ref  text
);
create index if not exists policies_scan_id_idx on policies (scan_id);

create table if not exists consent_signals (
    id                      uuid primary key default gen_random_uuid(),
    scan_id                 uuid not null references consent_scans(id) on delete cascade,
    mechanism_type          text not null
                              check (mechanism_type in ('banner','cmp','none','unknown')),
    cmp_vendor              text,
    has_reject_all          boolean,
    has_granular_choices    boolean,
    evidence                jsonb not null default '{}'
);
create index if not exists consent_signals_scan_id_idx on consent_signals (scan_id);

-- ── Agent runs, findings, review ───────────────────────────────────────────
create table if not exists agent_runs (
    id                      uuid primary key default gen_random_uuid(),
    scan_id                 uuid not null references consent_scans(id) on delete cascade,
    agent_name              text not null default 'consent_agent',
    status                  text not null default 'pending'
                              check (status in ('pending','running','paused','completed','failed')),
    llm_provider            text default 'nvidia',
    llm_model               text,
    langgraph_thread_id     text,
    error                   text,
    started_at              timestamptz,
    completed_at            timestamptz
);
create index if not exists agent_runs_scan_id_idx on agent_runs (scan_id);
create index if not exists agent_runs_langgraph_thread_id_idx on agent_runs (langgraph_thread_id);

create table if not exists consent_findings (
    id                      uuid primary key default gen_random_uuid(),
    scan_id                 uuid not null references consent_scans(id) on delete cascade,
    agent_run_id            uuid not null references agent_runs(id),
    category                text not null
                              check (category in ('analytics','marketing','functional','other')),
    risk_level              text not null check (risk_level in ('low','medium','high')),
    finding_text            text not null,
    evidence                jsonb not null default '[]',
    dpdp_reference          jsonb not null default '[]',
    requires_human_review   boolean not null default true,
    status                  text not null default 'pending'
                              check (status in ('pending','approved','rejected','edited')),
    created_at              timestamptz not null default now()
);
create index if not exists consent_findings_scan_id_idx on consent_findings (scan_id);
create index if not exists consent_findings_status_idx on consent_findings (status);

create table if not exists consent_recommendations (
    id                      uuid primary key default gen_random_uuid(),
    finding_id              uuid not null references consent_findings(id) on delete cascade,
    recommendation_text     text not null,
    priority                text
);
create index if not exists consent_recommendations_finding_id_idx on consent_recommendations (finding_id);

create table if not exists approvals (
    id                  uuid primary key default gen_random_uuid(),
    finding_id          uuid not null references consent_findings(id) on delete cascade,
    reviewer_user_id    uuid not null,
    decision            text not null check (decision in ('approved','rejected','edited')),
    reason              text,
    edited_payload      jsonb,
    decided_at          timestamptz not null default now()
);
create index if not exists approvals_finding_id_idx on approvals (finding_id);

create table if not exists audit_logs (
    id              uuid primary key default gen_random_uuid(),
    org_id          uuid not null,
    actor_user_id   uuid,
    action          text not null,
    entity_type     text not null,
    entity_id       uuid not null,
    before          jsonb,
    after           jsonb,
    agent_run_id    uuid references agent_runs(id),
    model_name      text,
    created_at      timestamptz not null default now()
);
create index if not exists audit_logs_org_id_idx on audit_logs (org_id);
create index if not exists audit_logs_entity_type_entity_id_idx on audit_logs (entity_type, entity_id);

-- ── RAG knowledge base ──────────────────────────────────────────────────────
create table if not exists knowledge_documents (
    id              uuid primary key default gen_random_uuid(),
    title           text not null,
    source_type     text not null
                      check (source_type in
                        ('dpdp_act','dpdp_rules','govt_guidance','govt_notification',
                         'consiva_policy','other')),
    source_ref      text not null,
    version         text,
    checksum        text,
    is_approved     boolean not null default false,
    ingested_at     timestamptz not null default now()
);

-- NOTE: vector(1024) must match NVIDIA_EMBED_DIMENSIONS. Edit before first ingest
-- if a different embedding model/dimension is chosen.
create table if not exists knowledge_chunks (
    id              uuid primary key default gen_random_uuid(),
    document_id     uuid not null references knowledge_documents(id) on delete cascade,
    chunk_index     integer not null,
    content         text not null,
    embedding       vector(1024) not null,
    token_count     integer,
    chunk_metadata  jsonb not null default '{}'
);
create index if not exists knowledge_chunks_document_id_idx on knowledge_chunks (document_id);
create index if not exists knowledge_chunks_embedding_idx on knowledge_chunks
    using hnsw (embedding vector_cosine_ops);

-- ── Background job queue (docs/architecture §B) ─────────────────────────────
create table if not exists agent_jobs (
    id          uuid primary key default gen_random_uuid(),
    org_id      uuid not null,
    job_type    text not null check (job_type in ('scan','analyze')),
    payload     jsonb not null default '{}',
    status      text not null default 'queued'
                  check (status in ('queued','running','done','failed')),
    attempts    integer not null default 0,
    run_after   timestamptz not null default now(),
    locked_at   timestamptz,
    locked_by   text,
    error       text,
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now()
);
create index if not exists agent_jobs_status_run_after_idx on agent_jobs (status, run_after);

-- ── Row Level Security ──────────────────────────────────────────────────────
alter table websites enable row level security;
alter table consent_scans enable row level security;
alter table website_pages enable row level security;
alter table trackers enable row level security;
alter table cookies enable row level security;
alter table consent_forms enable row level security;
alter table third_party_services enable row level security;
alter table policies enable row level security;
alter table consent_signals enable row level security;
alter table agent_runs enable row level security;
alter table consent_findings enable row level security;
alter table consent_recommendations enable row level security;
alter table approvals enable row level security;
alter table audit_logs enable row level security;
alter table agent_jobs enable row level security;
-- knowledge_documents/knowledge_chunks are intentionally NOT org-scoped — approved
-- DPDP/legal knowledge is shared across all tenants.

-- CREATE POLICY has no IF NOT EXISTS in Postgres -- guarded via pg_policies instead,
-- same pattern as migrations 0002/0006.
do $$
begin
    if not exists (select 1 from pg_policies where schemaname = current_schema() and tablename='websites' and policyname='tenant_isolation') then
        create policy tenant_isolation on websites using (org_id = current_org_id());
    end if;
    if not exists (select 1 from pg_policies where schemaname = current_schema() and tablename='consent_scans' and policyname='tenant_isolation') then
        create policy tenant_isolation on consent_scans using (org_id = current_org_id());
    end if;
    if not exists (select 1 from pg_policies where schemaname = current_schema() and tablename='audit_logs' and policyname='tenant_isolation') then
        create policy tenant_isolation on audit_logs using (org_id = current_org_id());
    end if;
    if not exists (select 1 from pg_policies where schemaname = current_schema() and tablename='agent_jobs' and policyname='tenant_isolation') then
        create policy tenant_isolation on agent_jobs using (org_id = current_org_id());
    end if;

    if not exists (select 1 from pg_policies where schemaname = current_schema() and tablename='website_pages' and policyname='tenant_isolation') then
        create policy tenant_isolation on website_pages
            using (exists (select 1 from consent_scans cs
                           where cs.id = website_pages.scan_id and cs.org_id = current_org_id()));
    end if;
    if not exists (select 1 from pg_policies where schemaname = current_schema() and tablename='trackers' and policyname='tenant_isolation') then
        create policy tenant_isolation on trackers
            using (exists (select 1 from consent_scans cs
                           where cs.id = trackers.scan_id and cs.org_id = current_org_id()));
    end if;
    if not exists (select 1 from pg_policies where schemaname = current_schema() and tablename='cookies' and policyname='tenant_isolation') then
        create policy tenant_isolation on cookies
            using (exists (select 1 from consent_scans cs
                           where cs.id = cookies.scan_id and cs.org_id = current_org_id()));
    end if;
    if not exists (select 1 from pg_policies where schemaname = current_schema() and tablename='consent_forms' and policyname='tenant_isolation') then
        create policy tenant_isolation on consent_forms
            using (exists (select 1 from consent_scans cs
                           where cs.id = consent_forms.scan_id and cs.org_id = current_org_id()));
    end if;
    if not exists (select 1 from pg_policies where schemaname = current_schema() and tablename='third_party_services' and policyname='tenant_isolation') then
        create policy tenant_isolation on third_party_services
            using (exists (select 1 from consent_scans cs
                           where cs.id = third_party_services.scan_id and cs.org_id = current_org_id()));
    end if;
    if not exists (select 1 from pg_policies where schemaname = current_schema() and tablename='policies' and policyname='tenant_isolation') then
        create policy tenant_isolation on policies
            using (exists (select 1 from consent_scans cs
                           where cs.id = policies.scan_id and cs.org_id = current_org_id()));
    end if;
    if not exists (select 1 from pg_policies where schemaname = current_schema() and tablename='consent_signals' and policyname='tenant_isolation') then
        create policy tenant_isolation on consent_signals
            using (exists (select 1 from consent_scans cs
                           where cs.id = consent_signals.scan_id and cs.org_id = current_org_id()));
    end if;
    if not exists (select 1 from pg_policies where schemaname = current_schema() and tablename='agent_runs' and policyname='tenant_isolation') then
        create policy tenant_isolation on agent_runs
            using (exists (select 1 from consent_scans cs
                           where cs.id = agent_runs.scan_id and cs.org_id = current_org_id()));
    end if;
    if not exists (select 1 from pg_policies where schemaname = current_schema() and tablename='consent_findings' and policyname='tenant_isolation') then
        create policy tenant_isolation on consent_findings
            using (exists (select 1 from consent_scans cs
                           where cs.id = consent_findings.scan_id and cs.org_id = current_org_id()));
    end if;
    if not exists (select 1 from pg_policies where schemaname = current_schema() and tablename='consent_recommendations' and policyname='tenant_isolation') then
        create policy tenant_isolation on consent_recommendations
            using (exists (select 1 from consent_findings f
                           join consent_scans cs on cs.id = f.scan_id
                           where f.id = consent_recommendations.finding_id and cs.org_id = current_org_id()));
    end if;
    if not exists (select 1 from pg_policies where schemaname = current_schema() and tablename='approvals' and policyname='tenant_isolation') then
        create policy tenant_isolation on approvals
            using (exists (select 1 from consent_findings f
                           join consent_scans cs on cs.id = f.scan_id
                           where f.id = approvals.finding_id and cs.org_id = current_org_id()));
    end if;
end $$;


-- Source: migrations/0002_lookup_provenance_consent_state.sql
-- Delta migration (docs/architecture, master-prompt hardening pass):
--   1. DB-backed cookie/tracker lookup table (Build Plan Component 3, Layer 1) +
--      purpose taxonomy table — replaces the hardcoded Python catalog as the source
--      of truth, per "data, not hardcoded dicts."
--   2. Classification provenance on cookies/trackers: lookup | rule | llm_interpretation
--      | human_confirmed (master prompt §4) — findings' human involvement is already
--      fully captured via the `approvals` table, so provenance is scoped to
--      cookie/tracker classification only, not duplicated onto consent_findings.
--   3. `consent_states` on cookies/trackers: which of the three scan passes
--      (pre_consent, post_accept, post_reject) each item was observed in — an array,
--      not a single state, since the same cookie can appear in more than one pass.
-- Additive only, per this project's migration convention.
-- Idempotent (safe to re-run): every statement is guarded with IF NOT EXISTS /
-- ON CONFLICT / a pg_policies check — re-running an already-applied migration was
-- previously a hard error (empirically reproduced: `relation "cookie_lookup" already
-- exists`), which is exactly the wrong behavior for retried deploys and local resets.

-- ── Cookie/tracker lookup (seeded from the Open Cookie Database) ───────────────
create table if not exists cookie_lookup (
    id                  uuid primary key default gen_random_uuid(),
    name_pattern        text not null,        -- exact name, or a prefix when is_prefix_pattern
    is_prefix_pattern   boolean not null default false,  -- from the source's "Wildcard match" flag
    domain_pattern      text,                 -- optional domain substring to disambiguate
    vendor              text,
    category            text not null check (category in ('analytics','marketing','functional','other')),
    source              text not null default 'open_cookie_database',
    raw_metadata        jsonb not null default '{}',  -- whatever extra columns the source dataset has
    created_at          timestamptz not null default now()
);
create index if not exists cookie_lookup_name_pattern_idx on cookie_lookup (name_pattern);

-- ── Purpose taxonomy (fixed category list owned by compliance, not hardcoded) ──
create table if not exists purpose_taxonomy (
    code         text primary key,
    label        text not null,
    description  text,
    created_at   timestamptz not null default now()
);
insert into purpose_taxonomy (code, label, description) values
    ('analytics', 'Analytics', 'Usage measurement and analytics tracking'),
    ('marketing', 'Marketing', 'Advertising, retargeting and marketing attribution'),
    ('functional', 'Functional', 'Site functionality, security or user-requested features'),
    ('other', 'Other', 'Does not fit the categories above, or purpose could not be determined')
on conflict (code) do nothing;

-- ── Provenance + consent-state tracking ─────────────────────────────────────────
alter table cookies
    add column if not exists source text not null default 'rule'
        check (source in ('lookup','rule','llm_interpretation','human_confirmed')),
    add column if not exists consent_states text[] not null default '{}';

alter table trackers
    add column if not exists source text not null default 'rule'
        check (source in ('lookup','rule','llm_interpretation','human_confirmed')),
    add column if not exists consent_states text[] not null default '{}';

-- `priority` (master prompt §8) is distinct from `risk_level`: risk_level is how
-- serious the issue is, priority is how urgently it should be worked relative to the
-- other findings in the same report.
alter table consent_findings
    add column if not exists priority text not null default 'medium' check (priority in ('low','medium','high'));

alter table purpose_taxonomy enable row level security;
alter table cookie_lookup enable row level security;
-- Both tables are shared reference data (not org-scoped), same rationale as
-- knowledge_documents/knowledge_chunks in 0001_init.sql — no tenant_isolation policy
-- needed; readable by any authenticated role via a permissive policy.
-- (CREATE POLICY has no IF NOT EXISTS in Postgres — guarded via pg_policies instead.)
do $$
begin
    if not exists (select 1 from pg_policies where schemaname = current_schema() and tablename = 'purpose_taxonomy' and policyname = 'read_all') then
        create policy read_all on purpose_taxonomy for select using (true);
    end if;
    if not exists (select 1 from pg_policies where schemaname = current_schema() and tablename = 'cookie_lookup' and policyname = 'read_all') then
        create policy read_all on cookie_lookup for select using (true);
    end if;
end $$;


-- Source: migrations/0003_agent_run_stages.sql
-- Stage-level timing/status for the demo UI + latency measurement (verification pass).
-- Keyed by scan_id (not agent_run_id): the pipeline the demo UI shows as one continuous
-- run actually spans two independently-retryable backend steps — scan (evidence
-- gathering) and analyze (agent reasoning) — see docs/architecture §D. scan_id is the
-- one identifier stable across both, known from the very first API call, so it's the
-- natural key for a unified timeline. agent_run_id is recorded too, nullable, for the
-- stages that happen during analysis, so a stage row can still be traced to the
-- specific agent_run that produced it.
create table if not exists agent_run_stages (
    id              uuid primary key default gen_random_uuid(),
    scan_id         uuid not null references consent_scans(id) on delete cascade,
    agent_run_id    uuid references agent_runs(id) on delete cascade,
    stage           text not null
                      check (stage in (
                          'url_validation','website_scan','data_structuring','classification',
                          'rules_check','rag_retrieval','llm_analysis','output_validation',
                          'findings_generated','audit_saved'
                      )),
    status          text not null default 'pending'
                      check (status in ('pending','running','completed','failed')),
    started_at      timestamptz,
    completed_at    timestamptz,
    duration_ms     integer,
    error           text,
    metadata        jsonb not null default '{}',
    created_at      timestamptz not null default now()
);
create index if not exists agent_run_stages_scan_id_idx on agent_run_stages (scan_id);
create index if not exists agent_run_stages_agent_run_id_idx on agent_run_stages (agent_run_id);

alter table agent_run_stages enable row level security;
do $$
begin
    if not exists (select 1 from pg_policies where schemaname = current_schema() and tablename='agent_run_stages' and policyname='tenant_isolation') then
        create policy tenant_isolation on agent_run_stages
            using (exists (select 1 from consent_scans cs
                           where cs.id = agent_run_stages.scan_id and cs.org_id = current_org_id()));
    end if;
end $$;


-- Source: migrations/0004_append_only_enforcement.sql
-- DB-level append-only enforcement for the audit trail (master reference §12 High).
--
-- audit_logs and approvals were append-only by CODING CONVENTION only: the repository
-- layer exposes no update/delete, and tests/test_review_bypass.py proves no app code
-- path mutates them -- but nothing stopped a direct UPDATE/DELETE issued outside the
-- app (a psql session, a compromised credential, a future ORM mistake). These triggers
-- make the guarantee a database property: any UPDATE or DELETE on either table raises,
-- regardless of which role issues it (triggers fire irrespective of RLS/role, unlike
-- the RLS policies in 0001 which the service-role connection bypasses).
--
-- Idempotent: CREATE OR REPLACE + DROP TRIGGER IF EXISTS, safe to re-run.
-- A true superuser can still ALTER TABLE ... DISABLE TRIGGER -- that action itself is
-- loud, privileged, and visible in pg_trigger, which is the point: tampering becomes
-- an explicit administrative act, never a quiet data edit.

create or replace function reject_append_only_mutation() returns trigger as $$
begin
    raise exception '% is append-only: % is not permitted (rows may only be inserted)',
        tg_table_name, tg_op;
end;
$$ language plpgsql;

drop trigger if exists audit_logs_append_only on audit_logs;
create trigger audit_logs_append_only
    before update or delete on audit_logs
    for each row execute function reject_append_only_mutation();

drop trigger if exists approvals_append_only on approvals;
create trigger approvals_append_only
    before update or delete on approvals
    for each row execute function reject_append_only_mutation();


-- Source: migrations/0005_action_module_and_monitoring.sql
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


-- Source: migrations/0006_rls_consistency_action_module_monitoring.sql
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


