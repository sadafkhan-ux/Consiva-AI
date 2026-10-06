-- 0028: instrument the two Consent Agent graph nodes that had no stage timer at all.
--
-- A real analyze run (scan 42288619, swaransoft.com, 2026-10-06) showed the "analyze"
-- job taking 220.66s end to end while its five instrumented stages -- rules_check,
-- rag_retrieval, llm_analysis, output_validation, findings_generated -- summed to only
-- ~50s. Queue wait was ruled out (0.85s) and checkpoint bloat was ruled out (10 rows,
-- under 10KB for the whole thread), which leaves ~170s genuinely unaccounted for
-- inside the job's own execution. The two LangGraph nodes with zero stage coverage --
-- normalize (loads scan evidence) and human_review_gate (checks pending findings,
-- then pauses) -- are the only place left for it to be hiding.
--
-- Widening the CHECK rather than assuming the insert would just work: 0021 already
-- hit exactly this trap once (a new stage name rejected by this same constraint,
-- silently swallowed by track_stage's own error tolerance, no crash, just no row).
--
-- Additive. Widening a CHECK cannot invalidate a row that already satisfies it, so
-- there is nothing to backfill and no existing stage name changes meaning.
alter table agent_run_stages
    drop constraint if exists agent_run_stages_stage_check;

alter table agent_run_stages
    add constraint agent_run_stages_stage_check
    check (stage in (
        'url_validation','website_scan','data_structuring','classification',
        'normalize_evidence',
        'rules_check','rag_retrieval','llm_analysis','output_validation',
        'findings_generated','rule_findings_generated',
        'human_review_gate',
        'audit_saved'
    ));
