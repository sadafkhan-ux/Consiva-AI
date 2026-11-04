# Development report, 2026-11-03 17:44:14 IST (Phase 2, first increment: URL inventory and crawl controls)

## 1. Executive summary
The URL inventory and configurable crawl controls are implemented, tested offline, and deployed to the local Docker stack (migration 0047 applied, backend healthy). Every URL a scan discovers now has a persistent record with its depth, status and reason. Application scans accept depth, URL-count, concurrency, rate and memory controls inside server-side ceilings; the public web profile is unchanged. The root cause of the known RLS test failure was found and fixed. No live website was scanned, so none of this has been seen on a real whole-site crawl.

## 2. Technical design
`docs/URL_INVENTORY_DESIGN.md`: reused structures, new table, lifecycle, controls, API contract, compatibility. Key decisions: the inventory is keyed on the crawler's own normalized URL; sitemap URLs count as depth 1; controls live on `scan_options.crawl`, are stored already clamped, and are re-clamped on read; a scan without controls (every web scan, and application scans made before this) runs as it always did.

## 3. Database changes
Migration `0047_scan_url_inventory.sql`, additive:
- Table `scan_url_inventory` (unique `(scan_id, normalized_url)`, status check, "failed needs a reason" and "excluded needs a reason" checks, three indexes, nullable link to `website_pages`, cascade on scan delete). RLS enabled **and forced**, policy through the scan's organisation.
- `FORCE ROW LEVEL SECURITY` on `dsr_evidence_pushes`, `scan_screenshots`, `llm_usage_events`, `report_pdfs`.
Applied to the Docker database (by the migrate service) and to the host development database.

## 4. API contracts
- New: `GET /api/v1/consent/scans/{scan_id}/url-inventory` with `status`, `http_status`, `source`, `depth`, `q`, `failure`, `limit` (1 to 500), `offset`. Returns `summary` (counts over the whole inventory), `urls` (one page), `total`. Another organisation's scan is a 404. A scan with no inventory returns zeros and no rows.
- Extended (additive): `POST /api/v1/consent-agent/scans/application`, `scan_options` gains `max_depth`, `max_urls`, `concurrency`, `per_domain_concurrency`, `requests_per_second`, `memory_budget_mb`. Authorization, DNS verification and SSRF checks are unchanged.

## 5. URL inventory implementation
Implemented and tested: record per distinct URL; normalization and duplicate prevention (an extra source is added, not a second record); multiple discovery sources; depth assignment; the six statuses with enforced transitions (final statuses cannot change); failure and exclusion reasons persisted; timestamps (discovered, started, completed, updated); PARTIALLY_SCANNED for a page that loaded but produced no text or is not HTML; discovered-but-never-reached URLs stay PENDING with the reason. A redirect to another website is EXCLUDED and the other address is not recorded (the scan's existing rule).
Not implemented: pagination discovery, SPA route discovery, dynamic navigation, per-page extraction detail.

## 6. Crawl controls
Implemented and tested: maximum depth (entry address = 0; deeper URLs are recorded as EXCLUDED `depth_limit`); maximum URLs (further URLs counted in `diagnostics.discovery_limit`, not recorded); overall and per-domain concurrency (the smaller applies; the existing per-worker navigation limit still applies on top); per-domain rate limit (ordered spacing of navigation starts); memory budget (launching pauses while over budget, events recorded, scan stops with `resource_limit` if nothing is in flight; reported "not enforced" if psutil is missing). HTTP 429 handling and bounded backoff already existed in `_navigate_with_retry` and were not changed or re-tested here.
Not verified: the memory guard against real browser processes; the rate limiter under real network latency.

## 7. Frontend implementation
URL inventory panel (status tiles, "N of M discovered were scanned" note, filters for status, HTTP status, source, depth, URL and reason, paging, redirect target and reasons shown, empty and error states). Whole-site scan settings form with client-side validation matching the API bounds; it states that the server decides and may lower any value. Validated by 36 unit tests; not exercised in a browser against a real scan.

## 8. Security validation
Tested: the web profile ignores stored controls and keeps the five-page cap; an unknown or tampered profile resolves to web; a tampered stored crawl block cannot widen the ceilings and a malformed value falls back to a default, not to "no limit"; robots.txt-disallowed URLs are never loaded; redirects off-site are never listed; tenant isolation through the endpoint (404 for another organisation); RLS forced on the new table. DNS verification and SSRF checks were not changed; their existing tests pass in the full run.

## 9. Test results (executed)
- Backend, full suite excluding the two live-model tests, last full run: 4,505 passed, 1 failed, 48 skipped. That failure (`test_consent_console_contract`, endpoint count) was fixed afterwards and the file re-run: 97 passed. The full suite was not re-run after that change.
- New backend tests: 101 (26, 27, 21, 27 across the four new files), all passing.
- Frontend: 1,137 passed. Type check clean.
- Lint: new files clean; edited files unchanged from their prior findings.

## 10. Existing RLS test
`test_rls_is_forced_on_every_tenant_table` failed because migrations 0035, 0040, 0041 and 0043 enabled RLS and created policies but never forced it (0018 had warned that later tables must opt in). Migration 0047 forces it. Verified two ways: the test now passes against the host database, and a direct query of the Docker database shows no table with RLS enabled but not forced. Effect today: none for the application, which connects as a superuser; it matters at the cutover to the least-privilege role. The new migration adds no tenant-isolation problem (checked by the hardening tests and a dedicated test).

## 11. Performance observations
Not measured. The offline suite for the new code runs in about a minute, most of it a deliberate rate-limit test. A 5,000-row cap protects a public scan with a huge sitemap; large application inventories (up to 10,000 by default) are inserted in one batch and have not been timed.

## 12. Known limitations
- No live whole-site scan has been run; behaviour on real sites, large sitemaps and the memory guard is unverified.
- Application scans carry the default controls only when created through the updated endpoint; older application scans run uncontrolled.
- The dashboard's whole-site button needs an organisation administrator and a DNS-verified domain; otherwise the server refuses, and the message is shown as for any failed scan.
- `PENDING` for a scan still running shows pages not yet reached; the panel does not poll while a scan runs.
- Sitemap depth is a convention (1), not a measured link distance.
- The scan-session logging architecture does not exist yet; the README only reserves its place.

## 13. Remaining tasks (next phases)
Pagination and SPA discovery; page-level extraction attached to inventory rows; cookie, token, storage, session and network observations; scan-session log files; backend copy of the ten-check score; agent registry; rename "consent" identifiers.

## 14. Git
Branch `feature/privacy-policy-agent`; base `63a281b` (documentation commit); Phase 2 changes uncommitted; nothing pushed. `main` untouched.
