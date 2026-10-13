-- "Push evidence" for Agent 3 (DSR Fulfillment): lets Consiva run the subject
-- search itself (it already holds the customer's database credentials) and POST
-- the matched records to the agent for analysis, instead of the agent connecting
-- to the customer's database directly (POST /requests/{id}/search, which fails
-- SOURCE_NOT_AUTHORIZED for every org with no registered dsr_source_authorizations
-- row). Mirrors ROPA's existing push path (POST /api/v1/ropa/evidence,
-- migrations 0007/0033/0034) and reuses its encryption approach (0031).
--
-- Additive only; idempotent (IF NOT EXISTS), same convention as every migration here.
-- Nothing here touches an Agent 1 (consent), Agent 2 (ropa discovery) or Agent 4
-- (breach) table.

-- ── dsr_search_runs: tell a pushed run apart from a connector-driven one ─────────
-- Mirrors ropa_discovery_runs.ingest_mode (0007/0033). A pushed run never has a
-- data_source_id (there may be no registered source at all -- that is the whole
-- point of this feature), so `ingest_mode` is what a reviewer or the audit trail
-- reads to tell "we searched it" from "Consiva searched it and sent us the result".
alter table dsr_search_runs add column if not exists ingest_mode text not null default 'connector' check (ingest_mode in ('connector', 'push'));

-- Idempotency for POST /requests/{id}/evidence: a retried push with the same
-- client_reference must return the SAME run, never open a second search run and
-- double-count evidence. NULL (every pre-existing and every connector-driven row)
-- is excluded from the unique index for the same reason 0011's
-- dsr_requests_idempotency_idx excludes NULL: many rows legitimately have no key,
-- and a plain unique(org_id, key) would make every one of those NULLs collide.
alter table dsr_search_runs add column if not exists idempotency_key text;

create unique index if not exists dsr_search_runs_idempotency_idx
    on dsr_search_runs (org_id, idempotency_key) where idempotency_key is not null;

-- ── dsr_evidence_pushes: the raw pushed package, encrypted at rest ───────────────
-- `dsr_evidence` (0011) already holds the MINIMIZED, classified projection every
-- DSR decision is built from -- exactly as it does for a connector-driven search,
-- unchanged by this migration ("apply the existing retention for stored evidence").
-- This table exists beside it for one reason only: a push carries whole matched
-- ROWS from Consiva's database (every returnable column, not just the subset a
-- finished plan ends up disclosing), and that raw package is bulk personal data
-- that deserves its own, tighter lifecycle -- encrypted at rest, and PURGEABLE
-- once the case closes, independent of dsr_evidence/dsr_actions, which the
-- engine must keep reading for as long as the case (and its audit trail) exists.
--
-- `payload_ciphertext` is Fernet-encrypted (app/agents/ropa/connectors/factory.py's
-- encrypt_credential/decrypt_credential, ROPA_CREDENTIAL_ENCRYPTION_KEY) JSON of
-- exactly the pushed `tables` array (columns, identifier_columns, record_key_columns,
-- rows, truncated) -- the bulk content. `subject` is stored in the clear because it
-- is no more sensitive than requester_email/requester_phone/requester_reference,
-- already plaintext on dsr_requests. Nothing here is ever written to a log --
-- app code logs only table_count/row_count/source_name/searched_at.
create table if not exists dsr_evidence_pushes (
    id                     uuid primary key default gen_random_uuid(),
    org_id                 uuid not null,
    request_id             uuid not null references dsr_requests(id) on delete cascade,
    search_run_id          uuid not null references dsr_search_runs(id) on delete cascade,
    integration_key_id     uuid references ropa_integration_keys(id),
    source_name            text not null,
    searched_at            timestamptz,
    subject                jsonb not null default '{}'::jsonb,
    -- NULL once purged. Never re-derivable afterwards -- a purge is a deletion.
    payload_ciphertext     text,
    table_count            integer not null default 0,
    row_count              integer not null default 0,
    truncated              boolean not null default false,
    -- Idempotency key Consiva supplied (`client_reference`). A second push with the
    -- same key, for the same org, returns this row rather than creating another.
    client_reference       text,
    purged_at              timestamptz,
    created_at             timestamptz not null default now()
);
create index if not exists dsr_evidence_pushes_org_id_idx on dsr_evidence_pushes (org_id);
create index if not exists dsr_evidence_pushes_request_idx on dsr_evidence_pushes (request_id);
create unique index if not exists dsr_evidence_pushes_client_ref_idx
    on dsr_evidence_pushes (org_id, client_reference) where client_reference is not null;

alter table dsr_evidence_pushes enable row level security;

do $$
begin
    if not exists (
        select 1 from pg_policies
        where schemaname = current_schema()
          and tablename = 'dsr_evidence_pushes'
          and policyname = 'tenant_isolation'
    ) then
        create policy tenant_isolation on dsr_evidence_pushes
            using (org_id = current_org_id());
    end if;
end $$;
