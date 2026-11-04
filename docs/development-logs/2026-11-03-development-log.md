# Development log, 2026-11-03

## Session information
- Date: 2026-11-03
- Start time: not recorded for the earlier part of this session. The audit below started at 17:07 IST.
- End time: open (session still running when this entry was written, 17:07 IST)
- Timezone: Asia/Kolkata (UTC+05:30)
- Branch: `feature/privacy-policy-agent`
- Commit hash: `b9b0993` ("sdfgh"). `main` and `origin/main` are at the same commit.

## Development work
**Features and fixes (earlier today, all in `b9b0993`)**
- Page evidence now stores the final URL after redirects and the content type (migration 0046; scanner, repository and API).
- Ten-check engine (`frontend/src/report/tenChecks.ts`): new result "Not Assessed" (no marks, left out of the maximum); evidence-quality rules for checks 4, 5 and 6; check 10 no longer counts its own evidence; recommendations tied to checks that did not pass; "third-party requests" wording fix in check 8.
- Free and Full PDF score cards now print the ten-check score (same number as the Summary PDF and dashboard hero).
- Dashboard: collapsed "How the Privacy Readiness Score was calculated" panel (`TenChecksPanel.tsx`).
- Summary PDF: compact refinement of both pages; page 1 now has the score and the consent banner in a left column beside a slightly smaller screenshot, with no score breakdown on page 1.

**Modules modified:** `backend/app/scanner/*`, `backend/app/db/*`, `backend/app/api/v1/routes/consent_scans.py`, `frontend/src/report/*`, `frontend/src/components/*`, `frontend/src/ConsentAgentView.tsx`.
**Architecture changes:** none.
**Database changes:** migration `0046_website_page_final_url_content_type.sql` (two nullable columns on `website_pages`). Applied to the Docker database and, today, to the host dev database together with the 12 earlier pending migrations (13 in all).
**API changes:** `EvidencePageItem` gained `final_url` and `content_type` (nullable).
**Frontend changes:** as above.

## Files changed
Recorded in Git history for `b9b0993`. Documentation files added in this session: `docs/PRIVACY_READINESS_TEN_CHECKS.md`, `docs/development-logs/2026-11-03-development-log.md`, `docs/development-reports/development-report-2026-11-03-17-07-23.md`, `docs/scan-session-logs/README.md`.

## Testing (what was actually executed)
- Backend, full suite excluding the two live-model tests: 4,403 passed, 1 failed, 48 skipped. The failure is `test_rls_is_forced_on_every_tenant_table` on the host dev database (RLS not forced on `dsr_evidence_pushes`, `scan_screenshots`, `llm_usage_events`, `report_pdfs`); not investigated further. Backend code has not changed since that run.
- Frontend: 1,101 passed (vitest, after the page 1 layout change).
- Type check (`tsc -p tsconfig.app.json`): clean. Lint: not run. Build: Docker images rebuilt and healthy.

## Scanning and validation
No live scans were run. Stored scans were re-rendered through the PDF pipeline: swaransoft.com (stored scan 6ad4b110) and cookieyes.com (c2903a78), both two pages. No scan statistics are claimed here.

## Pending work
See the development report of the same date. The master requirements (full crawl inventory, storage and token capture, session tracking, network records, scan-session logs) are not implemented yet. Next: confirm the build order and start Phase 3.

## Git information
- Branch: `feature/privacy-policy-agent`; latest commit `b9b0993`.
- Uncommitted changes: the documentation files listed above.
- Push status: `main` and `feature/privacy-policy-agent` pushed at `b9b0993`; the documentation files are not committed or pushed.

---

# Session 2 (Phase 2: URL inventory and crawl controls)

## Session information
- Date: 2026-11-03. Started about 17:15 IST (after the Phase 1 commit); this entry written 17:44 IST. Timezone: Asia/Kolkata (UTC+05:30).
- Branch: `feature/privacy-policy-agent`. Base commit `63a281b` ("docs: add repository audit and development tracking reports", the Phase 1 documentation commit on top of `b9b0993`). Phase 2 work is uncommitted.

## Development work
- **Design:** `docs/URL_INVENTORY_DESIGN.md` (existing structures reused, new table, lifecycle, crawl controls, API, compatibility).
- **URL inventory:** new `scan_url_inventory` table (migration 0047), `ScanUrlInventory` model, `scanner/url_inventory.py` (record, tracker, status lifecycle with enforced transitions, extraction check), crawler integration, persistence in `save_scan_result` (own savepoint; a failure is logged as an error, the scan is kept), `get_url_inventory` query with filters and counts.
- **Crawl controls (application profile only):** `scanner/crawl_controls.py` (max depth, max URLs, concurrency, per-domain concurrency, requests per second, memory budget; server-side ceilings in `config.py` `CONSENT_APP_*`), a per-domain rate limiter, a memory guard (psutil), controls stored on `scan_options.crawl` and read back by the worker. The public web profile ignores them and is unchanged.
- **API:** `GET /api/v1/consent/scans/{id}/url-inventory` (new); `POST /api/v1/consent-agent/scans/application` accepts the controls in `scan_options` (additive).
- **Frontend:** URL inventory dashboard (counts by status, filters for status, HTTP status, source, depth, URL, reason, paging), whole-site scan settings form with validation, API client methods.
- **RLS:** root cause of `test_rls_is_forced_on_every_tenant_table` found and fixed in migration 0047: 0035, 0040, 0041 and 0043 enabled RLS but never forced it. Verified on both databases (see below).
- **Dependency added:** `psutil` (requirements.txt and lock file).
- **Existing tests changed, with reason:** `test_scan_profiles` (the stored application options now include the `crawl` block) and `test_consent_console_contract` (the console calls one more endpoint, 18 not 17).

## Files changed
- Added: `backend/app/scanner/url_inventory.py`, `backend/app/scanner/crawl_controls.py`, `backend/migrations/0047_scan_url_inventory.sql`, `backend/tests/test_url_inventory.py`, `test_crawl_controls.py`, `test_crawl_inventory_integration.py`, `test_url_inventory_db.py`, `docs/URL_INVENTORY_DESIGN.md`, `frontend/src/components/UrlInventoryPanel.tsx`, `CrawlSettings.tsx`, `urlInventory.test.tsx`, `frontend/src/report/crawlConfig.ts`, `crawlConfig.test.ts`, this report and log.
- Modified: `backend/app/config.py`, `db/models.py`, `db/repositories/scan_repository.py`, `scanner/crawler.py`, `scanner/scan_profiles.py`, `scanner/schemas.py`, `api/v1/routes/consent_agent.py`, `consent_scans.py`, `api/v1/schemas/consent_agent.py`, `requirements.txt`, `requirements.lock.txt`, `backend/tests/test_scan_profiles.py`, `test_consent_console_contract.py`, `frontend/src/ConsentAgentView.tsx`, `api/client.ts`, `api/types.ts`, `hooks/useConsentScan.ts`.
- Deleted: none.

## Testing (executed)
- Backend, full suite excluding the two live-model tests, last full run: 4,505 passed, 1 failed, 48 skipped. The one failure (`test_consent_console_contract`, endpoint count) was then fixed and that file re-run: 97 passed. The full suite was not re-run after that one-line change.
- New backend tests: 101 across four files, all passing (url inventory 26, crawl controls 27, crawl integration 21, database 27). New frontend tests: 36 (crawl configuration 21, inventory dashboard and settings form 15). `test_rls_is_forced_on_every_tenant_table` now passes.
- Frontend: 1,137 passed (54 files). Type check clean.
- Lint (ruff), scoped to files created or edited in this increment: clean on the new files; the edited files have only findings that were already there (same count as at HEAD) apart from one I removed.
- Docker images rebuilt; migration 0047 applied by the migrate service; backend healthy; database check: no table has RLS enabled but not forced.

## Scanning and validation
No live scans. Everything above uses fake browsers, fixtures and rolled-back database transactions. Not verified end to end: a real whole-site scan, the dashboard against a scan that has an inventory, and the memory budget against real browser processes.

## Pending work
Pagination and SPA route discovery; per-page extraction detail; cookie, token, storage, session and network observations (Phases 5 onward); scan-session log files; a backend copy of the ten-check score; an agent registry. See the report of the same date for detail.

## Git information
- Branch `feature/privacy-policy-agent`, base `63a281b`. Uncommitted changes: the Phase 2 files listed above. Not pushed.
