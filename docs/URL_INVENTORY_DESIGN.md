# URL inventory and crawl controls: technical design (Phase 2)

Status: design for the first Phase 2 increment, as implemented on 2026-11-03. Items under "Not in this increment" are not built.

## 1. What already exists and is reused
| Need | Existing structure | Decision |
|---|---|---|
| A record per attempted page | `website_pages` (url, title, http_status, discovered_via, final_url, content_type) | Kept as is. It holds only pages the scanner navigated to. The inventory links to it (`website_page_id`). |
| Discovered, skipped, never-reached URLs | `scan_diagnostics.skipped_pages` (capped list, no persistence of the full set) | Replaced as the source of truth by the inventory; the diagnostics counters stay for compatibility. |
| URL normalization and de-duplication | `scanner/url_normalize.normalize_url`, the crawler's `seen_keys` | Reused unchanged; the inventory is keyed on the same normalized form. |
| Scan budget and profile | `scanner/scan_profiles.ScanProfile`, options stored on `consent_scans.scan_options` | Extended with an optional `crawl` block (application profile only). |
| Concurrency | `scanner_max_concurrent_pages` (per scan), `scanner_max_concurrent_navigations` (per worker, all scans) | Reused. The new controls can only lower them. |
| 429 and retry handling | `_navigate_with_retry`, `failures.parse_retry_after` | Reused. The new per-domain rate limiter spaces navigation starts; it does not replace retry. |
| Tenant isolation | `consent_scans.org_id` + RLS; `scan_screenshots` pattern (policy through the scan) | Same pattern for the new table. |

## 2. New table: `scan_url_inventory` (migration 0047)
One row per distinct normalized URL per scan.

| Column | Type | Notes |
|---|---|---|
| id | uuid pk | |
| scan_id | uuid not null, fk consent_scans on delete cascade | tenancy follows the scan |
| website_page_id | uuid null, fk website_pages on delete set null | where later modules attach page observations |
| domain | text not null | host of the page |
| original_url | text not null | first spelling seen |
| normalized_url | text not null | unique with scan_id |
| final_url | text | after redirects; null until the page loads |
| page_title, http_status, content_type | text, int, text | null until known |
| discovery_source | text not null | first source: seed, link, sitemap |
| discovery_sources | text[] not null | every source that named it |
| crawl_depth | int not null, >= 0 | seed 0; link = parent + 1; sitemap = 1 (one step from the entry address) |
| scan_status | text not null | PENDING, IN_PROGRESS, COMPLETED, FAILED, PARTIALLY_SCANNED, EXCLUDED |
| status_reason | text | why EXCLUDED, why still PENDING, what was incomplete for PARTIALLY_SCANNED |
| failure_reason | text | FAILED only |
| discovered_at, scan_started_at, scan_completed_at, last_updated_at | timestamptz | |

Indexes: unique `(scan_id, normalized_url)`; `(scan_id, scan_status)`; `(scan_id, crawl_depth)`; `(scan_id, http_status)`.
Constraints: status in the set; FAILED rows carry a failure_reason; EXCLUDED rows carry a status_reason.
RLS: enabled and **forced**, policy `tenant_isolation` through the scan's `org_id`.

## 3. Status lifecycle
```
PENDING -> IN_PROGRESS -> COMPLETED
                       -> PARTIALLY_SCANNED   (page loaded, content extraction incomplete)
                       -> FAILED              (page did not load)
PENDING -> EXCLUDED                           (robots.txt, depth limit, duplicate query variant)
IN_PROGRESS -> EXCLUDED                       (the page loaded but redirected to a page already scanned, or to another website)
```
No other transition is accepted; COMPLETED, PARTIALLY_SCANNED, FAILED and EXCLUDED are final. A URL that redirects to another website is EXCLUDED with the reason `redirected_to_another_site` and **the other address is not recorded**: the scan's existing rule is that another website is never listed. A URL still PENDING when the crawl ends stays PENDING with a `status_reason` (page limit, deadline, cancelled, slow site, resource limit): discovered is not scanned. HTTP success does not mean extraction succeeded: a page that loaded with no extractable text is PARTIALLY_SCANNED.

## 4. Crawl controls (application profile only)
Stored on `consent_scans.scan_options.crawl` after the server has clamped them. Public `web` scans ignore every field: the five-page cap and behaviour are unchanged.

| Field | Meaning | Default | Hard ceiling (setting) |
|---|---|---|---|
| max_depth | deepest link level crawled (seed = 0) | none (unbounded) | `CONSENT_APP_CRAWL_MAX_DEPTH_CEILING` (50) |
| max_urls | most distinct URLs recorded in the inventory (a scan without controls, such as every public web scan, records at most 5,000) | `CONSENT_APP_DEFAULT_MAX_URLS` (10,000) | `CONSENT_APP_MAX_URLS_CEILING` (1,000,000) |
| concurrency | pages in flight in this scan | `SCANNER_MAX_CONCURRENT_PAGES` (4) | `CONSENT_APP_CONCURRENCY_CEILING` (16) |
| per_domain_concurrency | pages in flight against one domain | same as concurrency | concurrency |
| requests_per_second | navigation starts per second against the domain | `CONSENT_APP_DEFAULT_RPS` (5) | `CONSENT_APP_RPS_CEILING` (20) |
| memory_budget_mb | pause launching pages while the scanner's browsers use more than this | 0 (off) | `CONSENT_APP_MEMORY_BUDGET_CEILING_MB` (16,384) |

A request can only choose values inside the ceilings; a value above a ceiling is clamped, a value below the minimum is refused (HTTP 422). The effective values are stored on the scan and returned.
Limits reached are recorded, never silent: `diagnostics.discovery_limit` (URLs not recorded past `max_urls`), `diagnostics.resource_events` (pauses and stops), and the inventory rows' `status_reason`.

Memory: measured with `psutil` over the Python process and its children (the browsers). If `psutil` is missing the control is reported as "not enforced", not silently skipped.

## 5. API (additive)
- `POST /api/v1/consent-agent/scans/application`: `scan_options` gains optional `max_depth`, `max_urls`, `concurrency`, `per_domain_concurrency`, `requests_per_second`, `memory_budget_mb`. Existing callers send none and see no change. The response already returns the stored options.
- `GET /api/v1/consent-agent/scans/{scan_id}/url-inventory`: query `status`, `http_status`, `source`, `depth`, `q` (substring of the URL), `failure` (substring of the reason), `limit` (1 to 500, default 100), `offset`. Returns `{ summary: {total, pending, in_progress, completed, failed, partially_scanned, excluded}, urls: [...], total, limit, offset }`. Tenant-scoped by the caller's organisation; another organisation's scan is a 404.

## 6. Backward compatibility
- All migrations are additive. Scans made before this increment have no inventory rows; the endpoint returns an empty list with zero counts, never invented rows.
- `scan_diagnostics` keeps every existing key. `ScanResult.url_inventory` is a new field with an empty default.
- The web profile, DNS verification, SSRF checks, robots.txt, same-site restriction and de-duplication are not touched.

## 7. Not in this increment
Pagination and SPA route discovery, per-page extraction detail, cookie, token, storage, session and network observations, and the scan-session log files. The inventory carries `website_page_id` and `scan_id` so those can attach later.
