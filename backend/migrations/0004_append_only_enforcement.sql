-- DB-level append-only enforcement for the audit trail (master reference §12 High).
--
-- audit_logs and approvals were append-only by CODING CONVENTION only: the repository
-- layer exposes no update/delete, and tests/test_review_bypass.py proves no app code
-- path mutates them -- but nothing stopped a direct UPDATE/DELETE issued outside the
-- app (a psql session, a compromised credential, a future ORM mistake). These triggers
-- make the guarantee a database property: any UPDATE or DELETE on either table raises,
-- regardless of which role issues it (triggers fire irrespective of RLS/role, unlike
-- the RLS policies in 0001 which the service-role connection bypasses).
--
-- Idempotent: CREATE OR REPLACE + DROP TRIGGER IF EXISTS, safe to re-run.
-- A true superuser can still ALTER TABLE ... DISABLE TRIGGER -- that action itself is
-- loud, privileged, and visible in pg_trigger, which is the point: tampering becomes
-- an explicit administrative act, never a quiet data edit.

create or replace function reject_append_only_mutation() returns trigger as $$
begin
    raise exception '% is append-only: % is not permitted (rows may only be inserted)',
        tg_table_name, tg_op;
end;
$$ language plpgsql;

drop trigger if exists audit_logs_append_only on audit_logs;
create trigger audit_logs_append_only
    before update or delete on audit_logs
    for each row execute function reject_append_only_mutation();

drop trigger if exists approvals_append_only on approvals;
create trigger approvals_append_only
    before update or delete on approvals
    for each row execute function reject_append_only_mutation();
