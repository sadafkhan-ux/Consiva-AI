-- Agent 2 (Data Discovery / ROPA) persistence: authorized external data sources,
-- discovery runs, versioned ROPA records, and risk/gap findings with a human-review
-- decision on each.
--
-- Additive only; idempotent (IF NOT EXISTS / guarded policy creation), same
-- convention as every other migration in this project. Nothing here touches any
-- Agent 1 / consent table.
--
-- CREDENTIAL HANDLING -- deliberate design, read before changing:
-- `ropa_data_sources` never stores a password, API key, or connection string. It
-- stores `credential_ref`, the NAME of the environment/secret-store entry that holds
-- the secret (e.g. 'PREPMYEVENT_DB_PASSWORD'). Consequences:
--   * a database dump contains no usable credential
--   * rotation is changing the secret in the environment, with no DB write
--   * the API can return a data source row without ever exposing a secret
--   * no encryption key has to be managed inside this schema
-- The non-secret half of the connection (host, port, dbname, user, sslmode / base_url)
-- lives in `config` so an administrator can see and edit it safely.

-- ── Authorized external data sources ─────────────────────────────────────────────
create table if not exists ropa_data_sources (
    id                    uuid primary key default gen_random_uuid(),
    org_id                uuid not null,
    name                  text not null,           -- logical source name, e.g. 'prepmyevent.com'
    connector             text not null,           -- 'postgres' | 'rest_api' (connectors/base.py registry key)
    source_type           text not null check (source_type in ('database', 'api', 'file', 'application')),
    config                jsonb not null default '{}'::jsonb,  -- NON-SECRET connection details only
    credential_ref        text,                    -- env/secret NAME holding the password or API key -- never the secret
    credential_rotated_at timestamptz,
    last_verified_at      timestamptz,             -- last successful connect + least-privilege check
    enabled               boolean not null default true,
    created_at            timestamptz not null default now(),
    updated_at            timestamptz not null default now(),
    unique (org_id, name)
);
create index if not exists ropa_data_sources_org_id_idx on ropa_data_sources (org_id);

-- ── One row per discovery + analysis run ─────────────────────────────────────────
-- `ingest_mode` records HOW evidence arrived: 'connector' means this backend
-- connected to the source itself; 'evidence_push' means an external integration
-- posted already-structured evidence and this backend never held source credentials
-- at all (the safer path for a third party like PrepMyEvent).
create table if not exists ropa_discovery_runs (
    id                     uuid primary key default gen_random_uuid(),
    org_id                 uuid not null,
    data_source_id         uuid references ropa_data_sources(id),
    source_name            text not null,
    ingest_mode            text not null default 'connector'
        check (ingest_mode in ('connector', 'evidence_push')),
    status                 text not null default 'pending'
        check (status in ('pending', 'discovering', 'analyzing', 'completed', 'failed')),
    tables_scanned         integer not null default 0,
    columns_scanned        integer not null default 0,
    personal_data_elements integer not null default 0,
    overall_confidence     numeric,
    -- Aggregate counts and the evidence-reference list only. Never raw row values:
    -- the whole pipeline is metadata-first and this column must not become the place
    -- personal data leaks into Consiva's own database.
    summary                jsonb not null default '{}'::jsonb,
    error                  text,
    requested_by_user_id   uuid,
    -- Caller-supplied key making a repeated submit return the SAME run instead of
    -- starting a duplicate one (idempotency requirement).
    idempotency_key        text,
    started_at             timestamptz,
    completed_at           timestamptz,
    created_at             timestamptz not null default now(),
    unique (org_id, idempotency_key)
);
create index if not exists ropa_discovery_runs_org_id_idx on ropa_discovery_runs (org_id);
create index if not exists ropa_discovery_runs_source_idx on ropa_discovery_runs (data_source_id);

-- ── Versioned ROPA records ───────────────────────────────────────────────────────
-- A ROPA is a living document: a new run never edits an approved record in place.
-- It writes a NEW version and marks the old one 'superseded' via supersedes_id, so
-- the full history stays reconstructable (same principle as scan_diffs for Agent 1).
create table if not exists ropa_records (
    id                  uuid primary key default gen_random_uuid(),
    org_id              uuid not null,
    discovery_run_id    uuid not null references ropa_discovery_runs(id),
    processing_activity text not null,
    version             integer not null default 1,
    status              text not null default 'draft'
        check (status in ('draft', 'in_review', 'approved', 'rejected', 'superseded')),
    payload             jsonb not null,          -- the full RopaRecord as produced by ropa_service
    edited_payload      jsonb,                   -- reviewer's edited version, when status='approved' after an edit
    confidence          numeric,
    review_required     boolean not null default true,
    supersedes_id       uuid references ropa_records(id),
    decided_by_user_id  uuid,
    decided_at          timestamptz,
    decision_reason     text,
    created_at          timestamptz not null default now(),
    updated_at          timestamptz not null default now(),
    unique (discovery_run_id, processing_activity, version)
);
create index if not exists ropa_records_org_id_idx on ropa_records (org_id);
create index if not exists ropa_records_run_idx on ropa_records (discovery_run_id);
create index if not exists ropa_records_status_idx on ropa_records (org_id, status);

-- ── Risk / gap findings, each independently reviewable ───────────────────────────
create table if not exists ropa_findings (
    id                 uuid primary key default gen_random_uuid(),
    org_id             uuid not null,
    discovery_run_id   uuid not null references ropa_discovery_runs(id),
    finding            text not null,
    gap_status         text not null,   -- 'Potential Gap' | 'Requires Review' | 'Evidence Incomplete' | 'Potential Privacy Risk'
    severity           text not null check (severity in ('low', 'medium', 'high', 'critical')),
    severity_factors   jsonb not null default '[]'::jsonb,  -- why this severity -- scoring must stay explainable
    related_evidence   jsonb not null default '[]'::jsonb,
    confidence         numeric,
    recommendation     text,
    review_status      text not null default 'pending'
        check (review_status in ('pending', 'approved', 'rejected', 'edited')),
    edited_payload     jsonb,
    decided_by_user_id uuid,
    decided_at         timestamptz,
    decision_reason    text,
    created_at         timestamptz not null default now()
);
create index if not exists ropa_findings_org_id_idx on ropa_findings (org_id);
create index if not exists ropa_findings_run_idx on ropa_findings (discovery_run_id);

-- ── Tenant isolation (matches every other org-scoped table; see 0006) ────────────
alter table ropa_data_sources    enable row level security;
alter table ropa_discovery_runs  enable row level security;
alter table ropa_records         enable row level security;
alter table ropa_findings        enable row level security;

do $$
begin
    if not exists (select 1 from pg_policies where schemaname = current_schema() and tablename = 'ropa_data_sources' and policyname = 'tenant_isolation') then
        create policy tenant_isolation on ropa_data_sources using (org_id = current_org_id());
    end if;
    if not exists (select 1 from pg_policies where schemaname = current_schema() and tablename = 'ropa_discovery_runs' and policyname = 'tenant_isolation') then
        create policy tenant_isolation on ropa_discovery_runs using (org_id = current_org_id());
    end if;
    if not exists (select 1 from pg_policies where schemaname = current_schema() and tablename = 'ropa_records' and policyname = 'tenant_isolation') then
        create policy tenant_isolation on ropa_records using (org_id = current_org_id());
    end if;
    if not exists (select 1 from pg_policies where schemaname = current_schema() and tablename = 'ropa_findings' and policyname = 'tenant_isolation') then
        create policy tenant_isolation on ropa_findings using (org_id = current_org_id());
    end if;
end $$;
