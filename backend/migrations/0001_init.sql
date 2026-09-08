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
