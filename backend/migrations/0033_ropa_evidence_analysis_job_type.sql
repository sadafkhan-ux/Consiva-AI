-- 0033: let the queue hold 'ropa_evidence_analysis'.
--
-- Evidence-push (POST /api/v1/ropa/evidence) used to run its whole analysis pipeline
-- inline inside the request -- safe when it was pure and offline (discovery_service.
-- run_pipeline's own docstring: "no I/O, no network, no database"), until LLM
-- enrichment was wired into that path and gave it a real network call. A slow or
-- unavailable self-hosted model then hung the endpoint for minutes, well past any
-- caller's timeout (an observed incident, not a hypothetical). The fix queues
-- evidence-push through this same agent_jobs table, the way connector-based discovery
-- (job_type 'ropa_discovery') already is -- for the identical reason.
--
-- agent_jobs.job_type is an enumerated CHECK, invisible to the Python that writes into
-- it: a new value type-checks, passes review, passes every test that never opens a
-- database, and fails only at runtime with CheckViolationError. That trap is exactly
-- what tests/test_job_types_match_schema.py exists to catch, and it caught this one --
-- this migration is that test's fix, not a response to a runtime failure.
--
-- Rebuilt from the full list rather than appended to, same reason 0023/0025 did: an
-- ALTER that assumed this file's prior version was the live constraint would silently
-- narrow it and break every other agent's queue writes.
--
-- Additive: widening a CHECK cannot invalidate a row that already satisfies it.
alter table agent_jobs
    drop constraint if exists agent_jobs_job_type_check;

alter table agent_jobs
    add constraint agent_jobs_job_type_check
    check (job_type in (
        'scan', 'analyze', 'ropa_discovery', 'ropa_evidence_analysis', 'dsr_search', 'dsr_execute',
        'incident_analysis', 'regwatch_collect', 'regwatch_assess',
        'consent_api_chain', 'consent_webhook',
        'purpose_assessment'
    ));
