-- 0049: a shared cache of policy analyses, keyed by the hash of the exact question the model was asked.
--
-- Why: the policy is read by an AI model, and the same text asked in two organisations (or two deployments) was answered separately, so one site could score 76 in one place and
-- 64 in another with nothing changed. A result was only ever reused inside one organisation (row-level security hides every other organisation's rows).
--
-- What it holds: the analysis of PUBLIC policy text only (the hash covers the instructions, the policy text, the requirement list, the sector reading and the model), so one organisation's
-- scan data is never stored here, and the row carries no organisation, scan or user id. Any change to the text, the requirements or the model gives a different hash, so a stale
-- answer is never served. Additive and idempotent: no existing table or column is touched.
create table if not exists policy_analysis_cache (
    input_hash  text primary key,
    result      jsonb not null,
    created_at  timestamptz not null default now()
);

do $$
begin
    if exists (select 1 from pg_roles where rolname = 'consiva_app') then
        grant select, insert on policy_analysis_cache to consiva_app;
    end if;
end $$;
