-- Add Agent 4's job type to `agent_jobs.job_type`.
--
-- 0012 widened this constraint for Agents 2 and 3 after discovering it had been
-- silently rejecting their jobs since 0001. Agent 4 queues `incident_analysis`, so it
-- needs the same treatment -- and it needs it BEFORE the first incident is analysed
-- rather than after, which is the whole point of writing this migration alongside the
-- worker branch instead of waiting for a CheckViolationError in production.
--
-- Strictly widening: every value legal before is still legal, so no existing row can
-- violate it and no other agent's behaviour changes.
--
-- Note what is NOT here. There is no `incident_contain` or `incident_execute` job
-- type, and there will not be one: containment is performed by a person and attested
-- to, so there is nothing for a worker to carry out. A queued job that claimed to
-- disable an account would be an action Consiva cannot actually take.
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
        'dsr_execute',
        -- Agent 4 (Breach Response) -- analysis only; see note above.
        'incident_analysis'
    ));
