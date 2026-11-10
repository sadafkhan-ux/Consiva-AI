-- 0048: report approvals. A DPDP scan report is released to a customer only after a named person has approved it.
--
-- One row per scan: who approved it, when, against which scorecard version, and an optional note. A later change to the scoring rules makes an approval stale
-- (it was given for another version), and a reviewer can withdraw an approval (revoked_at / revoked_by). Additive: no existing table or column is touched, and the
-- download only asks for an approval when REPORT_REQUIRE_APPROVAL is switched on. Idempotent.
create table if not exists report_approvals (
    scan_id              uuid primary key references consent_scans(id) on delete cascade,
    approved_by_user_id  uuid not null,
    approved_at          timestamptz not null default now(),
    report_version       text not null,
    note                 text,
    revoked_at           timestamptz,
    revoked_by_user_id   uuid,
    constraint report_approvals_revoke_pair check ((revoked_at is null) = (revoked_by_user_id is null))
);

alter table report_approvals enable row level security;
alter table report_approvals force row level security;
do $$
begin
    if to_regprocedure('current_org_id()') is null then
        raise notice 'report_approvals: current_org_id() not present -- row-level security policy NOT created. Tenancy rests on the scan lookup in every query.';
        return;
    end if;
    if not exists (select 1 from pg_policies where schemaname = current_schema() and tablename = 'report_approvals' and policyname = 'tenant_isolation') then
        create policy tenant_isolation on report_approvals
            using (exists (select 1 from consent_scans cs where cs.id = report_approvals.scan_id and cs.org_id = current_org_id()));
    end if;
    if exists (select 1 from pg_roles where rolname = 'consiva_app') then
        grant select, insert, update, delete on report_approvals to consiva_app;
    end if;
end $$;
