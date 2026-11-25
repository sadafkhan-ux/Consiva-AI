# Local scan-data cleanup (both agents)

Code: `backend/app/cleanup/` (`registry.py` what may be deleted, `core.py` the pass, `__main__.py` the command). Docker: the `cleanup` service in `docker-compose.prod.yml` and `scripts/cleanup_scan_data.sh`.
Checks: `backend/tools/cleanup_integration.py` (real Postgres, run in the Docker network, 38 checks), `backend/tests/test_cleanup_registry.py`, `frontend/src/enterprise/ScanDashboard.test.tsx`.
The original five-page agent's code, checks, scoring, prompts, APIs and PDF layout were not touched; nothing in it imports the cleanup.

> **Enterprise results are kept (owner's decision, 25 Nov 2026).** The automatic passes (the `cleanup` service and the pass the Enterprise pipeline asks for when a scan ends) clear
> **only the five-page agent**. Enterprise scans keep everything: the method 3 score, findings, evidence, the URL inventory and the stored PDF reports. This is
> `CLEANUP_KEEP_ENTERPRISE` (default `true`, `Settings.keep_enterprise` in `core.py`). Enterprise data is cleared only by an explicit `--agent enterprise` run, or by setting
> `CLEANUP_KEEP_ENTERPRISE=false`; after that, a cleared scan keeps only its history row (website, status, times). The five-page agent's cleanup is unchanged. The Enterprise column below
> describes what such an explicit run clears.

## What is kept, and what is cleared

For every scan of both agents the database keeps only the scan's own row:

| Agent | Table | Kept |
|---|---|---|
| Five-page | `consent_scans` | id, website address, status, failure reason, created / started / completed times (and its configuration columns, which are not scan output) |
| Enterprise | `ent_scans` | id, website, status, analysis status, failure reasons, created / started / completed times |

Everything else a finished scan produced is deleted (rows only; no table, column or constraint is ever dropped or changed). Enterprise: only by an explicit `--agent enterprise` run (see above):

| Five-page | Enterprise |
|---|---|
| `website_pages` (extracted text), `policies`, `cookies`, `trackers`, `consent_forms`, `consent_signals`, `third_party_services`, `scan_url_inventory` (URL-level crawl status), `consent_findings`, `consent_recommendations`, `agent_run_stages`, `agent_runs`*, `llm_usage_events`, `scan_screenshots`, **`report_pdfs` (stored five-page reports)**, LangGraph `checkpoints`, `checkpoint_blobs`, `checkpoint_writes` of the scan's runs, `policy_analysis_cache`, `purpose_*` | `ent_evidence` (extracted text, HTML, documents, observations), `ent_frontier` (URL queue and URL-level status), `ent_analysis_chunks`, `ent_assessments`, `ent_findings`, `ent_score_results`, **`ent_reports` (stored Enterprise reports)**, `ent_audit_events`, the policy knowledge base (`ent_policy_docs`, `_versions`, `_chunks` with their embeddings, `_sync_log`, `_retrievals`), `ent_fact_cache` |

\* kept when the audit log still points at the run (see below).

Never touched: every table not in that list: users and organisations, websites, the audit log, remediation actions and approvals, webhook deliveries, schedules, the DPDP legal knowledge base, ROPA / DSR / breach / regulatory-watch data, the job queue, the checkpoint library's own bookkeeping.

## Safety rules the code enforces

* **The target is verified before anything is read or changed.** It stops (exit 2, nothing changed) unless the database host is in `CLEANUP_ALLOWED_DB_HOSTS` (default `db,localhost,127.0.0.1`), the database name in `CLEANUP_ALLOWED_DB_NAMES` (`consiva`), `CLEANUP_CONFIRM_DATABASE` equals `<database>@<host>` (`consiva@db`), the server is not a standby, and no partner production webhook (`AGENT_WEBHOOK_URL` other than the UAT one) is configured.
* **Active scans are never touched.** Only scans that finished (completed, failed, cancelled) at least `CLEANUP_GRACE_MINUTES` ago (default 1440, 24 hours) and have no queued or running job, and (five-page) no pending or running analysis run, are cleared. A scan still "running" a day later with no job working on it is treated as stuck and cleared too (its status is left as it was).
* **Mixed ownership is protected from the foreign keys themselves.** A row that a business table still points at is kept, found at run time: a finding a remediation action or approval references, a run the audit log references (the audit log is append-only and is never changed). The pass also keeps any parent row that a kept child still needs, and a delete the database refuses is skipped, not forced.
* **Integrity is checked after every pass:** the structure (columns, types, constraints, indexes) is identical, no other table lost a row, the scan history is byte-for-byte identical, and every constraint is valid. A failed check makes the command exit 3.
* **Repeatable and recoverable.** A second pass finds nothing left. A history CSV is written before clearing (`backups/scan_history_*.csv`; the timer keeps one `scan_history_latest.csv`), and the script takes a full backup first.

## Running it

Dry run (reports what would be deleted, changes nothing):

```
bash scripts/cleanup_scan_data.sh
```

One-time cleanup of existing data (backs up, clears, verifies, then runs a second dry pass that must find nothing), and give the disk space back:

```
bash scripts/cleanup_scan_data.sh --execute --grace-minutes 0 --vacuum
```

Only one agent: add `--agent five_page` or `--agent enterprise`. After pulling new cleanup code add `--build` once. Without the script, inside the running containers:

```
docker compose -f docker-compose.prod.yml exec -T -w /app -e CLEANUP_CONFIRM_DATABASE=consiva@db backend python -m app.cleanup
docker compose -f docker-compose.prod.yml exec -T -w /app -e CLEANUP_CONFIRM_DATABASE=consiva@db backend python -m app.cleanup --execute --history-dir /tmp
```

Automatic: the `cleanup` service runs a pass every `CLEANUP_INTERVAL_SECONDS` (default 300) and the Enterprise pipeline asks for one when a scan ends. `docker compose -f docker-compose.prod.yml up -d cleanup` starts it; `stop cleanup` pauses it. Both clear the five-page agent only while `CLEANUP_KEEP_ENTERPRISE` is true (the default). Settings go in the compose environment: `CLEANUP_GRACE_MINUTES`, `CLEANUP_ENABLED`, `CLEANUP_RESPECT_PAUSED_REVIEWS` (see below), `CLEANUP_KEEP_ENTERPRISE`.
Restore a backup: `docker compose -f docker-compose.prod.yml exec -T db pg_restore -U consiva -d consiva --clean --if-exists < backups/pre_cleanup_<time>.dump`.

## The dashboard

`/api/v1/enterprise/dashboard/summary` reads only the kept rows of `consent_scans` and `ent_scans`, so it is complete after cleanup: for each scan the agent, the website, when it started and finished, completed / failed / pending, and the failure reason. Cards: total, completed, failed, pending or incomplete, five-page scans, Enterprise scans, last scan; filters by status and agent; search; automatic refresh while a scan is pending. The cards for URLs discovered / crawled / analysed and total findings were removed: that data is cleared by design.

## Things to know

* **A cleared scan's report cannot be opened.** The five-page report is drawn from the scan's stored data. After cleanup the existing five-page `report.pdf` endpoint (frozen, not changed) still answers, but with a blank "No score, 0 pages scanned" document, and caches it again (the next pass deletes the cache). The five-page console's own download button behaves the same for a cleared scan. Enterprise scans are kept by the automatic passes; one cleared by an explicit `--agent enterprise` run keeps only its history row, and its results and stored PDFs are gone.
* **The grace period matters for five-page scans.** A partner that fetches a five-page report after a scan finishes (for example after the status webhook) must do so within `CLEANUP_GRACE_MINUTES`. Enterprise results are kept. Pending human reviews of a cleared five-page scan's findings are gone with the findings (`CLEANUP_RESPECT_PAUSED_REVIEWS=true` keeps scans whose run is paused for review).
* Clearing the Enterprise fact cache means a repeat scan calls the model again (the cross-scan reuse saves nothing once its cache is cleared).
* Rows are deleted; disk space comes back only after `VACUUM FULL` (the `--vacuum` option).
* Test rows from development (`https://five.example.test/...` scans of throw-away organisations) cannot all be removed: the audit log rows that point at their runs are append-only. They hold no scan data.
