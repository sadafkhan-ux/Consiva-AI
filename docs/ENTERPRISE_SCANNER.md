# Enterprise Scanner: what was built, how it flows, and what is still open

Status date: 13 Nov 2026. Everything below is **uncommitted** in git (`git status` shows it as modified or new). Nothing here was pushed.

How to read this document: sections 1 to 3 explain the idea. Sections 4 and 5 are the two flows, step by step. Sections 6 to 9 are the reference (database, API, settings, files). Sections 10 to 14 say what is verified, what is open, and what needs a decision.

---

## 1. What "Enterprise" means today

There are **two layers**. They share a name but do different jobs.

| | Layer A: the dashboard "Enterprise" tab | Layer B: the Enterprise crawler API |
|---|---|---|
| What the user sees | A screen that looks and works exactly like the normal Privacy Policy agent, but scans the whole website and downloads a detailed PDF | Nothing in the dashboard (the screen for it exists but is not linked) |
| Engine | The **existing** scanner, existing analysis, existing score, existing PDF. Only the page limit is lifted | A **new, separate** crawler with its own tables |
| Output | Score (e.g. 69/100), ten checks, findings, cookies, trackers, forms, policies, 59-page detailed PDF | Coverage list (every URL and what became of it) and raw evidence. **No score, no findings, no report** |
| Endpoint | `POST /api/v1/consent-agent/scans/application` (already existed) | `POST /api/v1/enterprise/scans` (new) |
| Used by the user today | **Yes** | Used once (a 149-URL crawl of swaransoft.com); not used from the dashboard now |

Why two layers exist: first the plan was a completely separate Enterprise product. Later the user said: make it work *exactly* like the 5-page agent, just over the whole website. Layer A is that. Layer B was already built and stays as a foundation (resumable crawl, evidence store, document reader, DPDP registry). It is switched on but unused by the dashboard.

---

## 2. Decisions the user made, in order

1. Do not touch the existing 5-page scanner, its score, APIs or PDFs.
2. GDPR is **not** being built now. DPDP only.
3. Architecture was discussed first; building started only after "now start building".
4. Phase 1 (foundation) approved. Then the crawl, documents and registry were built.
5. The user does **not** want DNS ownership verification for Enterprise scans. They should behave like normal scans.
6. The Enterprise tab must be the normal agent, only over the whole site, with a detailed PDF.
7. Scoring formula changes need the user's approval. None was made to the score engines.
8. Never commit unless asked. REPORT_REQUIRE_APPROVAL stays off.

---

## 3. Architecture in one picture

```
                         DASHBOARD (React)
        ┌───────────────────────┴───────────────────────┐
   Privacy Policy tab                              Enterprise tab
   <ConsentAgentView />                    <ConsentAgentView wholeSite />
   "Scan Website" (5 pages)                "Scan Whole Website" (no page cap)
        │                                                │
 POST /consent-agent/scans (web profile)   POST /consent-agent/scans/application
        └───────────────────────┬────────────────────────┘
                                ▼
            ONE job: consent_api_chain  (crawl, then analysis, then webhook)
                                ▼
  crawler.py  →  evidence tables  →  analysis (rules + LLM)  →  findings
                                ▼
          GET /assessment (the one score)  +  report.pdf (Chromium prints the web report)

   ── separate, not wired to the tab ──────────────────────────────────────────
   POST /api/v1/enterprise/scans → job enterprise_scan → enterprise/crawl/runner.py
        → ent_scans / ent_frontier / ent_evidence   (+ ent_registry_* for DPDP provisions)
```

---

## 4. Flow A: the dashboard Enterprise tab (the one in use)

### 4.1 What the user does
1. Opens `http://localhost:8100/#/enterprise` (sidebar item "Enterprise").
2. Types an address (a bare domain such as `swaransoft.com` is accepted).
3. Ticks "I own / am authorized to scan this domain".
4. Presses **Scan Whole Website**. The button is disabled until an address is typed and the box is ticked.

### 4.2 What the browser does (`ConsentAgentView.tsx`, `hooks/useConsentScan.ts`)
1. `handleFullSiteScan` calls `run(url, authorized, {})`. The empty object `{}` means "whole-site, use the server's own limits".
2. `run` sees the third argument is set and calls `api.createApplicationScan` (`POST /api/v1/consent-agent/scans/application`).
3. The server answers 202 with a `scan_id`. The page moves to "scanning".
4. `follow(scanId, runId, analyze=false, chained=true)` starts polling `GET /consent/scans/{id}` and `/stages` about every 1.2 s until the scan is `completed`, `failed` or `cancelled`.
5. Because the scan is **chained**, the page does **not** request the analysis again. (Earlier it did, the server answered 409 "conflicts with the scan's current state", and the page wrongly called the scan failed and never loaded results. That bug is fixed; `hooks/wholeSiteRun.test.tsx` guards it.)
6. It then polls the stages until the analysis finishes (up to 600 s) and loads: findings, audit, evidence, requirements, policy analysis, screenshot and the assessment (the score).
7. If a finding still needs human review, the phase is "awaiting review"; otherwise "completed".
8. The **Download Detailed Report (PDF)** button appears. It fetches `report.pdf?variant=long`.

### 4.3 What the server does
1. `consent_agent.py → create_application_scan`: the caller must be an organisation admin, or a service key with scope `consent:scan:application`.
2. `scan_profiles.resolve_profile("application")`: no page cap when `CONSENT_APP_MAX_PAGES=0`, no crawl deadline when `CONSENT_APP_MAX_SECONDS=0`, and a fixed 24 h ceiling only so a hung browser cannot hold a worker.
3. Domain verification: required only if `SCANNER_REQUIRE_DOMAIN_VERIFICATION` is on, **or** if the application profile asks for it. The profile asks for it only when `CONSENT_APP_REQUIRE_VERIFIED_DOMAIN=true`. On this machine it is `false`, so there is no DNS record step.
4. The address is checked for safety (private, loopback and link-local addresses are always refused).
5. A scan row is written, and one job `consent_api_chain` is queued (the crawl and the analysis run as one unit).
6. The worker runs `crawler.run_scan`:
   - reads robots.txt (RFC 9309), reads the sitemap, then follows links on the same registered domain;
   - renders each page in Chromium; records cookies, third-party requests, forms, scripts, policies, headers;
   - runs three consent passes (before consent, after Accept, after Reject) and a withdrawal test on the homepage;
   - records every discovered URL in `scan_url_inventory` with a status and a reason.
7. Then the analysis stages run (see 4.5), and finally the PDFs are pre-printed.

### 4.4 What happens to each kind of URL during the crawl

| URL kind | Result | Recorded as |
|---|---|---|
| Normal HTML page | Rendered and scanned | `COMPLETED` |
| Same page already queued or scanned | Not repeated | `duplicate` |
| Disallowed by robots.txt | Not visited | `robots_disallowed` |
| Page the site answers 200 but says "Page Not Found" | Treated as missing | `FAILED` (soft 404) |
| Real 404 / 410 | Missing | `FAILED` |
| Slow page (30 s timeout, retried) | Failed after retries | `FAILED` (timeout) |
| **Link to a file (PDF and similar)** | The browser tries to download it, so it cannot be shown as a page | `EXCLUDED: document_download`. Not a failure. Its text is **not read** |
| `/blog?page=2` (pagination) | **Followed** in a whole-site scan; skipped in a 5-page scan | `COMPLETED` / `query_variant_of_known_page` |
| `?filter=BFSI`, `?utm_source=...` | Same page with another query | `EXCLUDED: query_variant_of_known_page` |
| Redirects to another site | Not scanned; where it went is not recorded | `EXCLUDED: redirected_to_another_site` |

### 4.5 Analysis stages (after the crawl)
`url_validation → website_scan → classification → data_structuring → policy_analysis → normalize_evidence → rules_check → rag_retrieval → llm_analysis → output_validation → findings_generated → human_review_gate (→ audit_saved after review)`.

### 4.6 How the score is made (unchanged by Enterprise)
- Served by `GET /api/v1/consent/scans/{id}/assessment`. It is computed on request from stored evidence. It is the **only** score.
- Twelve checks. Each is pass, partial (half the marks), fail, not assessed, or not applicable.
- `score = round-half-up( marks earned ÷ marks in play × 100 )`. Quick scans are capped at 99.
- Coverage = 100 − the marks that could not be assessed. Below 60 there is no score.
- Worked example from the real report (scan 3ba3a805): earned 48 of 70 marks in play, 48/70 = 68.6, shown as **69/100**, coverage 70%.

### 4.7 What the detailed PDF contains (`variant=long`)
Cover with score, scan summary, scan transparency, what was and was not tested, framework readiness, top findings, consent behaviour, tracking technologies, privacy elements, the checks behind the score, prioritised fixes, grievance officer, DPO, Indian-language support, security signals, five-page DPDP readiness review, **Website Pages Scanned** (now every page for a whole-site scan), sector, policy content analysis, consent banner, forms, cookie inventory, third-party requests, policies, services, and compliance findings in full. The real swaransoft report was 59 pages.

---

## 5. Flow B: the Enterprise crawler API (separate engine)

### 5.1 Start
`POST /api/v1/enterprise/scans` with `{"website_url": "...", "authorized": true}`.
1. `ENTERPRISE_ENABLED` must be true, else every route answers 404.
2. Caller: an organisation admin, or a service key with `consent:scan:application`. Otherwise 403.
3. `authorized` must be true, else 400.
4. DNS verification only if `SCANNER_REQUIRE_DOMAIN_VERIFICATION` is on (same rule as the 5-page scan).
5. A row is written to `ent_scans` (status `queued`, the limits stored on it) and a job `enterprise_scan` is queued. Answer: 202 with `scan_id`.
6. A bare domain such as `consiva.ai` is accepted and stored as `https://consiva.ai`. `localhost`, `ftp://` and half addresses are refused with a plain message.

### 5.2 Run (`enterprise/crawl/runner.py`): strictly one URL at a time (owner's sequence, 19 Nov 2026)
1. The worker picks the job and calls `runner.run`. The scan moves `queued → running`; a scan already `completed`, `failed` or `cancelled` is returned as is.
2. Chromium starts with the 5-page scanner's host pinning, locale and user agent. Every browser context (shared, per page, discovery, each consent session) gets `crawl/guard.py`'s `host_guard`: a top-level navigation to any host other than the submitted one is aborted, then the existing SSRF guard runs. Embedded iframes and scripts load as for a normal visitor (they are the banner and tracker evidence); their links are never followed.
3. **Robots**, **resume** (`reclaim_stale`) and **seed** (homepage at depth 0, sitemap URLs at depth 1; off-host sitemap entries recorded as excluded, off-host sitemap files never fetched).
4. **Phase 1, discovery** (`_discover_inventory`): every pending page on the submitted host is visited once, one at a time (5-page navigation, settle, progressive scroll), only to read its links. Links are normalised, de-duplicated and recorded; subdomains and other websites are recorded as `excluded_subdomain` / `excluded_external_domain`. Redirects are read hop by hop before navigation (`crawl/redirects.py`, HEAD with no redirect following); an off-host hop excludes the URL with the original and destination. A same-host redirect is inspected at its destination once. Ends with audit event `url_discovery_completed` (not repeated on resume). The blocking rule applies here too.
5. If the submitted homepage redirects to another host (including `example.com → www.example.com`), the scan stops: `failed`, with a message naming the destination. Only the exact submitted host is scanned.
6. **Slug triage**: the model reads the complete inventory's addresses for policies the patterns miss (they get priority 0).
7. **Homepage** deep inspection (`crawl/inspect.py`: the 5-page first-page sequence plus the Enterprise extras), then the **consent tests** on it, one session at a time.
8. **Phase 2**: the privacy policies (priority 0), one at a time. When none is left, `_privacy_review` runs the text analysis on them (facts with exact quotes, compared to the DPDP parameters) through `_InterimRepo`, which keeps the readings but writes no final status or assessment; the result is audit event `privacy_policy_reviewed`.
9. **Phase 3**: the other legal pages (1), then every other page (2), one at a time (`claim_next(limit=1)`). Each page is checked before navigation (robots, exact host, redirect chain), deeply inspected in a fresh context, checked after loading (final host; duplicate destination), its evidence stored and committed, and the stored evidence checked in the database (`missing_evidence`) before the next URL opens.
10. **Blocking**: before 10 pages inspected, the first refusal (401/403/429/451, CAPTCHA, challenge) stops the scan; after that a website-wide refusal (429, challenge) or 3 refusals in a row stop it. The blocked URL and reason are recorded; unreached URLs stay pending with "not inspected: …"; `POST /resume` reopens a blocked scan.
11. **Finish**: blocked → `failed` with `stop_reason=bot_blocked|access_blocked`, yet the inspected pages are still analysed and reported, labelled "Blocked, incomplete". A limit → `completed` with `stop_reason`, labelled partial. No page done → `failed`. Score and PDF are produced only after the crawl and the analysis.

Evidence dedup: a cookie is `name|domain (no leading dot, lowercase)|path`, a script is its URL without the query string. Each page's cookie and network records list what is `new_in_scan` and which cookies `changed_in_scan` (other flags or lifetime class); every page keeps its full record, and the report shows each identity once with its pages. The same text at two addresses is read by the model once.

### 5.3 Frontier state machine (`ent_frontier`)
```
pending ──claim──► in_progress ──ok──────────► done
   ▲                    │ ├─ retryable failure & attempts left ─► pending
   │                    │ ├─ not retryable / attempts used up ──► failed (reason kept)
   └─ lease expired ────┘ └─ out of scope (robots, other site) ─► excluded (reason kept)
```
Added directly as `excluded` (with the reason): URLs beyond the depth limit, and query variants that are not pagination.

### 5.4 What the crawler may follow (`crawl/scope.py`, `crawl/host.py`)
Only the exact submitted hostname (case, trailing dot, default port and IDN form normalised; never a suffix test).

| Link | Decision |
|---|---|
| `mailto:`, `tel:`, `javascript:` | skip |
| Another host of the submitted site (subdomain, `www.` twin, parent domain, another port) | recorded as `excluded_subdomain`, never opened |
| Another website | recorded as `excluded_external_domain`, never opened |
| Images, scripts, styles, fonts, media, archives | skip |
| `.pdf .doc .docx .odt .rtf .xls .xlsx .ppt .pptx` | document: fetched and read |
| `?page=2`, `?paged=3&p=3` (pagination only) | page: followed |
| Any other query (`?color=red&sort=price`) | recorded as `excluded`, not crawled |
| `#fragment` | removed (same page) |

### 5.5 Documents (`enterprise/documents/`)
- PDF: read page by page with its page number. A page with no text (a scan image) is listed as "needs OCR"; OCR is **not built**, so those pages are reported as not read.
- DOCX: read paragraph by paragraph as one unpaged text. XML entities are not expanded (safe against hostile files). An oversize XML body is refused.
- Fetch: same safety check as a page, redirects followed one at a time and kept on the same site, hard size limit (default 25 MB). Larger files are excluded with the reason, never cut.

### 5.6 Evidence store (`enterprise/evidence/store.py`)
- Each captured item is stored with kind, source URL, page number, a SHA-256 of the content, size, how it was detected and when.
- The same content from the same place is stored once (unique on scan, kind, source, page, hash).
- Content over 5,000,000 bytes is refused whole, never silently cut.

### 5.7 Registry (`enterprise/registry/`)
- `seed_dpdp.json`: 38 DPDP provisions (Act sections and Rules) each with a phrase copied from the text. It is exactly the set in `scoring/legal_map.py`; a test fails if the two drift apart.
- It is **not** every section and rule. It says so in its own `coverage_note`.
- Loading creates a **draft** version. Only a named person's approval makes it approved. An approved version is frozen by database triggers: its provisions cannot be inserted, changed or deleted, and the version cannot be edited or un-approved.
- `applicability.effectiveness(commences, as_of)` answers `in_force` or `not_yet_effective` plus days until. It never decides *who* a provision applies to.

---

## 6. Database (migrations 0050, 0051, 0052; all applied, 52 migrations total)

| Table | Purpose | Key rules |
|---|---|---|
| `ent_scans` | one row per Enterprise crawl | `org_id`; status in queued, running, completed, failed, cancelled; a failed scan must carry an error; limits stored in `options` |
| `ent_frontier` | the crawl queue (what makes resume possible) | unique per (scan, normalized URL); status pending, in_progress, done, failed, excluded; failed needs `last_error`; excluded needs `status_reason`; a claimed row needs who and when |
| `ent_evidence` | raw captured content | kind in page_html, page_text, network, cookie, form, document_text, screenshot, header; unique per content |
| `ent_registry_versions` | versions of the DPDP registry | draft or approved; approved needs a named approver and a date; frozen once approved |
| `ent_registry_provisions` | provisions of one version | Act or Rules; reference, summary, relation, phrase, `official_text` (empty until loaded and signed off), commencement date |
| `agent_jobs` (existing) | job queue | its job-type CHECK now also allows `enterprise_scan` (0051) |

Row-level security is enabled **and forced** on `ent_scans`, `ent_frontier` and `ent_evidence` (tenant isolation through the scan's `org_id`). The registry tables are shared reference data, so they have no `org_id` and no RLS.
A database backup taken before the first migration is in the scratchpad folder as `pre_enterprise.dump` (92 MB).

---

## 7. API reference

### 7.1 Layer A (existing endpoints used by the Enterprise tab)
`POST /api/v1/consent-agent/scans/application`, `GET /api/v1/consent/scans/{id}` and `/stages`, `/evidence`, `/findings`, `/requirements`, `/policy-analysis`, `/screenshot`, `/audit`, `/assessment`, `/url-inventory`, `GET /api/v1/consent/scans/{id}/report.pdf?variant=long`.

### 7.2 Layer B (new)
| Endpoint | Who | What |
|---|---|---|
| `POST /api/v1/enterprise/scans` | admin or service key `consent:scan:application` | start, 202 |
| `GET /api/v1/enterprise/scans/{id}/status` | any signed-in caller of the org | state, URL counts, progress, stop reason, error |
| `GET /api/v1/enterprise/scans/{id}/coverage?status=&limit=&offset=` | same | every URL with depth, how it was found, HTTP status, reason |
| `POST /api/v1/enterprise/scans/{id}/cancel` | same | stop; evidence already collected is kept |
| `POST /api/v1/enterprise/scans/{id}/resume` | admin or service key | queue again; finished pages are not revisited |

A scan of another organisation answers "not found", as everywhere else. Without a token every route answers 401 (checked against the running backend).

---

## 8. Settings

In `backend/.env` (documented in `backend/.env.example`):

| Variable | Value here | Meaning |
|---|---|---|
| `ENTERPRISE_ENABLED` | `true` | the Layer B routes answer; `false` makes them 404 |
| `ENTERPRISE_MAX_URLS` | 0 | most URLs per scan; 0 = no cap |
| `ENTERPRISE_MAX_DEPTH` | 50 | deepest link level |
| `ENTERPRISE_CONCURRENCY` | 4 | pages in flight per scan |
| `ENTERPRISE_PER_DOMAIN_RPS` | 2 | navigation starts per second against one domain |
| `ENTERPRISE_MEMORY_BUDGET_MB` | 0 | pause launching pages above this; 0 = none |
| `ENTERPRISE_CLAIM_LEASE_SECONDS` | 300 | a claimed URL not finished in this time is taken back (resume) |
| `ENTERPRISE_MAX_ATTEMPTS` | 3 | tries per URL before it is recorded as failed |
| `ENTERPRISE_MAX_DOCUMENT_MB` | 25 | largest document fetched |
| `ENTERPRISE_MAX_EVIDENCE_BYTES` | 5,000,000 | largest single piece of evidence |
| `CONSENT_APP_MAX_PAGES` | 0 | whole-site scans: no page cap |
| `CONSENT_APP_MAX_SECONDS` | 0 | whole-site scans: no crawl deadline |
| `CONSENT_APP_REQUIRE_VERIFIED_DOMAIN` | `false` | no mandatory DNS record for whole-site scans (default in code is `true`) |

Out-of-range values stop the application from starting, so a typo never silently widens a crawl.

---

## 9. Files

### 9.1 New: backend
| File | Job |
|---|---|
| `app/enterprise/config.py` | `EnterpriseSettings`, the `ENTERPRISE_*` variables, read from the same `.env` |
| `app/enterprise/models.py` | SQLAlchemy models of the five new tables |
| `app/enterprise/scans.py` | create a scan, move it through its lifecycle, org-scoped lookup |
| `app/enterprise/crawl/frontier.py` | the queue: add, claim, done, failed, excluded, reclaim, counts |
| `app/enterprise/crawl/scope.py` | which links to follow (section 5.4) |
| `app/enterprise/crawl/discovery.py` | robots.txt, sitemap and link recording (reuses the existing readers) |
| `app/enterprise/crawl/renderer.py` | render one page in Chromium and return DOM, text, links |
| `app/enterprise/crawl/runner.py` | the whole crawl loop (section 5.2) |
| `app/enterprise/documents/readers.py` | PDF and DOCX text |
| `app/enterprise/documents/fetch.py` | safe document download |
| `app/enterprise/evidence/store.py` | hashed, de-duplicated evidence |
| `app/enterprise/registry/loader.py`, `applicability.py`, `seed_dpdp.json` | the DPDP registry (section 5.7) |
| `app/enterprise/api/scans.py`, `schemas.py`, `router.py`, `errors.py`, `jobs.py` | the API and the background job |
| `migrations/0050_enterprise_foundation.sql`, `0051_enterprise_scan_job_type.sql`, `0052_enterprise_registry.sql` | schema |

### 9.2 New: frontend
`src/api/enterprise.ts` (client for Layer B), `src/components/EnterpriseConsole.tsx` (a screen for Layer B: start, status, coverage, DNS panel; **not linked** from the sidebar now), plus tests.

### 9.3 Existing files that were changed (small, additive)
| File | Change |
|---|---|
| `api/v1/router.py`, `jobs/worker.py` | one registration line and one job branch for Layer B |
| `config.py` | the `CONSENT_APP_REQUIRE_VERIFIED_DOMAIN` setting (default unchanged) |
| `scanner/scan_profiles.py` | the application profile reads that setting |
| `scanner/crawler.py` | files that download are recorded as `document_download` (not failed, not counted as attempted); an uncapped scan follows pagination |
| `scanner/url_normalize.py` | `is_pagination_only` and the pagination parameter list |
| `api/v1/routes/consent_scans.py` | `/requirements` now passes the scan's real page limit (from its stored profile); `page_limit` may be null |
| `scoring/requirements.py`, `findings.py`, `review.py` | the page limit is a parameter, so "pages beyond the first 5 were not assessed" is only said of a capped scan. **No score logic.** |
| `frontend/src/App.tsx`, `dsrRoute.ts` | the Enterprise sidebar item and the `#/enterprise` address |
| `frontend/src/ConsentAgentView.tsx` | `wholeSite` mode: title, one "Scan Whole Website" button, detailed PDF |
| `frontend/src/hooks/useConsentScan.ts` | `chained` scans do not request the analysis again |
| `frontend/src/report/scanPages.ts`, `requirements.ts`, `print/ReportDocument.tsx`, `api/types.ts`, `components/ScanStatusSection.tsx` | no "limit 5" wording for an uncapped scan; every page listed; files shown as files |

**Untouched by design:** `scoring/scorecard.py`, `domain_score.py`, `ten_checks.py`, `ten_inputs.py`, `disclosure.py` (the files that compute the score). A test checks they have no git changes.

---

## 10. Rules that keep the old product safe

1. Nothing outside the Enterprise package imports it, except the two registration points (`api/v1/router.py`, `jobs/worker.py`). A test enforces it.
2. The Enterprise package never imports `app.scoring` or `app.services`. A test enforces it.
3. The score-engine files have no git changes. A test enforces it.
4. Defaults keep the old behaviour: the 5-page scan still stops at 5, the application profile still requires a verified domain unless the setting says otherwise.
5. Enterprise scans use their own job type, so they can be given their own worker pool later and never starve 5-page scans.

---

## 11. What is verified and what is not

| Item | State | How |
|---|---|---|
| Migrations 0050 to 0052 applied | verified | migrate service log; tables, RLS and triggers read back from Postgres |
| Frontier claim, resume, retry, evidence de-duplication | verified live | a script against the real Postgres (two workers claimed at once, no URL twice; stale claim taken back; attempts used up gave `failed`); test rows deleted |
| Registry loads as draft, 38 rows; approved version is frozen | verified live | five change attempts all refused by the database; the test version rolled back |
| Layer B crawl on a real site | verified by the user's run | swaransoft.com: 149 URLs, 142 scanned, 6 failed, 1 not scanned |
| Layer A whole-site scan, results and PDF | verified by the user's runs | scans 3ba3a805 (162 URLs, score 69, 59-page PDF) and d58f331b |
| A cookie on several pages is counted once | tested | 6 tests in `test_cookie_counting.py`; confirmed by 142 pages giving 5 cookies |
| Backend suite | last full run | 5,027 passed, 49 skipped, 1 failed (an over-broad guard, since narrowed; its 3 tests pass). The two tests that call a live LLM are excluded on purpose |
| Frontend suite | last full run | 1,012 passed, type check clean |
| New wording and page list in a **fresh** scan's PDF | **not yet seen** | no scan has been run since the last deploy |
| Authenticated calls to Layer B from a script | not run | no login token was available to the assistant |
| OCR, GDPR, DOCX/PDF text feeding the findings | not built | see section 13 |

---

## 12. Problems found in the real report and their state

| # | Problem | State |
|---|---|---|
| 1 | "Limit 5" / "Free scan limit" wording on a whole-site report | fixed (new scans and re-downloaded reports) |
| 2 | Section 16 listed only 8 of 142 pages | fixed |
| 3 | 8 PDF links shown as "failed to load" and the Partial-scan banner | fixed for **new** scans |
| 4 | Pagination pages skipped in a whole-site scan | fixed |
| 5 | Results never loaded after the crawl (409 on the second analyze request) | fixed |
| 6 | **Check 3 "No non-essential cookies before a choice" is FAIL 0 of 12 while the same report calls the same fact "Partial" (Section 3) and "Partially Met" (top finding)**; with Partial the score would be about 77, not 69 | **open, needs approval** (touches the scoring rule) |
| 7 | Check 3's evidence names Google Tag Manager, Analytics and Ads as "before consent", but the report's own tracker table shows them only after Accept; the only before-consent item is one Segment cookie from a Loom video on a blog page, which did not repeat in a clean reload | open |
| 8 | That cookie is listed under "Homepage", and "Highest-risk page: Homepage", though it came from a blog page | open |
| 9 | Policy analysis unavailable; text says "model could not be reached (LLMOutputValidationError)". The real cause is in section 13 | open |
| 10 | `${page.onThe}` printed literally in Section 14 | open (a template bug) |
| 11 | A blog article is read as the "Cookie policy" | open |
| 12 | "4 further checks do not apply" vs "0 not applicable"; privacy page "named, not loaded" while it is in the scanned list | open |
| 13 | 246 near-identical forms take 35 pages; 169 requests with long signed URLs take 11; the report could group them | open |
| 14 | The first page still shows the free-scan call to action ("Improve My Score with Consiva AI") | open |
| 15 | The section "Five-page DPDP readiness review" still reads five pages | open, by design of that section |

---

## 13. The Claude key problem (the real reason policy checks are "not assessed")

- The analysis calls Claude (`claude-haiku-5-5`) first and falls back to NVIDIA (`nemotron-3.5-lightning-30b`).
- Claude worked: 182 successful calls up to 10 Nov 23:28.
- From 12 Nov 11:05 every Claude call fails with `AuthenticationError`. On 13 Nov the key was checked against Anthropic's free model-list endpoint: **HTTP 401 `invalid x-api-key`**. The key is no longer valid. This is a credential problem, not a code problem.
- The NVIDIA fallback produces findings but cannot return the structured policy extraction (`LLMOutputValidationError`, 3 of 3 attempts).
- Effect: checks 7, 8, 9 and R9, R14A, R14B are "not assessed", so coverage is 70% and the score 69 is based on 9 of 12 checks. With a working key these checks will be assessed and the score can change.
- Needed from the user: a valid key from console.anthropic.com placed in `backend/.env` as `ANTHROPIC_API_KEY`. (A claude.ai or Claude Code subscription is not an API key.) The assistant was blocked from searching other env files for a working key and did not work around that.

---

## 14. Not built, and decisions still needed

| Item | Why not yet |
|---|---|
| Enterprise scoring (DPDP score, technical score, evidence confidence) | needs an approved formula and weights; none was invented |
| Findings from Layer B evidence | each finding must map to a legal provision; needs legal sign-off (GR-24) |
| Full DPDP registry (every section, rule and clause) | only 38 provisions are loaded; full gazetted text and sign-off are needed; `official_text` is empty |
| GDPR | the user said to leave it for now |
| OCR for scanned PDFs | needs a new dependency and an ARM64 build check on the GB10 |
| Reading PDF text in Layer A | the existing scanner only skips files with an honest label; the reader exists in Layer B |
| Separate worker pool for Layer B | designed (own job type), not configured |
| "Seen on N pages" for cookies | optional; the count itself is already correct |
| Removing Layer B or linking its screen | the user has not decided |

---

## 15. Running it

- Start: Enterprise tab, address, tick, **Scan Whole Website**. Download with **Download Detailed Report (PDF)**.
- Turn Layer B off: set `ENTERPRISE_ENABLED=false` in `backend/.env`, then `docker compose -f docker-compose.prod.yml up -d --no-deps backend worker`. Layer A is not affected.
- Require DNS verification again for whole-site scans: set `CONSENT_APP_REQUIRE_VERIFIED_DOMAIN=true` and restart the same two services.
- Deploy code: `docker compose -f docker-compose.prod.yml build backend frontend`, then `up -d --no-deps --wait backend worker frontend`. Migrations run through the `migrate` service.
- Database rollback: the migrations only add; to undo, restore `pre_enterprise.dump` or drop the `ent_*` tables and the two triggers. Do not do this while Layer B scans exist.
