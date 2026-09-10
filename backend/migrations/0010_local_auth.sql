-- First-party authentication: organizations and users owned by THIS database
-- rather than by Supabase.
--
-- Background: migration 0006-era code got both its database and its identity
-- provider from Supabase. 0007's predecessor moved the database to local
-- Postgres but left auth on Supabase, so the app still could not boot without a
-- live Supabase project. These two tables close that gap.
--
-- `org_id` has been an unconstrained UUID everywhere in this schema on purpose
-- (see app/db/models.py's module docstring: "tenancy/organization management is
-- a platform-level concern ... An `organizations` table is assumed to exist (or
-- be added) elsewhere"). This migration is that "elsewhere". Existing org_id
-- columns are deliberately NOT converted to foreign keys here: rows already
-- reference org ids that predate this table, and adding an FK would either fail
-- or require inventing organization rows to satisfy it. The FK can be added
-- later, once every historical org_id has a real row.
--
-- Password storage: `password_hash` holds a bcrypt hash (see app/core/passwords.py).
-- No plaintext, no reversible encryption, and the column is never returned by
-- any API response model.
--
-- Additive only; idempotent.

create table if not exists organizations (
    id          uuid primary key default gen_random_uuid(),
    name        text not null,
    -- Optional short handle for URLs/support ("swaransoft"), not used for auth.
    slug        text unique,
    is_active   boolean not null default true,
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now()
);

create table if not exists users (
    id              uuid primary key default gen_random_uuid(),
    org_id          uuid not null references organizations(id),
    -- Stored lowercased by the application so logins are case-insensitive; the
    -- unique index below then genuinely prevents duplicate accounts.
    email           text not null,
    password_hash   text not null,
    full_name       text,
    -- 'admin' may create other users; 'member' may not. Deliberately just two
    -- roles -- a permission matrix nobody needs yet is worse than none.
    role            text not null default 'member' check (role in ('admin', 'member')),
    is_active       boolean not null default true,
    last_login_at   timestamptz,
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now()
);
create unique index if not exists users_email_key on users (lower(email));
create index if not exists users_org_id_idx on users (org_id);

alter table organizations enable row level security;
alter table users         enable row level security;

do $$
begin
    -- organizations is keyed by `id`, not `org_id`, so its tenant predicate
    -- compares the primary key.
    if not exists (select 1 from pg_policies where schemaname = current_schema() and tablename = 'organizations' and policyname = 'tenant_isolation') then
        create policy tenant_isolation on organizations using (id = current_org_id());
    end if;
    if not exists (select 1 from pg_policies where schemaname = current_schema() and tablename = 'users' and policyname = 'tenant_isolation') then
        create policy tenant_isolation on users using (org_id = current_org_id());
    end if;
end $$;
