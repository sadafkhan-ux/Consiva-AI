-- 0021: let the fallback record itself.
--
-- `create_rule_findings` writes its stage as 'rule_findings_generated'. The CHECK
-- from 0003 lists ten stage names and that is not one of them, so the insert was
-- rejected every time the node ran -- CheckViolationError, confirmed live against
-- the development database rather than inferred from reading the constraint.
--
-- The damage was not that findings were lost. They were not: on scan
-- 63497fc6 (hubspot.com, 2026-09-23) the node created its three findings and
-- committed them in its own session BEFORE track_stage tried to write the stage row
-- on the way out of the context manager. What was lost is the RECORD that those
-- findings came from rules with no narrative -- exactly the metadata the node goes
-- out of its way to write (`source='rules'`, `narrative_missing=True`, the reason,
-- the rule ids) so that a reader of the audit trail is not misled about what they
-- are looking at. A finding that understates its own provenance is the failure this
-- whole fallback was built to avoid.
--
-- It also took the run down after the commit, which is why that scan's agent_run is
-- still 'paused' with no 'audit_saved' stage: the graph raised on the way out of a
-- node that had already done its work.
--
-- Additive. Widening a CHECK cannot invalidate a row that already satisfies it, so
-- there is nothing to backfill and no existing stage name changes meaning.
alter table agent_run_stages
    drop constraint if exists agent_run_stages_stage_check;

alter table agent_run_stages
    add constraint agent_run_stages_stage_check
    check (stage in (
        'url_validation','website_scan','data_structuring','classification',
        'rules_check','rag_retrieval','llm_analysis','output_validation',
        'findings_generated','rule_findings_generated','audit_saved'
    ));
