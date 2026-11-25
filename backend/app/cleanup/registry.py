"""What the local cleanup may delete, for both scanning agents, and what it must never touch.

The cleanup deletes ROWS of scan-generated tables for scans that have finished. It never drops or alters a table, a column or a constraint, never touches a scan's own row (the scan history), and never
touches a table that is not listed here. A table that a business table points at (an approval, a remediation action, the audit log) is protected at run time from the foreign keys themselves: a row that
such a table still references is kept (see core.referencing_guards), so a mixed-ownership row is never deleted by a guess.

Scan history that is kept, per agent:
  five_page   consent_scans   id, url, status, error, created_at, started_at, completed_at  (and the scan's configuration columns, which are not scan output)
  enterprise  ent_scans       id, website_url, status, analysis_status, error columns, created_at, started_at, completed_at
Everything else about a finished scan is cleared.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Step:
    """Rows of `table` that belong to the scans being cleaned. `where` uses :ids (the scan ids) or, for organisation-level tables, :orgs / :pairs."""
    table: str
    where: str
    note: str = ""


@dataclass(frozen=True)
class Agent:
    key: str
    label: str
    scans_table: str
    steps: tuple[Step, ...] = field(default_factory=tuple)


_BY_SCAN = "scan_id = any(cast(:ids as uuid[]))"
_BY_THREAD = "thread_id in (select langgraph_thread_id from agent_runs where scan_id = any(cast(:ids as uuid[])) and langgraph_thread_id is not null)"

FIVE_PAGE = Agent(
    key="five_page", label="Original five-page Privacy Policy agent", scans_table="consent_scans",
    steps=(
        Step("checkpoint_writes", _BY_THREAD, "intermediate analysis state (LangGraph checkpoint)"),
        Step("checkpoint_blobs", _BY_THREAD, "intermediate analysis state (LangGraph checkpoint)"),
        Step("checkpoints", _BY_THREAD, "intermediate analysis state (LangGraph checkpoint)"),
        Step("consent_recommendations", "finding_id in (select id from consent_findings where scan_id = any(cast(:ids as uuid[])))", "advice generated for a scan's findings"),
        Step("consent_forms", _BY_SCAN, "forms found on the scanned pages"),
        Step("scan_url_inventory", _BY_SCAN, "URL-level crawl status of the scan"),
        Step("cookies", _BY_SCAN, "cookies observed"),
        Step("trackers", _BY_SCAN, "trackers observed"),
        Step("consent_findings", _BY_SCAN, "findings of the scan"),
        Step("agent_run_stages", _BY_SCAN, "per-stage records of the analysis, including model output"),
        Step("agent_runs", _BY_SCAN, "the analysis runs of the scan"),
        Step("website_pages", _BY_SCAN, "the crawled pages and their extracted text"),
        Step("consent_signals", _BY_SCAN, "consent banner signals"),
        Step("policies", _BY_SCAN, "policy documents and their extracted text"),
        Step("third_party_services", _BY_SCAN, "third parties observed"),
        Step("llm_usage_events", _BY_SCAN, "token use of the scan's model calls"),
        Step("report_pdfs", _BY_SCAN, "stored five-page report PDFs"),
        Step("scan_screenshots", _BY_SCAN, "homepage screenshots"),
        Step("purpose_assessments", _BY_SCAN, "purpose assessments of the scan"),
        Step("purpose_runs", _BY_SCAN, "purpose analysis runs of the scan"),
    ),
)

# Written into ent_scans.purge_summary.cleared_by when the cleanup deletes an Enterprise scan's results. The Enterprise API reads it to answer
# 410 for that scan (app/enterprise/scans.py: results_cleared). Kept here so the cleanup never has to import the Enterprise package.
FULL_CLEAR_MARK = "local scan-data cleanup"

ENTERPRISE = Agent(
    key="enterprise", label="Enterprise agent", scans_table="ent_scans",
    steps=(
        Step("ent_analysis_chunks", _BY_SCAN, "analysed passages and model output"),
        Step("ent_assessments", _BY_SCAN, "parameter assessments"),
        Step("ent_findings", _BY_SCAN, "findings"),
        Step("ent_reports", _BY_SCAN, "stored Enterprise report PDFs"),
        Step("ent_score_results", _BY_SCAN, "scores and score history"),
        Step("ent_method3_results", _BY_SCAN, "method 3.0.0 results and their history"),
        Step("ent_audit_events", _BY_SCAN, "per-scan audit events"),
        Step("ent_evidence", _BY_SCAN, "extracted page text and HTML, documents, browser observations"),
        Step("ent_frontier", _BY_SCAN, "the URL queue and URL-level crawl status"),
    ),
)

#: Organisation-and-website level Enterprise data derived from scans of that website: the policy knowledge base and the reuse cache. Cleared for a website once none of its scans is active.
ENTERPRISE_SITE_STEPS: tuple[Step, ...] = (
    Step("ent_policy_chunks", "(org_id, domain) in (select * from unnest(cast(:orgs as uuid[]), cast(:domains as text[])))", "policy passages and their embeddings"),
    Step("ent_policy_sync_log", "(org_id, domain) in (select * from unnest(cast(:orgs as uuid[]), cast(:domains as text[])))", "knowledge base synchronisation log"),
    Step("ent_policy_retrievals", "(org_id, domain) in (select * from unnest(cast(:orgs as uuid[]), cast(:domains as text[])))", "knowledge base search log"),
    Step("ent_policy_versions", "doc_id in (select id from ent_policy_docs where (org_id, domain) in (select * from unnest(cast(:orgs as uuid[]), cast(:domains as text[]))))", "stored policy versions (full extracted text)"),
    Step("ent_policy_docs", "(org_id, domain) in (select * from unnest(cast(:orgs as uuid[]), cast(:domains as text[])))", "policy document identities"),
)
ENTERPRISE_ORG_STEPS: tuple[Step, ...] = (
    Step("ent_fact_cache", "org_id = any(cast(:orgs as uuid[]))", "reusable analysis results (facts and quotes from website text)"),
)

#: A cache of AI policy analyses keyed by the hash of their input (shared by every organisation, so not tied to one scan). Cleared only when no five-page scan is active anywhere.
FIVE_PAGE_GLOBAL_STEPS: tuple[Step, ...] = (Step("policy_analysis_cache", "true", "cached AI responses of policy analyses"),)

#: Tables that look like scan data but are NOT cleared: bookkeeping of the checkpoint library, the DPDP legal knowledge base, and everything else not listed above.
NEVER_CLEARED = ("checkpoint_migrations", "knowledge_documents", "knowledge_chunks", "report_approvals", "webhook_deliveries", "scan_schedules", "audit_logs")

AGENTS: tuple[Agent, ...] = (FIVE_PAGE, ENTERPRISE)

#: Statuses at which a scan has finished. A scan in any other state is active and is never touched.
FIVE_PAGE_TERMINAL = ("completed", "failed", "cancelled")
ENTERPRISE_TERMINAL = ("completed", "failed", "cancelled")
#: Job states that mean work for a scan is still to be done or in progress.
JOBS_DONE = ("done", "failed")


def all_cleared_tables() -> set[str]:
    return {s.table for a in AGENTS for s in a.steps} | {s.table for s in ENTERPRISE_SITE_STEPS} | {s.table for s in ENTERPRISE_ORG_STEPS} | {s.table for s in FIVE_PAGE_GLOBAL_STEPS}
