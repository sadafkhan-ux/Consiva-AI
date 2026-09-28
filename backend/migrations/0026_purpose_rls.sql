-- 0026: row-level security on the Purpose Classifier's three tables.
--
-- WHAT 0025 GOT WRONG
--
-- It created purpose_runs, purpose_assessments and purpose_findings with an `org_id`
-- column and stopped there. Every other tenant table in this database has FORCE row
-- level security with a `org_id = current_org_id()` policy -- 47 of them, from
-- migration 0018 -- and these three were the only ones without. `consiva_app` could
-- SELECT all three with no policy consulted at all.
--
-- Not an active leak: every query in app/api/v1/routes/purpose.py and
-- app/services/purpose_run_service.py carries its own explicit `org_id` filter, and
-- that is what scopes the application today. But it is precisely the defence the other
-- 47 tables have and these did not, and the one that survives a future query that
-- forgets its filter. A compliance product holding one tenant's assessment of another
-- tenant's data behind nothing but developer discipline is the wrong default.
--
-- Found by tests/test_hardening_regressions.py::test_no_tenant_table_is_left_without_a
-- _policy, which reads the live database rather than any migration file. It named all
-- three by hand, on the first run after the purpose tables existed.
--
-- WHY THIS IS A NEW FILE RATHER THAN AN EDIT TO 0025
--
-- 0025 is already recorded in schema_migrations on every database that has it, so
-- migrate.py would never re-run it. Editing an applied migration changes the file and
-- not the database, which is worse than the gap it was meant to close: the schema and
-- the migration that claims to describe it silently disagree.

-- ── The policies ────────────────────────────────────────────────────────────
--
-- Guarded anyway, although `current_org_id()` is created by 0018 on this branch and
-- the guard should therefore never fire here. The alternative to a NOTICE is a
-- migration that errors halfway and leaves the schema partly applied. Raising a NOTICE is the honest outcome there --
-- silently creating the tables without isolation, and saying nothing, is the failure
-- this migration exists to correct.
--
-- FORCE matters as much as ENABLE. Without it the table OWNER bypasses its own policy,
-- and the owner is exactly the role this deployment still runs as (see
-- ALLOW_PRIVILEGED_DB_ROLE in docker-compose.prod.yml). FORCE is what makes the policy
-- mean something the day that role changes -- which is the point of setting it now
-- rather than as part of that cutover.
do $$
declare
    t text;
begin
    if to_regprocedure('current_org_id()') is null then
        raise notice
            'purpose tables: current_org_id() not present -- row-level security NOT '
            'enabled. Tenancy rests entirely on the explicit org_id filter in every '
            'query. Apply the RLS migration to close this.';
        return;
    end if;

    foreach t in array array['purpose_runs', 'purpose_assessments', 'purpose_findings']
    loop
        execute format('alter table %I enable row level security', t);
        execute format('alter table %I force row level security', t);

        -- Dropped and recreated rather than created-if-absent, so re-running this
        -- converges on the policy written here instead of leaving an older one in
        -- place that merely happens to share the name.
        execute format('drop policy if exists tenant_isolation on %I', t);
        execute format(
            'create policy tenant_isolation on %I using (org_id = current_org_id()) '
            'with check (org_id = current_org_id())', t);

        -- WITH CHECK as well as USING, deliberately. USING filters what a statement can
        -- READ; without WITH CHECK a tenant could INSERT a row stamped with somebody
        -- else's org_id and then be unable to see the row it had just written.
        if exists (select 1 from pg_roles where rolname = 'consiva_app') then
            execute format('grant select, insert, update on %I to consiva_app', t);
        end if;
    end loop;
end $$;
