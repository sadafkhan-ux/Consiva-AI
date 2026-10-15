-- DSR (Agent 3): let an administrator store a source's WRITE credential once, encrypted,
-- instead of naming an environment variable that someone must set on the server and
-- redeploy for every write-enabled source.
--
-- Same mechanism and the same Fernet key (ROPA_CREDENTIAL_ENCRYPTION_KEY) the READ
-- credential already uses (migration 0031 on ropa_data_sources): ciphertext only, never
-- returned by any API, never logged. `write_credential_ref` stays as the fallback, so an
-- existing source configured with an environment entry keeps working unchanged.
--
-- Additive and idempotent: one nullable column, no data change, no default. Existing
-- rows keep NULL and behave exactly as before. The table already has row-level security
-- (migration 0011); adding a column does not alter its policies.

alter table dsr_source_authorizations add column if not exists write_credential_ciphertext text;

comment on column dsr_source_authorizations.write_credential_ciphertext is
    'Fernet ciphertext of the source''s write password. Write-only through PUT /api/v1/dsr/config/sources; never returned.';
