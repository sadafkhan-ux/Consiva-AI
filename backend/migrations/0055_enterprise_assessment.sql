-- 0055: the Enterprise assessment layer: parameter assessments, scores, findings, analysis chunks, reports and the audit trail, plus analysis and extraction status per URL and per scan.
--
-- Additive only: new tables, new columns with defaults, new CHECKs on those new columns. Nothing in the 5-page agent's tables is touched.
-- Every table is tenant-scoped through its scan (ent_scans.org_id), like ent_frontier and ent_evidence (0050).
--
-- History is kept, never overwritten:
--   ent_assessments   a parameter's result is superseded by a newer one (a review, a re-analysis), not edited; one 'current' row per scan and parameter.
--   ent_score_results every calculation is a row; the latest is the current score, the rest are the score history.
--   ent_findings      keyed by a stable finding_key so the same finding in a later scan can be recognised (change detection).

-- ---- per-scan and per-URL status ------------------------------------------------------------------------------------------------------------------------------------

alter table ent_scans
    add column if not exists analysis_status text not null default 'not_started',
    add column if not exists analysis_started_at timestamptz,
    add column if not exists analysis_completed_at timestamptz,
    add column if not exists analysis_error text,
    add column if not exists previous_scan_id uuid references ent_scans(id) on delete set null;

alter table ent_scans
    drop constraint if exists ent_scans_analysis_status_check;
alter table ent_scans
    add constraint ent_scans_analysis_status_check check (analysis_status in ('not_started', 'running', 'completed', 'partial', 'failed'));

alter table ent_frontier
    add column if not exists extraction_status text,
    add column if not exists extraction_method text,
    add column if not exists analysis_status text not null default 'not_started';

alter table ent_frontier
    drop constraint if exists ent_frontier_extraction_status_check;
alter table ent_frontier
    add constraint ent_frontier_extraction_status_check
    check (extraction_status is null or extraction_status in ('extracted', 'ocr_extracted', 'partly_extracted', 'unreadable', 'encrypted', 'unsupported', 'too_large'));

alter table ent_frontier
    drop constraint if exists ent_frontier_analysis_status_check;
alter table ent_frontier
    add constraint ent_frontier_analysis_status_check check (analysis_status in ('not_started', 'done', 'failed', 'not_applicable'));

-- ---- assessments ----------------------------------------------------------------------------------------------------------------------------------------------------

create table if not exists ent_assessments (
    id                 uuid primary key default gen_random_uuid(),
    scan_id            uuid not null references ent_scans(id) on delete cascade,
    parameter_id       text not null,
    state              text not null check (state in ('met', 'partly_met', 'not_met', 'not_assessed', 'not_applicable', 'needs_records', 'human_review')),
    evidence_refs      jsonb not null default '[]'::jsonb,
    confidence         text check (confidence is null or confidence in ('high', 'medium', 'low')),
    note               text not null default '',
    assessor           text not null,               -- 'rule:technical', 'rule:consent', 'llm', 'review'
    provider           text,                        -- the model provider, for an llm assessor
    model              text,
    reviewed_by        text,
    review_reason      text,
    catalogue_version  text not null,
    status             text not null default 'current' check (status in ('current', 'superseded')),
    created_at         timestamptz not null default now(),
    constraint ent_assessments_review_complete check (assessor <> 'review' or (reviewed_by is not null and review_reason is not null))
);
create unique index if not exists ent_assessments_one_current on ent_assessments (scan_id, parameter_id) where status = 'current';
create index if not exists ent_assessments_scan_idx on ent_assessments (scan_id, parameter_id, created_at);

-- ---- scores ---------------------------------------------------------------------------------------------------------------------------------------------------------

create table if not exists ent_score_results (
    id                     uuid primary key default gen_random_uuid(),
    scan_id                uuid not null references ent_scans(id) on delete cascade,
    version                text not null,
    catalogue_fingerprint  text not null,
    as_of                  date not null,
    score                  integer check (score is null or (score >= 0 and score <= 100)),
    coverage               integer not null check (coverage >= 0 and coverage <= 100),
    basis                  text not null,
    result                 jsonb not null,
    created_at             timestamptz not null default now()
);
create index if not exists ent_score_results_scan_idx on ent_score_results (scan_id, created_at desc);

-- ---- findings -------------------------------------------------------------------------------------------------------------------------------------------------------

create table if not exists ent_findings (
    id                   uuid primary key default gen_random_uuid(),
    scan_id              uuid not null references ent_scans(id) on delete cascade,
    finding_key          text not null,               -- stable across scans: parameter id plus what it is about
    parameter_id         text not null,
    title                text not null,
    description          text not null,
    severity             text not null check (severity in ('high', 'medium', 'low', 'info')),
    state                text not null,               -- the parameter state this finding comes from
    legal_refs           jsonb not null default '[]'::jsonb,
    affected_urls        jsonb not null default '[]'::jsonb,
    affected_count       integer not null default 0 check (affected_count >= 0),
    evidence_refs        jsonb not null default '[]'::jsonb,
    expected             text not null default '',
    observed             text not null default '',
    reason               text not null default '',
    confidence           text check (confidence is null or confidence in ('high', 'medium', 'low')),
    recommendation       text not null default '',
    human_review         boolean not null default false,
    verification_status  text not null default 'unverified' check (verification_status in ('unverified', 'verified', 'disputed')),
    schedule_item        integer,
    penalty_ceiling_cr   integer,
    created_at           timestamptz not null default now(),
    constraint ent_findings_unique unique (scan_id, finding_key)
);
create index if not exists ent_findings_scan_idx on ent_findings (scan_id, severity);

-- ---- analysis chunks (the AI pipeline's record of what was processed) ------------------------------------------------------------------------------------------------

create table if not exists ent_analysis_chunks (
    id            uuid primary key default gen_random_uuid(),
    scan_id       uuid not null references ent_scans(id) on delete cascade,
    evidence_id   uuid references ent_evidence(id) on delete set null,
    source_url    text not null,
    chunk_index   integer not null default 0 check (chunk_index >= 0),
    chunk_count   integer not null default 1 check (chunk_count >= 1),
    level         text not null check (level in ('page', 'category', 'cross_page', 'mapping', 'final')),
    status        text not null default 'pending' check (status in ('pending', 'done', 'failed', 'skipped')),
    provider      text,
    model         text,
    input_chars   integer,
    attempts      integer not null default 0 check (attempts >= 0),
    error         text,
    result        jsonb,
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now(),
    constraint ent_analysis_chunks_unique unique (scan_id, level, source_url, chunk_index)
);
create index if not exists ent_analysis_chunks_scan_idx on ent_analysis_chunks (scan_id, level, status);

-- ---- reports --------------------------------------------------------------------------------------------------------------------------------------------------------

create table if not exists ent_reports (
    id               uuid primary key default gen_random_uuid(),
    scan_id          uuid not null references ent_scans(id) on delete cascade,
    version          integer not null check (version >= 1),
    score_result_id  uuid references ent_score_results(id) on delete set null,
    status           text not null default 'ready' check (status in ('ready', 'failed')),
    pdf              bytea,
    sha256           text,
    byte_size        integer,
    page_count       integer,
    error            text,
    generated_at     timestamptz not null default now(),
    constraint ent_reports_unique unique (scan_id, version)
);

-- ---- audit trail ----------------------------------------------------------------------------------------------------------------------------------------------------

create table if not exists ent_audit_events (
    id         uuid primary key default gen_random_uuid(),
    scan_id    uuid not null references ent_scans(id) on delete cascade,
    actor      text not null,
    event      text not null,
    detail     jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now()
);
create index if not exists ent_audit_events_scan_idx on ent_audit_events (scan_id, created_at);

-- ---- tenant isolation: the same pattern as ent_frontier and ent_evidence in 0050 --------------------------------------------------------------------------------------

alter table ent_assessments enable row level security;
alter table ent_assessments force row level security;
alter table ent_score_results enable row level security;
alter table ent_score_results force row level security;
alter table ent_findings enable row level security;
alter table ent_findings force row level security;
alter table ent_analysis_chunks enable row level security;
alter table ent_analysis_chunks force row level security;
alter table ent_reports enable row level security;
alter table ent_reports force row level security;
alter table ent_audit_events enable row level security;
alter table ent_audit_events force row level security;

do $$
declare t text;
begin
    if to_regprocedure('current_org_id()') is null then
        raise notice 'ent_*: current_org_id() not present -- row-level security policies NOT created for the assessment tables. Tenancy rests on the scan lookup in every query.';
        return;
    end if;
    foreach t in array array['ent_assessments', 'ent_score_results', 'ent_findings', 'ent_analysis_chunks', 'ent_reports', 'ent_audit_events']
    loop
        if not exists (select 1 from pg_policies where schemaname = current_schema() and tablename = t and policyname = 'tenant_isolation') then
            execute format(
                'create policy tenant_isolation on %I using (exists (select 1 from ent_scans s where s.id = %I.scan_id and s.org_id = current_org_id()))', t, t);
        end if;
    end loop;
    if exists (select 1 from pg_roles where rolname = 'consiva_app') then
        grant select, insert, update on ent_assessments, ent_score_results, ent_findings, ent_analysis_chunks, ent_reports, ent_audit_events to consiva_app;
    end if;
end $$;
