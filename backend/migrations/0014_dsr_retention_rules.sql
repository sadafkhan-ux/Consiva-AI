-- Retention rules as configuration rather than code (blueprint §24, phase 12).
--
-- The constraint engine has always applied retention rules correctly -- it blocks an
-- erasure, names the authority, and tells the requester the date the rule lifts. What
-- it had no way to do was LOAD them: a rule had to be passed in by a caller, so in
-- practice only a test ever supplied one and no real deployment could express "keep
-- invoices for seven years".
--
-- WHY THIS IS CONFIGURATION AND NOT LOGIC
-- Consiva does not decide how long an organisation must keep a record. That is the
-- organisation's own obligation, from their own legal advice, and the `authority`
-- column records whose rule it is so a reviewer reading a blocked deletion -- and the
-- requester reading the response -- can see the source of the decision. There is
-- deliberately no column asserting what any law requires.

create table if not exists dsr_retention_rules (
    id                     uuid primary key default gen_random_uuid(),
    org_id                 uuid not null,
    -- Scope. A rule applies to one table on one authorized source; NULL data_source_id
    -- means "every source in this org that has this table", which is how an
    -- organisation-wide policy ("all invoices, wherever they live") is expressed.
    data_source_id         uuid references ropa_data_sources(id),
    table_name             text not null,
    -- The column holding the date the retention period counts from.
    date_column            text not null,
    -- How long records must be kept, in days. Stored as an integer rather than an
    -- interval so the value a reviewer configured is exactly the value applied --
    -- no calendar arithmetic hiding inside the type.
    retention_days         integer not null check (retention_days > 0),
    -- Whose rule this is. Shown to reviewers AND to the data principal, because why
    -- an erasure was refused on policy grounds is what they are entitled to know.
    authority              text not null,
    -- Which operations it blocks. Deletion by default; an organisation may also wish
    -- to prevent a field being overwritten while a record is under retention.
    applies_to_operations  jsonb not null default '["delete_record"]'::jsonb,
    enabled                boolean not null default true,
    notes                  text,
    created_by_user_id     uuid,
    created_at             timestamptz not null default now(),
    updated_at             timestamptz not null default now(),
    unique (org_id, data_source_id, table_name, date_column)
);
create index if not exists dsr_retention_rules_org_idx on dsr_retention_rules (org_id);
create index if not exists dsr_retention_rules_lookup_idx
    on dsr_retention_rules (org_id, table_name) where enabled = true;

alter table dsr_retention_rules enable row level security;

do $$
begin
    if not exists (
        select 1 from pg_policies
        where schemaname = current_schema()
          and tablename = 'dsr_retention_rules'
          and policyname = 'tenant_isolation'
    ) then
        create policy tenant_isolation on dsr_retention_rules
            using (org_id = current_org_id());
    end if;
end $$;
