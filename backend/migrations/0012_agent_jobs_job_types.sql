-- Widen `agent_jobs.job_type` to cover every agent that uses the shared queue.
--
-- 0001 created the constraint as check (job_type in ('scan','analyze')) when Agent 1
-- was the only thing queueing work. Two agents have been added since and neither
-- extended it, so the constraint has been silently rejecting their jobs:
--
--   * 'ropa_discovery'  -- Agent 2's connector-based discovery (app/jobs/worker.py).
--     Never noticed because the PrepMyEvent integration used the evidence_push path,
--     which completes inside the request and queues nothing.
--   * 'dsr_search' / 'dsr_execute' -- Agent 3, which queues both.
--
-- Found by enqueueing a real dsr_search against a live database: every attempt
-- failed with CheckViolationError, so neither DSR search nor DSR execution could
-- start at all. No unit test caught it because none of them insert into agent_jobs.
--
-- This is the one place Agent 3 had to change something Agent 1 owns. The change is
-- strictly widening -- every value that was legal before is still legal -- so no
-- existing row can violate it and no Agent 1 behaviour changes.
--
-- Idempotent: dropping by name with IF EXISTS and recreating, safe to re-run.

alter table agent_jobs drop constraint if exists agent_jobs_job_type_check;

alter table agent_jobs add constraint agent_jobs_job_type_check
    check (job_type in (
        -- Agent 1 (Consent)
        'scan',
        'analyze',
        -- Agent 2 (Data Discovery / ROPA)
        'ropa_discovery',
        -- Agent 3 (DSR Fulfillment)
        'dsr_search',
        'dsr_execute'
    ));
