-- 0024: a scan can be cancelled.
--
-- The third enumerated CHECK this integration work has had to widen, and the third to
-- be found the same way -- by an endpoint returning 500 on its first real call:
--
--   0021  agent_run_stages.stage  rejected 'rule_findings_generated'
--   0023  agent_jobs.job_type     rejected 'consent_api_chain'
--   0024  consent_scans.status    rejects 'cancelled'  (this one)
--
-- The pattern is worth naming once: an enumerated CHECK is invisible to the Python
-- that writes into it. A new value is a plain string, so it type-checks, reads fine in
-- review, and passes every unit test that never opens a database. It fails only at
-- runtime. tests/test_status_values_match_schema.py now holds this agreement for status
-- columns the way test_stage_names_match_schema.py does for stage names and
-- test_job_types_match_schema.py for job types.
--
-- 'cancelled' is deliberately NOT 'failed'. A cancelled scan was stopped on purpose and
-- nothing went wrong; folding the two together would put deliberate stops in the same
-- bucket as real errors on every count, dashboard and alert -- and would tell a caller
-- polling the API that their scan broke when they are the one who stopped it.
--
-- Additive: widening a CHECK cannot invalidate a row that already satisfies it, and no
-- existing row can be 'cancelled' because nothing could write it until now.
alter table consent_scans
    drop constraint if exists consent_scans_status_check;

alter table consent_scans
    add constraint consent_scans_status_check
    check (status in ('pending', 'running', 'completed', 'failed', 'cancelled'));
