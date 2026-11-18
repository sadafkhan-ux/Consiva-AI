# Consent (Five-Page) Scanner: every step from the URL to the score and the PDF

What happens, in order, when someone enters a URL, which API is called at each step, and which code does the work. Checked against the running system on 17 Nov 2026 (the live checks are marked **verified**; anything not checked is marked **not verified**).

All API paths start with `/api/v1`.

## 0. The whole flow on one page

```
Person or website                 API                                   Backend and worker                         Result
-------------------------------------------------------------------------------------------------------------------------------
1  Log in / use a service key     POST /auth/login                      token (12 h) or csv_ key
2  Enter URL + "I am authorised"  POST /consent/scans                   checks, scan row, job queued                scan id, status "pending"
                                  (website: POST /consent-agent/scans)
3  Wait                           GET  /consent/scans/{id}  + /stages   worker crawls up to 5 pages in a browser    status "running" -> "completed"
                                  (website: GET /consent-agent/scans/{id}/status)
4  Analysis                       POST /consent/scans/{id}/analyze      rules, legal search (RAG), Claude reads     policy_analysis "done"
                                  (website: automatic, nothing to call) the privacy notice, findings
5  Score and the 12 checks        GET  /consent/scans/{id}/assessment   computed from the stored evidence           score 0-100 + 12 rows
                                  (website: GET /consent-agent/scans/{id}/assessment)
6  Details                        findings, evidence, requirements, policy-analysis, screenshot, audit, stages
7  PDF report                     GET  /consent/scans/{id}/report.pdf   headless Chromium prints the report page    PDF file
                                  (website: GET /consent-agent/scans/{id}/report.pdf)
8  Clean-up                       (automatic)                           scan data deleted 15 minutes after the end  only the scan's own row stays
```

## 1. Two doors to the same scanner

| | Console door | Website (integration) door |
|---|---|---|
| Base path | `/api/v1/consent/scans/...` (19 routes) | `/api/v1/consent-agent/scans/...` (17 routes) |
| Who | The Consiva console (React) | The Consiva.io website's server |
| Token | Login token only (`POST /auth/login`, 12 hours) | Login token **or** a service key (`csv_...`, scope `consent:scan`) |
| Analysis | You call `POST .../analyze` yourself | Runs by itself inside the same job |
| PDF and score | Yes | **Yes (added 17 Nov 2026; the same handlers, not a copy)** |

Both doors call the same `scan_service.request_scan`, the same worker and the same scoring code. One scan has one score, whichever door is used.

A service key (`csv_...`) is created once by an admin: `POST /ropa/integration-keys` with `{"name": "...", "scopes": ["consent:scan"]}`. The response `api_key` is shown only once. Whole-site (application) scans need the extra scope `consent:scan:application`.

## 2. Step by step

### Step 1: Log in
- **API:** `POST /auth/login` with `{"email", "password"}` returns `access_token` (valid 12 hours). A website uses a service key instead and skips this step.
- **Code:** `api/v1/routes/auth.py`, `core/security.py` (`get_current_user`), `core/integration_auth.py` (`get_caller`).

### Step 2: Start the scan
- **APIs:**
  - Console: `POST /consent/scans` with `{"url": "https://example.com", "authorized": true}`.
  - Website: `POST /consent-agent/scans` (or `/scans/web`) with `{"website_url": "https://example.com", "authorized": true}`. Optional `Idempotency-Key` header.
  - Whole site (admin, no page cap): `POST /consent-agent/scans/application` with `{"website_url", "authorized", "scan_options": {...}}`.
- **What the backend checks, in order** (`services/scan_service.py: request_scan`):
  1. `authorized` must be true (otherwise **403** "Scanning requires an explicit authorization attestation for this domain.").
  2. The URL must be absolute with a real host (otherwise **422**).
  3. The address must be safe to fetch: private, loopback and internal addresses are refused (`scanner/url_safety.py`).
  4. Daily limit per organisation (`SCANNER_MAX_SCANS_PER_ORG_PER_DAY`; 0 means no limit, scans just queue).
  5. DNS ownership verification only when the deployment requires it (`SCANNER_REQUIRE_DOMAIN_VERIFICATION`).
- **What it writes:** a website row (host without `www.`), a scan row with status `pending`, an audit entry, and one background job (`scan` for the console, `consent_api_chain` for the website door, which later also runs the analysis).
- **Answer:** **202** with the scan id. Console: `{"id","url","domain","status":"pending"}`. Website: `{"scan_id","status","message","profile","max_pages","max_seconds"}`.

### Step 3: The worker picks up the job
- A separate worker process (`jobs/worker.py`) takes jobs from the queue as the machine has room (CPU and memory governor). A job that is stuck "running" is requeued by a reaper only after its time budget (about 30 minutes plus margin for a web scan; 24 hours plus margin for a whole-site scan).
- Stage **`url_validation`** runs first and records the stage.

### Step 4: The crawl (stage `website_scan`, about 50 seconds for a normal site)
Playwright with Chromium runs several passes (`scanner/crawler.py`):
1. **Pre-consent pass.** A fresh browser session that clicks nothing. It loads up to **5 pages**, and everything seen is tagged "before consent": cookies, trackers, third-party requests, forms, scripts, the consent banner or consent tool, the privacy and cookie policy text. The robots.txt file and sitemap are read.
2. **Which 5 pages.** The entered address always comes first. Then pages whose address says privacy, cookies, terms, contact or grievance (found through the sitemap or links), and a page that collects visitor details (for example a login or contact form). Each page records why it was chosen.
3. **Accept pass and Reject pass.** Two separate fresh sessions on the homepage. One clicks an Accept control, the other a Reject control. The scanner records what cookies and trackers appear afterwards.
4. **Pre-consent re-check.** If trackers fired before consent, the pages are loaded again alone to confirm it was not noise.
5. **Withdrawal test.** After Accept, the scanner looks for a way to reopen the choices and withdraw (for example "Cookie Preferences", "revoke all") and checks what stops.
6. **TLS check.** The SSL certificate (valid, days left, issuer) and the TLS version (`scanner/tls_assessment.py`).
- **How the banner is judged (fixed 17 Nov):** a consent mechanism is reported only for a known consent tool, or for visible Accept or Reject controls. A footer "Cookie settings" link alone, a hidden dialog, a privacy-policy page whose container is named "privacy", or a pop-up about notifications or e-mails is not a banner.
- **If the site refuses the scanner** (403, access denied, bot protection): the scan ends `failed` with the reason, it is not retried pointlessly, and it produces no score. An error event is recorded (see Step 11).

### Step 5: Classify and store (stages `classification`, `data_structuring`)
- Cookies and trackers are classified (analytics, marketing, functional, unknown) from a catalogue; the consent tool is matched by script and global names.
- The evidence is stored in tables: `website_pages`, `consent_forms`, `cookies`, `trackers`, `third_party_services`, `policies`, `consent_signals`, `scan_screenshots`.
- The scan row becomes **`completed`**. For a swaransoft.com test this took 40 to 70 seconds, with 5 pages, all HTTP 200.

### Step 6: Wait and poll (what the screen or website calls)
- Console: `GET /consent/scans/{id}` (status, `evidence_counts`, `outcome`, `failure`) and `GET /consent/scans/{id}/stages` (12 rows).
- Website: `GET /consent-agent/scans/{id}/status` (`progress`, `pages_discovered`, `pages_scanned`, `current_stage`, `outcome`).
- **Status values:** `pending`, `running`, `completed`, `failed`, `cancelled`. Stop polling at the last three. Cancel: `POST /consent-agent/scans/{id}/cancel`.

### Step 7: Analysis
- **Console:** `POST /consent/scans/{id}/analyze` (returns 202). **Website door:** nothing to call; the same job runs it after the crawl.
- **Stages, in order** (`agents/consent_agent/graph.py`, `services/`):
  1. `normalize_evidence`: put the stored evidence into one standard shape.
  2. `rules_check`: the written DPDP rules decide what the evidence shows (for example trackers before consent).
  3. `rag_retrieval`: legal search. The question is turned into a vector (NVIDIA embedding model `nvidia/nemotron-3.5-embed-1b`, 1024 dimensions) and matched against 279 approved legal passages stored in Postgres with pgvector (DPDP Act and Rules). 
  4. `llm_analysis`: Claude (`claude-haiku-5-5`) reasons over the evidence plus the retrieved provisions.
  5. `output_validation`: the model's answer is checked against rules before it is trusted.
  6. `findings_generated`: findings are written (a rule-based path is used if the model path is not usable).
  7. `human_review_gate`: findings that need a person are marked for review.
  - `policy_analysis` runs in this phase too: Claude reads the **privacy notice text** and answers the notice checks (what data and purposes are listed, DPO or grievance contact, Data Principal rights). Without it, checks 7, 8 and 9 stay "Not Assessed" and the assessment shows `policy_analysis: "not_run"`.
- **If the Claude key is wrong** (HTTP 401) or the embedding model is retired (HTTP 410), these stages fail and the notice checks are not assessed. The health checks (Step 11) report it.

### Step 8: The score (computed when asked, never stored separately)
- **API:** `GET /consent/scans/{id}/assessment` (website: `GET /consent-agent/scans/{id}/assessment`). Same JSON from both. `final` is true once the scan is completed and the policy analysis is done or failed.
- **The 12 checks and their marks (total 100)** (`scoring/scorecard.py`, version 3.6.0):

| | Check | Marks |
|---|---|---|
| Cookie Consent (48) | P1 Consent banner shown on first visit | 8 |
| | P2 Accept and Reject offered with equal ease | 8 |
| | P3 No non-essential cookies or trackers before a choice | 12 |
| | P4 Accept triggers trackers; Reject blocks them | 12 |
| | P5 Consent can be withdrawn or changed at any time | 8 |
| Privacy Notice (36) | P6 Privacy notice published and reachable | 6 |
| | P7 Notice itemises data collected and specific purposes | 10 |
| | P8 DPO or grievance officer contact published | 10 |
| | P9 Data Principal rights and how to exercise them | 10 |
| Forms and Security (16) | P10 Forms have consent and a notice link, over HTTPS | 8 |
| | P11 SSL certificate valid for more than 30 days | 4 |
| | P12 Secure TLS version | 4 |

- **Each check:** pass = full marks, partial = half, fail = 0. "Not assessed" (the check could not be run) earns 0 marks but is not called a failure; "not applicable" is left out.
- **Score = the marks the website earned, out of 100** (`scorecard.py`: `score = int(awarded + 0.5)`). It is not divided by the marks in play, so checks that could not be assessed (for example P7 to P9 when the policy analysis did not run) lower the score. The score is shown whatever the coverage; coverage is reported beside it.
- **Bands:** 90 and above "Substantially addressed", 75 "Largely addressed", 50 "Partially addressed", below that "Needs attention".
- **Verified example:** swaransoft.com scored 94 (P5 partial 4 of 8, P12 partial 2 of 4, all others full). The same scan through both doors returned identical JSON.

### Step 9: The details
All read-only, `GET`, same shape on both doors (verified identical with a service key on 17 Nov):

| What | Console | Website door |
|---|---|---|
| Findings | `/consent/scans/{id}/findings` | `/consent-agent/scans/{id}/findings` (its own, shorter format) |
| Full result | `/consent/scans/{id}` | `/consent-agent/scans/{id}` (its own format) |
| Summary | | `/consent-agent/scans/{id}/summary` |
| Evidence | `/consent/scans/{id}/evidence` | `/consent-agent/scans/{id}/evidence` |
| DPDP requirements | `/consent/scans/{id}/requirements` | `/consent-agent/scans/{id}/requirements` |
| Policy analysis | `/consent/scans/{id}/policy-analysis` | `/consent-agent/scans/{id}/policy-analysis` |
| Screenshot | `/consent/scans/{id}/screenshot` | `/consent-agent/scans/{id}/screenshot` |
| Stages | `/consent/scans/{id}/stages` | `/consent-agent/scans/{id}/stages` |
| Audit trail | `/consent/scans/{id}/audit` | `/consent-agent/scans/{id}/audit` |

Only on the console, by design (they change a scan, so a person's login token is required): `POST .../analyze`, report approval (`GET/POST/DELETE .../report-approval`), finding review (`POST /consent/findings/{id}/approve|reject|edit`), usage and scan lists, URL inventory, monitoring schedule.

### Step 10: The PDF report
- **API:** `GET /consent/scans/{id}/report.pdf?variant=<v>` (website: `GET /consent-agent/scans/{id}/report.pdf?variant=<v>`). Returns `application/pdf` with a file name like `consiva-privacy-report-<domain>-<date>[-variant].pdf`.
- **Variants:** `summary` (server default, two pages), `long` (what the console asks for: the full evidence report), `free` (two-page Free report), `full`.
- **How it is made** (`services/report_pdf_service.py`): headless Chromium opens the same report page the web shows (`REPORT_FRONTEND_URL`, `http://frontend:8080/?report=<id>`), signed in with the caller's own token. The page reads the 9 results routes (scan, stages, findings, audit, evidence, requirements, policy-analysis, screenshot, assessment) and prints itself to PDF. A stored copy is reused while it is still exactly what would be printed. Nothing is minted or elevated.
- **Errors:** 401 no token, 404 unknown scan or another organisation's, 409 the scan is not completed, 422 unknown variant, 503 the PDF could not be made (the scan is unaffected).
- **Release gate:** when `REPORT_REQUIRE_APPROVAL=true`, only an approved report can be downloaded (an admin may always download to review). It is false now.
- **Verified 17 Nov:** all four variants return `%PDF` through a service key on `/consent-agent` (summary 937 KB, free 236 KB, long 1.8 MB, full 1.7 MB), and the console route is unchanged.

### Step 11: Errors, blocked sites and monitoring
- Every API error of 400 and above, every failed job and every health problem becomes an **error event** (`error_events`): category, severity, agent, scan id, website, status Open, Acknowledged or Resolved. Normal mistakes (400, 401, 404, 409, 422) are recorded as information and never emailed. Critical ones are emailed once, then reminded, with a recovery email. Email needs the mail server settings (`ALERT_SMTP_*`, `ALERT_EMAIL_TO`); without them the event shows "not configured".
- **Website blocked the scanner:** the scan fails with "Website blocked our scanner." and no score; it is shown as Blocked on the dashboard. The scanner never tries to get around a CAPTCHA or firewall.
- Health checks run every 15 minutes: database, Claude key, NVIDIA key, embedding model (with `dimensions`), vector database, job queue, failing AI calls.
- Screens: System health, Error monitoring (admin). APIs: `/system/monitoring/...` (internal, admin only).

### Step 12: Clean-up (automatic)
- A cleanup service deletes the scan's data **15 minutes after it ends** (`CLEANUP_GRACE_MINUTES`): results, evidence, findings, screenshots and stored PDFs. Only the scan's own row stays (id, URL, status, error, times). So fetch the results and the PDF within 15 minutes.

## 3. All APIs of the scan, by step

| Step | Console door | Website door |
|---|---|---|
| 1 | `POST /auth/login` | service key |
| 2 | `POST /consent/scans` | `POST /consent-agent/scans` or `/scans/web`; `/scans/application` (whole site) |
| 3 | `GET /consent/scans/{id}`, `/stages` | `GET /consent-agent/scans/{id}/status`, `/stages` |
| 4 | `POST /consent/scans/{id}/analyze` | automatic |
| 5 | `GET /consent/scans/{id}/assessment` | `GET /consent-agent/scans/{id}/assessment` |
| 6 | findings, evidence, requirements, policy-analysis, screenshot, audit | the same names under `/consent-agent/scans/{id}/` |
| 7 | `GET /consent/scans/{id}/report.pdf?variant=` | `GET /consent-agent/scans/{id}/report.pdf?variant=` |
| other | `GET /consent/scans` (list) | `GET /consent-agent/scans` (list), `POST /consent-agent/scans/{id}/cancel` |

## 4. What changed on 17 Nov 2026

1. **One door for the website.** `/consent-agent` now also serves the score, stages, evidence, requirements, policy-analysis, screenshot, audit and the PDF, through the console's own handlers. The 19 console routes keep their paths and answers.
2. **Service key on the print-page reads.** The ten read routes the PDF page uses (scan, stages, findings, evidence, requirements, policy-analysis, screenshot, assessment, audit, PDF) now accept a service key with `consent:scan` as well as a login token. A login token is checked by exactly the same function as before. Start, analyze, approvals, usage and lists stay login-only (verified: a key gets 401 there).
3. **Banner detection fixes** (frozen scanner, owner-approved, logged in `backend/frozen/CHANGES.md`): a lone "Cookie settings" link, a hidden dialog, a policy page named "privacy", and notification or e-mail "Accept all" pop-ups are no longer reported as a cookie banner.
4. **Embedding model** switched to `nvidia/nemotron-3.5-embed-1b` (1024 dimensions) because the old one was retired (HTTP 410); the 279 legal passages were re-embedded.
5. **Error monitoring** and bot-block detection added (Step 11). No response format of the scan APIs was changed by it.

## 5. Known points

- **After the backend container is recreated, the gateway can keep the old backend address** and answer 502 (seen three times on 17 Nov, also making the PDF time out because its page could not reach the API). Fix: `docker compose -f docker-compose.prod.yml exec frontend nginx -s reload`. Not fixed permanently; the gateway config for `/docs`, `/openapi.json` and `/health` uses a fixed address, and `/api/` failed to follow the change as well.
- **The other environment** that makes your customers' PDFs needs the same code, the new Claude key and the embedding change. Until then its checks 7 to 9 can show "Not Assessed".
- **Not verified:** a whole-site (application) scan through the new routes, webhooks (`webhook_url`, `callback_url`), a live scan of a site that blocks bots (five-page), and the Consiva.io website's own code (not on this machine).
