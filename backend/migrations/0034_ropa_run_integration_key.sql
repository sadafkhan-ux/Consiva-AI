-- Links a pushed ROPA run back to the integration key that pushed it.
--
-- Why this exists: POST /api/v1/ropa/evidence already authenticates the caller with a
-- RopaIntegrationKey (app/core/integration_auth.py) and already knows that key's
-- operator-chosen `name` (e.g. "ROPA integration (write+read)") at request time -- but
-- that name was only ever written to the audit log, never to the run itself. The run's
-- only other label is `source_name`, caller-supplied free text the external adapter
-- sends; in practice that is often left at a generic/boilerplate value, so two
-- genuinely different integrations can show up identically in the run list even though
-- each one's key has its own distinct name on the Integration Keys page.
--
-- This column lets the API join a pushed run back to the key that produced it and
-- surface that key's name alongside (or, when the adapter sent nothing usable, in
-- place of) the raw source_name -- without ever rewriting `source_name` itself, so no
-- historical row changes meaning.
--
-- Nullable: a connector-driven run (data_source_id set) has no integration key at all,
-- and an evidence-push run from before this migration has none recorded either.
-- `on delete set null` rather than restrict/cascade: revoking or (hypothetically)
-- deleting a key must never block deleting it, and a run is a historical record that
-- should outlive the credential that created it.
--
-- Additive only; idempotent.

alter table ropa_discovery_runs
    add column if not exists integration_key_id uuid references ropa_integration_keys(id) on delete set null;

create index if not exists ropa_discovery_runs_integration_key_id_idx
    on ropa_discovery_runs (integration_key_id);
