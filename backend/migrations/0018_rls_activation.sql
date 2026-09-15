-- Make row-level security actually do something.
--
-- THE PROBLEM THIS FIXES
-- ----------------------
-- A live sweep found that every RLS policy in this database was inert, and worse,
-- that turning them on would have taken the application down.
--
--   * `current_org_id()` was defined as `auth.jwt() ->> 'org_id'` -- a Supabase
--     function. This deployment moved to first-party auth in 0010, so the function
--     returned NULL for every request.
--   * The app connects as a superuser that also OWNS every table. Superusers bypass
--     RLS unconditionally; owners bypass it unless FORCE ROW LEVEL SECURITY is set,
--     and it was set on zero tables.
--   * So `select count(*) from incident_cases` with no org context returned every row
--     of every tenant.
--   * And the obvious hardening step -- run as a least-privilege role -- failed with
--     `permission denied for schema auth` on EVERY query, because the policies reached
--     into a schema that role cannot see.
--
-- 0001 predicted this in its own header: "Adjust `current_org_id()` once the real
-- auth/claims model is confirmed." This is that adjustment.
--
-- WHAT CHANGES
-- ------------
-- 1. `current_org_id()` reads a transaction-local GUC the application sets per
--    request (app/db/session.py). No dependency on the `auth` schema, so a restricted
--    role can evaluate it.
-- 2. FORCE ROW LEVEL SECURITY on every table that has a policy, so the owner is
--    subject to its own rules.
-- 3. A least-privilege role for the API to connect as. Created without LOGIN: a
--    password does not belong in a migration or in git. See the ops note at the end.
--
-- SAFE TO APPLY BEFORE THE APP MOVES OFF THE SUPERUSER. A superuser still bypasses
-- RLS, so nothing below changes behaviour until the connection role changes. That is
-- deliberate: schema first, cutover second, each verifiable on its own.
--
-- Idempotent throughout.

-- ── 1. current_org_id(), without the auth-schema dependency ─────────────────
-- `true` as the second argument to current_setting means "return NULL if unset"
-- rather than raising. NULL is the safe answer: `org_id = NULL` is never true, so an
-- unscoped connection sees nothing at all rather than seeing everything.
create or replace function current_org_id() returns uuid
language sql stable
as $$
  select nullif(current_setting('app.org_id', true), '')::uuid
$$;

-- ── 2. Make the owner obey its own policies ─────────────────────────────────
do $$
declare
    r record;
begin
    for r in
        select c.relname
          from pg_class c
          join pg_namespace n on n.oid = c.relnamespace
         where n.nspname = 'public'
           and c.relkind = 'r'
           and c.relrowsecurity          -- RLS already enabled
           and not c.relforcerowsecurity -- but not forced
    loop
        execute format('alter table public.%I force row level security', r.relname);
        raise notice 'forced RLS on %', r.relname;
    end loop;
end $$;

-- ── 3. The role the API should connect as ───────────────────────────────────
-- NOT the owner, so RLS applies to it by the ordinary rules; NOT a superuser, so it
-- cannot bypass them. NOLOGIN until an operator sets a password -- see below.
do $$
begin
    if not exists (select 1 from pg_roles where rolname = 'consiva_app') then
        create role consiva_app nologin;
        raise notice 'created role consiva_app (NOLOGIN until a password is set)';
    end if;
end $$;

grant usage on schema public to consiva_app;
grant select, insert, update, delete on all tables in schema public to consiva_app;
grant usage, select on all sequences in schema public to consiva_app;
grant execute on all functions in schema public to consiva_app;

-- Anything a later migration creates, too.
alter default privileges in schema public
    grant select, insert, update, delete on tables to consiva_app;
alter default privileges in schema public
    grant usage, select on sequences to consiva_app;
alter default privileges in schema public
    grant execute on functions to consiva_app;

-- The knowledge base is the shared regulatory corpus -- every tenant reads the same
-- DPDP text, and no tenant should be able to edit it.
revoke insert, update, delete on knowledge_documents, knowledge_chunks from consiva_app;

-- The migration ledger is the migration runner's, and the runner connects as the owner.
revoke insert, update, delete on schema_migrations from consiva_app;

-- ── 4. The bootstrap exemption, and why it is narrow ────────────────────────
--
-- Authentication has a chicken-and-egg problem: login looks a user up BY EMAIL and
-- key resolution looks an integration key up BY HASH, both before anyone knows which
-- organisation is involved. Under `org_id = current_org_id()` those lookups return
-- nothing on an unscoped connection, so nobody could ever log in.
--
-- So three tables get a SELECT-only policy that applies exactly when no scope is set,
-- which is exactly the pre-authentication moment. The instant a request is scoped,
-- `current_org_id()` is non-NULL, this policy stops matching, and the ordinary tenant
-- policy is the only one left.
--
-- SELECT only, deliberately. An unscoped connection can read a row to authenticate
-- against it; it cannot write one. And this does not weaken what RLS is here to
-- catch -- a repository function that forgets its org filter -- because every one of
-- those runs on a scoped connection, where this policy is inert.
do $$
begin
    if not exists (select 1 from pg_policies where tablename = 'users' and policyname = 'auth_bootstrap_read') then
        create policy auth_bootstrap_read on users
            for select using (current_org_id() is null);
    end if;

    if not exists (select 1 from pg_policies where tablename = 'organizations' and policyname = 'auth_bootstrap_read') then
        create policy auth_bootstrap_read on organizations
            for select using (current_org_id() is null);
    end if;

    if not exists (select 1 from pg_policies where tablename = 'ropa_integration_keys' and policyname = 'auth_bootstrap_read') then
        create policy auth_bootstrap_read on ropa_integration_keys
            for select using (current_org_id() is null);
    end if;
end $$;

-- `resolve_integration_key` stamps last_used_at on the row it just matched, which is
-- an UPDATE on a connection that is still unscoped at that instant. Allow it, bounded
-- to the unscoped moment in the same way.
do $$
begin
    if not exists (select 1 from pg_policies where tablename = 'ropa_integration_keys' and policyname = 'auth_bootstrap_touch') then
        create policy auth_bootstrap_touch on ropa_integration_keys
            for update using (current_org_id() is null) with check (current_org_id() is null);
    end if;
end $$;

-- ── OPS NOTE: completing the cutover ────────────────────────────────────────
--
-- This migration is inert on its own. To actually activate tenant isolation at the
-- database layer:
--
--   1. Give the role a password and a login:
--        alter role consiva_app login password '<generated>';
--
--   2. Point the API at it (backend/.env, or the `backend` service in compose):
--        DATABASE_URL=postgresql+asyncpg://consiva_app:<pw>@host:5432/consiva
--
--   3. LEAVE THE WORKER ON THE PRIVILEGED ROLE. It is deliberately cross-tenant --
--      the SLA sweeps (app/agents/{dsr,breach}/services/sla_service.py) and the
--      monitoring dispatcher walk every organisation's rows, and there is no single
--      org_id to scope them to. app/db/session.py sets the GUC from the request's
--      verified token; the worker has no request, so under a restricted role it would
--      correctly see nothing and silently stop sweeping.
--
--   4. Keep migrate.py on the owner role. consiva_app has no DDL rights, by design.
