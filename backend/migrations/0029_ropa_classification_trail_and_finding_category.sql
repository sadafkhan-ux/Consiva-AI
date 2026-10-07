-- Persist the per-column classification evidence trail, and give findings a
-- machine-readable category.
--
-- WHY ropa_classifications EXISTS
-- --------------------------------
-- classify_evidence() computes one PersonalDataElement per discovered column --
-- category, confidence, which rule fired, the evidence trail, review reason --
-- every single run. Before this migration, NONE of that survived past the
-- in-memory RopaAgentOutput: persist_output() only ever wrote the grouped
-- ropa_records (processing-activity level) and risk_and_gap_findings. The
-- per-column "why was THIS column classified THIS way" answer -- the thing a
-- reviewer or an auditor actually asks -- existed for one function call and
-- was then gone. This table is the fix: one row per column, per run, written
-- alongside (never instead of) the existing records/findings tables.
--
-- WHY ropa_findings.category EXISTS
-- ----------------------------------
-- risk_service.detect_gaps() already produces exactly six distinct kinds of
-- finding (sensitive data, unknown classification, unknown purpose, missing
-- retention, missing owner, no vendor evidence) but had no machine-readable
-- label for which one a given row is -- only free-text `finding` prose. A
-- consumer (API client, future dashboard filter) had no way to group or filter
-- findings by kind without parsing English. `category` is nullable and
-- backfilled with NULL for any pre-existing row; nothing reads it as NOT NULL.
--
-- Additive only; idempotent (IF NOT EXISTS / guarded policy creation), same
-- convention as every other migration in this project.

create table if not exists ropa_classifications (
    id               uuid primary key default gen_random_uuid(),
    org_id           uuid not null,
    discovery_run_id uuid not null references ropa_discovery_runs(id),
    source_name      text not null,
    schema_name      text,
    table_name       text not null,
    column_name      text not null,
    classification   text not null,   -- category string, or an explicit "Unknown" / "Not Personal Data (...)" label
    data_subject     text not null default 'Unknown',
    confidence       numeric not null,
    -- The same evidence-citation list PersonalDataElement.evidence carries:
    -- local_id references, "method:<stage>" and rule ids -- never a raw value.
    evidence         jsonb not null default '[]'::jsonb,
    review_required  boolean not null default false,
    review_reason    text,
    created_at       timestamptz not null default now()
);
create index if not exists ropa_classifications_org_id_idx on ropa_classifications (org_id);
create index if not exists ropa_classifications_run_idx on ropa_classifications (discovery_run_id);

alter table ropa_classifications enable row level security;
alter table ropa_classifications force row level security;
do $$
begin
    if not exists (
        select 1 from pg_policies
        where schemaname = current_schema() and tablename = 'ropa_classifications' and policyname = 'tenant_isolation'
    ) then
        create policy tenant_isolation on ropa_classifications using (org_id = current_org_id());
    end if;
end $$;

alter table ropa_findings add column if not exists category text;
