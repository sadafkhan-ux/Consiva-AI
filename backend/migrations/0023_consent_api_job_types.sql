-- 0023: let the queue hold the two job types the integration API needs, and let a job
-- be cancelled.
--
-- Both are CHECK constraints that enumerate values, and both were caught the same way
-- -- by an endpoint returning 500 on its first real call, not by reading the schema:
--
--   agent_jobs_job_type_check  rejected 'consent_api_chain'
--   agent_jobs_status_check    has no 'cancelled', so cancel_jobs_for_scan could not
--                              mark a queued job as anything other than failed
--
-- This is the same trap migration 0021 fixed on agent_run_stages.stage: an enumerated
-- CHECK is invisible to the Python that writes into it, so a new value type-checks,
-- passes review, passes unit tests that never touch a database, and fails at runtime.
-- tests/test_stage_names_match_schema.py holds that agreement for stage names;
-- tests/test_job_types_match_schema.py now does the same for job types.
--
-- WHAT THE TWO NEW TYPES ARE
--
--   consent_api_chain  one API-requested scan end to end: crawl, then analysis, then
--                      the completion callback. The console keeps using the separate
--                      'scan' and 'analyze' types, which still exist and are unchanged
--                      -- a person decides whether to analyse what a crawl found, an
--                      integrator posts a URL and polls one id.
--
--   consent_webhook    one delivery attempt to a caller-supplied endpoint. A separate
--                      job so the existing attempts/run_after backoff provides retries,
--                      and so a dead receiver cannot hold a worker slot inside the scan
--                      job or turn a successful scan into a failed one.
--
-- Additive: widening a CHECK cannot invalidate a row that already satisfies it.
alter table agent_jobs
    drop constraint if exists agent_jobs_job_type_check;

alter table agent_jobs
    add constraint agent_jobs_job_type_check
    check (job_type in (
        'scan', 'analyze', 'ropa_discovery', 'dsr_search', 'dsr_execute',
        'incident_analysis', 'regwatch_collect', 'regwatch_assess',
        'consent_api_chain', 'consent_webhook'
    ));

-- 'cancelled' is deliberately distinct from 'failed'. A cancelled job was stopped on
-- purpose and nothing is wrong; recording it as failed would put it in the same bucket
-- as genuine errors on every dashboard and alert that counts failures.
alter table agent_jobs
    drop constraint if exists agent_jobs_status_check;

alter table agent_jobs
    add constraint agent_jobs_status_check
    check (status in ('queued', 'running', 'done', 'failed', 'cancelled'));
