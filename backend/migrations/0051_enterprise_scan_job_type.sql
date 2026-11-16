-- 0051: allow the 'enterprise_scan' job type.
--
-- The Enterprise full-site crawl runs as a background job (app/enterprise/jobs.py). The queue's CHECK on job_type lists every job type, so the new one has to be added to it.
-- Additive: widening a CHECK cannot invalidate a row that already satisfies it. Same pattern as 0044.
alter table agent_jobs
    drop constraint if exists agent_jobs_job_type_check;

alter table agent_jobs
    add constraint agent_jobs_job_type_check
    check (job_type in (
        'scan', 'analyze', 'ropa_discovery', 'ropa_evidence_analysis', 'dsr_search', 'dsr_execute',
        'incident_analysis', 'regwatch_collect', 'regwatch_assess',
        'consent_api_chain', 'consent_webhook',
        'purpose_assessment',
        'render_reports',
        'enterprise_scan'
    ));
