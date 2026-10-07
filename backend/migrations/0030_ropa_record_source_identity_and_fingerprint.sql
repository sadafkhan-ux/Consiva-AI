-- Fixes two confirmed defects in ROPA record versioning:
--
-- 1. VERSION IDENTITY COLLISION ACROSS SOURCES.
--    `_latest_version` (ropa_repository.py) keyed a record's version chain on
--    (org_id, processing_activity) ONLY. Two different sources in the same
--    org that both produce an activity named e.g. "Marketing Communications"
--    shared one version chain: a run against source B would supersede an
--    approved record that actually belonged to source A. `ropa_records` had
--    no source_name column at all to filter on. This migration adds one,
--    backfilled from the owning discovery run, and the repository layer
--    (separate code change) now scopes every version lookup by it.
--
-- 2. MEANINGLESS VERSION CHURN.
--    Re-pushing identical evidence created a new version every time, because
--    nothing compared the new record's substance against the previous one.
--    `content_hash` is a fingerprint over the record's semantically
--    meaningful fields (NOT run-specific evidence ids, which legitimately
--    differ on every run even when nothing changed) -- the repository layer
--    skips minting a new version when the fingerprint is unchanged.
--
-- Additive only; idempotent (IF NOT EXISTS / guarded backfill).

alter table ropa_records add column if not exists source_name text;
alter table ropa_records add column if not exists content_hash text;

-- Backfill existing rows from the run they belong to, so the NOT NULL below
-- never fails against data that predates this migration.
update ropa_records r
set source_name = d.source_name
from ropa_discovery_runs d
where r.discovery_run_id = d.id
  and r.source_name is null;

-- Anything still null (an orphaned/corrupt discovery_run_id, which the FK
-- should already prevent) gets an explicit placeholder rather than blocking
-- the NOT NULL constraint on a row nothing else can explain either.
update ropa_records set source_name = 'unknown_source' where source_name is null;

alter table ropa_records alter column source_name set not null;

create index if not exists ropa_records_source_activity_idx
    on ropa_records (org_id, source_name, processing_activity, status);
