> **Superseded on 16 Nov 2026 by `docs/SCAN_DATA_CLEANUP.md`.** The owner decided that both agents keep only lightweight scan history (id, website, status, times, failure reason) and that everything else, reports included, is cleared. The cleanup code described here (`enterprise/retention.py`) is no longer called; the dashboard now shows both agents' history. The 15-second cache and the white theme below are still current.

# Enterprise retention, caching and dashboard

Added 16 Nov 2026 (migration 0060). Code: `backend/app/enterprise/retention.py`, `cache.py`, `api/dashboard.py`; frontend `frontend/src/enterprise/ScanDashboard.tsx`, `frontend/src/index.css`. Checks: `backend/tools/retention_integration.py` (real Postgres, run inside the container), `frontend/src/enterprise/ScanDashboard.test.tsx`.
Enterprise only. The 5-page agent's code, tables and reports were not touched (its console shares the page's stylesheet, so it takes the new white theme).

## What is cleared, and when

After a scan has been analysed and the grace period has passed (`ENTERPRISE_TEMP_RETENTION_HOURS`, default 24; 0 = as soon as the scan finishes), the next time any scan of the organisation finishes (or an administrator calls `POST /api/v1/enterprise/dashboard/retention/run`) the scan's temporary data is cleared:

| Cleared | Why it is temporary |
|---|---|
| Extracted text and HTML of pages that no assessment cites: every page not read as a privacy, cookie, terms, contact, grievance or about page (blog posts, product and service pages, listings) | They were read to find links and to be classified. Nothing cites them. |
| The bulky results of analysed passages (their facts and raw model output). The passage's counts and whether it was reused stay. | The facts are copied to the reuse cache first (below). |

| Always kept | |
|---|---|
| The scan, its URL inventory, discovered/crawled/analysed counts | history and dashboard figures |
| Assessments, findings, scores and score history, the audit trail | results |
| Every stored report (`ent_reports`) | never touched by retention |
| Browser observations: cookies, requests, forms, headers, consent tests | what the website does; small |
| Text and HTML of the pages read as policies (and contact, grievance, about) | the findings quote them and the report lists them |
| The policy knowledge base (`ent_policy_*`) | versioned policy history |

A scan is cleared once, never if it was not analysed. A scan from before pages were classified has no page categories; for it the pages kept are chosen by address (privacy, cookie, terms, policy, grievance, DPO, legal, contact, consent, about), which keeps more than needed and never less. Each clearing is recorded on the scan (`text_cleared_at`, `purge_summary`) and in its audit trail (`temp_data_cleared`). Nothing outside the Enterprise tables is touched.
Switch off with `ENTERPRISE_RETENTION_ENABLED=false`.

Disk space: the rows are deleted; PostgreSQL's autovacuum returns the space to the table for reuse, but the files do not shrink on disk until a `VACUUM FULL` (not run automatically: it locks the table).

## What is cached

* `ent_fact_cache`: what an analysed passage yielded (its facts and quotes), keyed by organisation, passage text hash and extraction prompt + model. A later scan of the same passage reuses it instead of calling the model; the cache is filled when a scan is cleared and is read before the intermediate rows. Entries unused for `ENTERPRISE_FACT_CACHE_DAYS` (default 90) are removed. A different organisation, text, prompt or model never matches.
* Dashboard aggregate: cached in the API process for 15 seconds (`cache.py`); a refresh button bypasses it. Per process, so it is never relied on for correctness.
* Stored reports are generated once per version and read back, not regenerated.

## The dashboard

`GET /api/v1/enterprise/dashboard/summary` returns counts, statuses and dates only: no report, evidence or page text. The screen shows interactive cards (URLs discovered, crawled, analysed; completed, failed and pending or incomplete scans; total findings; last scan status), a scan history with status filters, search, refresh (automatic every 15 seconds while a scan is running) and an open button, and a data-retention panel.

How a scan is classed: **failed** = the crawl or the analysis failed; **completed** = the crawl finished and the analysis completed (one that stopped at a limit or could not assess everything is shown as "completed, limited"); **pending or incomplete** = queued, crawling, analysing, or stopped before it finished (a cancelled scan is incomplete). Totals are summed over all of the organisation's scans, including cleared ones: the URL inventory is kept.

## Theme

The console is now a white theme: white panels on a light cool-grey page, deep blue for primary actions and active state, teal, green, amber and red for status, all dark enough to read on white. Colours are variables at the top of `frontend/src/index.css`.

## Not done, and why

* The integrated production database: the code base has no second database for Enterprise reports. The stored reports are in `ent_reports` (the same database as the scans) and are neither deleted nor duplicated by this change. If reports are kept elsewhere, point the report endpoints there before removing any local copy.
* The HTML of policy pages is kept (about 1 MB per policy page): the report reads policy titles from it. Clearing it too is possible once the report reads titles from the URL inventory instead.
* Scans that predate page classification are cleared by address patterns, not by category.
