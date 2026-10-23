-- 0039: record the Consent Agent's privacy-policy content analysis as a pipeline stage.
--
-- app/policy reads what a site's privacy and cookie policies say (with exact, verified quotations) beside the graph's reading of what the
-- site does. It runs as its own step before the graph and records itself as 'policy_analysis', with its result in the stage metadata.
-- The step is fail-soft: it always completes its stage (an unavailable result is in the metadata), because the console reads a FAILED
-- stage outside the analysis stages as the whole run failing.
--
-- Widening the CHECK, exactly as 0021 and 0028 did, rather than assuming the insert would work: a stage name the constraint does not list
-- is rejected, and track_stage swallows that error, so the step would run and leave no record at all (tests/test_stage_names_match_schema.py
-- exists to catch precisely that).
--
-- Additive. Widening a CHECK cannot invalidate a row that already satisfies it, so there is nothing to backfill and no existing stage name
-- changes meaning.
alter table agent_run_stages
    drop constraint if exists agent_run_stages_stage_check;

alter table agent_run_stages
    add constraint agent_run_stages_stage_check
    check (stage in (
        'url_validation','website_scan','data_structuring','classification',
        'normalize_evidence',
        'policy_analysis',
        'rules_check','rag_retrieval','llm_analysis','output_validation',
        'findings_generated','rule_findings_generated',
        'human_review_gate',
        'audit_saved'
    ));
