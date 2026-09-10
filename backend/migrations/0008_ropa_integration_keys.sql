-- Machine-to-machine credentials for external ROPA integrations.
--
-- Why this exists: an integration adapter running inside a CUSTOMER's own
-- infrastructure (e.g. PrepMyEvent's FastAPI backend on their VM) has no
-- Supabase user session, so it cannot use the human bearer-JWT path in
-- app/core/security.py. It needs a long-lived, revocable, org-scoped service
-- credential instead.
--
-- Storage model -- the key itself is NEVER stored:
--   * the full key is shown exactly ONCE, at creation
--   * only `key_hash` (SHA-256 of the full key) is persisted
--   * `key_prefix` is the short public identifier used for lookup and for
--     display in the UI ("csv_a1b2c3..."), so an admin can identify a key
--     without the secret existing anywhere in the database
-- A database dump therefore contains no usable credential, exactly like
-- ropa_data_sources.credential_ref.
--
-- SHA-256 (not a slow KDF) is correct here: these are 256-bit random tokens,
-- not user-chosen passwords, so there is no dictionary attack to slow down and
-- verification happens on every request.
--
-- Additive only; idempotent.

create table if not exists ropa_integration_keys (
    id            uuid primary key default gen_random_uuid(),
    org_id        uuid not null,
    name          text not null,                 -- human label, e.g. 'prepmyevent-adapter'
    key_prefix    text not null unique,          -- public lookup id; safe to display and log
    key_hash      text not null,                 -- SHA-256 of the full key -- never the key
    scopes        jsonb not null default '["evidence:write"]'::jsonb,
    enabled       boolean not null default true,
    last_used_at  timestamptz,
    expires_at    timestamptz,
    revoked_at    timestamptz,
    created_by_user_id uuid,
    created_at    timestamptz not null default now()
);
create index if not exists ropa_integration_keys_org_id_idx on ropa_integration_keys (org_id);
create index if not exists ropa_integration_keys_prefix_idx on ropa_integration_keys (key_prefix);

alter table ropa_integration_keys enable row level security;

do $$
begin
    if not exists (select 1 from pg_policies where schemaname = current_schema() and tablename = 'ropa_integration_keys' and policyname = 'tenant_isolation') then
        create policy tenant_isolation on ropa_integration_keys using (org_id = current_org_id());
    end if;
end $$;
