# Privacy Policy Agent: all APIs from scan start to the results

Base URL: `https://consiva-agent.gignati.com`

Every step shows the **console** API (`/api/v1/consent/...`) and the **website** API (`/api/v1/consent-agent/...`). Every path below is written in full, starting with `/api/v1`.

Auth: `Authorization: Bearer <token>`. Console: a login token. Website: a service key (`csv_...`, scope `consent:scan`) or a login token. Where a console route is marked "login only", a service key gets 401.
Errors: console `{"detail": "<message>"}`; website `{"error": {"code": "<CODE>", "message": "<text>", "scan_id": "<id>"}}`. Both: missing or invalid token `401 {"detail": "Missing bearer token"}`.

---

## The Consiva.io scan result page: the APIs it calls

The page reaches these through Consiva's own proxy (`/api/v1/public/consent-agent`), which forwards to the routes below. Each route exists on this API with the path shown.

| Order | Call | What it feeds on the page |
|---|---|---|
| 1 | `POST /api/v1/consent-agent/scans` | starts the scan (Step 2) |
| 2 | `GET /api/v1/consent-agent/scans/{scan_id}/status` | progress; "Pages found" (`pages_discovered`) and "Pages scanned" (`pages_scanned`) |
| 3 | `GET /api/v1/consent/scans/{scan_id}/stages` | Security signals, Indian-language support, pages count (the `website_scan` stage `metadata`) |
| 4 | `GET /api/v1/consent/scans/{scan_id}/evidence` | Tracking technologies, Privacy elements, Forms found |
| 5 | `GET /api/v1/consent/scans/{scan_id}/screenshot` | homepage screenshot |
| 6 | `GET /api/v1/consent/scans/{scan_id}/assessment` | the score, the Met / Partially met / Not met / Unable to verify counts, Top findings |
| 7 (saved, not shown) | `GET /api/v1/consent-agent/scans/{scan_id}`, `GET /api/v1/consent/scans/{scan_id}/audit`, `GET /api/v1/consent-agent/scans/{scan_id}/summary` | the saved report |

Notes for this team:
- Calls 3 to 6 and the audit call use `/api/v1/consent/...` routes. Until 17 Nov 2026 those took a login token only; they now also accept a service key (`csv_...`, scope `consent:scan`). A login token still works.
- The same five results are also served under `/api/v1/consent-agent/scans/{scan_id}/stages`, `/evidence`, `/screenshot`, `/assessment` and `/audit`, with identical JSON. Using them is optional.
- `POST /api/v1/website-scan-report` belongs to Consiva's own API, not to this one.

---

## Step 1. Sign in
**Console:** `POST /api/v1/auth/login` (website: uses a service key, no call).
Request: `{"email": "string", "password": "string"}`
Response 200: `{"access_token": "string", "expires_in": number, "org_id": "uuid", "user_id": "uuid", "role": "string"}` (token valid 12 hours).
Errors: 401 `{"detail": "Invalid email or password"}`.

## Step 2. Start the scan
**Console:** `POST /api/v1/consent/scans` (login only). **Website:** `POST /api/v1/consent-agent/scans` (or `POST /api/v1/consent-agent/scans/web`). Whole site, no page cap: `POST /api/v1/consent-agent/scans/application` (admin, or scope `consent:scan:application`).
Headers: `Content-Type: application/json`; website also takes optional `Idempotency-Key: <string>`.
Request, console: `{"url": "https://example.com", "authorized": true}`
Request, website: `{"website_url": "https://example.com", "authorized": true}` (optional `scan_options`: `max_pages` (can only lower 5), `max_depth`, `max_urls`, `concurrency`, `per_domain_concurrency`, `requests_per_second`, `memory_budget_mb`, `scan_consent_states`, `scan_before_consent`, `scan_after_accept`, `scan_after_reject`; optional `webhook_url`, `callback_url`, `callback_secret`)
Response 202, console: `{"id": "c950acef-c7b3-4d10-bd41-f0c3251b5f8f", "url": "https://swaransoft.com/", "domain": "swaransoft.com", "status": "pending"}`
Response 202, website: `{"scan_id": "b1429afa-9236-4219-96c8-122a83c413ed", "status": "queued", "message": "Scan queued successfully", "idempotent_replay": false, "profile": "web", "max_pages": 5, "max_seconds": null}`
Errors: 401; 403 (`authorized` is not true; website code `NOT_AUTHORIZED`); 422 (invalid or missing field; website code `VALIDATION_ERROR`); 429 (rate limit; website code `RATE_LIMIT_EXCEEDED`); website only: 400 `INVALID_URL`, 501 `WEBHOOK_NOT_CONFIGURED`, 403 `APPLICATION_SCAN_NOT_PERMITTED`.

## Step 3. Follow the progress
**Console:** `GET /api/v1/consent/scans/{scan_id}` and `GET /api/v1/consent/scans/{scan_id}/stages`. **Website:** `GET /api/v1/consent-agent/scans/{scan_id}/status` and `GET /api/v1/consent-agent/scans/{scan_id}/stages`.
Status values: console `pending`, `running`, `completed`, `failed`, `cancelled`; website `queued`, `running`, `completed`, `failed`, `cancelled`. Stop at `completed`, `failed` or `cancelled`.
Response 200, console scan: `{"id": "uuid", "url": "https://swaransoft.com/", "domain": "swaransoft.com", "status": "completed", "error": null, "evidence_counts": {"pages": 5, "forms": 7, "cookies": 3, "trackers": 49, "third_party_services": 6, "policies": 3}, "analysis_status": null, "findings_complete": false, "outcome": "COMPLETED", "failure": null}`
Response 200, website status: `{"scan_id": "uuid", "website_url": "https://swaransoft.com/", "status": "completed", "progress": 100, "pages_discovered": 119, "pages_scanned": 5, "pages_failed": 0, "current_stage": "human_review_gate", "started_at": "datetime", "completed_at": "datetime", "error": null, "outcome": "COMPLETED", "failure": null}`
Response 200, stages (both): list of `{"stage": "url_validation", "status": "completed", "duration_ms": 2, "error": null, "metadata": {...}, "started_at": "datetime", "completed_at": "datetime"}`. The 12 stages: `url_validation`, `website_scan`, `classification`, `data_structuring`, `normalize_evidence`, `rules_check`, `rag_retrieval`, `llm_analysis`, `policy_analysis`, `output_validation`, `findings_generated`, `human_review_gate`.
Errors: 401; 404 (unknown scan; website code `SCAN_NOT_FOUND`); 422.

## Step 4. Analysis (privacy notice, rules, legal search, AI)
**Console:** `POST /api/v1/consent/scans/{scan_id}/analyze` (login only). Call it after the scan is `completed`.
**Website:** nothing to call. The analysis runs by itself after the crawl.
Response 202: `{"id": "7e715e52-d5f5-4a5c-ae6b-c5dcce2df88e", "status": "pending"}`
Errors: 401; 404; 409 (scan not completed yet, or an analysis is already running); 422.

## Step 5. The score and the 12 checks
**Console:** `GET /api/v1/consent/scans/{scan_id}/assessment`. **Website:** `GET /api/v1/consent-agent/scans/{scan_id}/assessment`. The same JSON from both.
Response 200 fields: `scan_id`, `methodology_version`, `scan_status`, `policy_analysis` (`not_run`, `pending`, `running`, `done`, `failed`), `final` (boolean), `score` (integer or null), `score_text`, `total_text`, `awarded`, `applicable_max`, `counts`, `incomplete`, `limited_basis`, `summary`, `formula`, `checks`, `recommendations`, `pages`, `limitations`, `engine`, `scanner`.
```json
{ "scan_id": "uuid", "methodology_version": "scorecard-3.6.0", "scan_status": "completed", "policy_analysis": "done", "final": true, "score": 94, "score_text": "94/100", "awarded": 94, "applicable_max": 100,
  "counts": { "passed": 10, "partial": 2, "failed": 0, "not_assessed": 0, "not_applicable": 0, "beyond_scan_type": 0 }, "incomplete": 0,
  "checks": [ { "id": 1, "ref": "P1", "category": "Cookie Consent", "name": "Consent banner shown on first visit", "result": "Passed", "awarded": 8, "max": 8, "marks_text": "8 of 8 pts", "evidence": "Consent mechanism detected on the home page (Consiva Consent SDK)", "source_url": "https://swaransoft.com/", "explanation": "A consent banner was shown on first visit and on every inner page read.", "legal": "DPDP Act s.4(1), s.6(1)", "completed": true, "limitation": null, "basis": "Verified", "quality": "Specific" } ] }
```
Check `result`: `Passed`, `Partially Passed`, `Failed`, `Not Assessed`.
Errors: 401; 404; 422.

## Step 6. Findings
**Console:** `GET /api/v1/consent/scans/{scan_id}/findings`. **Website:** `GET /api/v1/consent-agent/scans/{scan_id}/findings`.
Response 200, console: list of `{"id": "uuid", "category": "analytics", "risk_level": "medium", "priority": "high", "finding_text": "string", "evidence": ["sig1", "t8"], "dpdp_reference": [], "requires_human_review": true, "status": "pending", "recommendations": [{"id": "uuid", "recommendation_text": "string", "priority": null}]}`
Response 200, website: `{"scan_id": "uuid", "findings": [ {"id", "severity" (high|medium|low), "category", "title", "description", "evidence", "confidence", "status", "requires_human_review", "recommendation", "dpdp_references", "created_at"} ]}`
Errors: 401; 404; 422.

## Step 7. The evidence behind the score
**Console:** `GET /api/v1/consent/scans/{scan_id}/evidence`. **Website:** `GET /api/v1/consent-agent/scans/{scan_id}/evidence`.
Response 200 fields: `pages`, `forms`, `cookies`, `trackers`, `third_party_services`, `policies`, `consent_signals`.

**Console:** `GET /api/v1/consent/scans/{scan_id}/url-inventory` (login only; every URL the crawl found). Query (optional): `status`, `http_status`, `source`, `depth`, `q`, `failure`, `limit`, `offset`.
Response 200: `{"summary": {"total": 119, "pending": 114, "in_progress": 0, "completed": 5, "failed": 0, "partially_scanned": 0, "excluded": 0}, "urls": [ {"id", "domain", "original_url", "normalized_url", "final_url", "page_title", "http_status", "content_type", "discovery_source", "discovery_sources", "crawl_depth", "scan_status", "status_reason", "failure_reason", "discovered_at", ...} ]}`
Errors: 401; 404; 422.

## Step 8. DPDP requirements and the privacy notice reading
**Console:** `GET /api/v1/consent/scans/{scan_id}/requirements` and `GET /api/v1/consent/scans/{scan_id}/policy-analysis`. **Website:** `GET /api/v1/consent-agent/scans/{scan_id}/requirements` and `GET /api/v1/consent-agent/scans/{scan_id}/policy-analysis`.
Requirements query (all optional): `as_of` (date), `processes_children_data`, `significant_data_fiduciary`, `consent_manager`, `erasure_class` (boolean), `business_sector` (string).
Response 200, requirements: `requirements_version`, `as_of`, `requirements`, `disclaimer`, `status_counts`, `evidence_summary`, `breakdown`, `human_review`, `finding_groups`, `methodology`, `assessment`, `remediation_plan`, `ledger`, `sector`, `out_of_scope`.
Response 200, policy-analysis: `status`, `reason`, `sector`, `documents`, `notes`, `summary`, `requirements`, `limitations`, `model`.
Errors: 401; 404; 422.

## Step 9. Homepage screenshot and audit trail
**Console:** `GET /api/v1/consent/scans/{scan_id}/screenshot` and `GET /api/v1/consent/scans/{scan_id}/audit`. **Website:** `GET /api/v1/consent-agent/scans/{scan_id}/screenshot` and `GET /api/v1/consent-agent/scans/{scan_id}/audit`.
Response 200, screenshot: `{"scan_id": "uuid", "domain": "swaransoft.com", "status": "ok", "available": true, "message": null, "page_url": "https://swaransoft.com/", "mime": "image/jpeg", "width": 1280, "height": 720, "captured_at": "datetime", "data": "<base64 image>"}`
Response 200, audit: list of `{"id": "uuid", "action": "scan.created", "entity_type": "consent_scan", "entity_id": "uuid", "actor_user_id": "uuid", "agent_run_id": "uuid|null", "model_name": "string|null", "provider": "string|null"}` (more fields may follow).
Errors: 401; 404; 422.

## Step 10. Summary and the full result (website)
`GET /api/v1/consent-agent/scans/{scan_id}/summary`:
`{"scan_id": "uuid", "website": "https://swaransoft.com/", "status": "completed", "pages_scanned": 5, "cookies_detected": 3, "trackers_detected": 49, "cmp_detected": true, "cmp_vendor": "Consiva Consent SDK", "cmp_confidence": 0.95, "consent_states_tested": {"before": true, "accept": true, "reject": true}, "findings": {"critical": 0, "high": 0, "medium": 1, "low": 0}, "compliance_status": "review_required"}` (`compliance_status`: `compliant`, `review_required`, `issues_found`, `unknown`).
`GET /api/v1/consent-agent/scans/{scan_id}`: `scan_id`, `website_url`, `status`, `scanner_version`, `scan_options`, `started_at`, `completed_at`, `duration_ms`, `page_stats`, `consent_mechanism`, `consent_states`, `evidence_counts`, `cookies`, `trackers`, `third_party_services`, `forms`, `policies`, `findings`, `stages`, `token_metrics`, `errors`.
Errors: 401; 404 `SCAN_NOT_FOUND`; 422.

## Step 11. The PDF report
**Console:** `GET /api/v1/consent/scans/{scan_id}/report.pdf?variant=`. **Website:** `GET /api/v1/consent-agent/scans/{scan_id}/report.pdf?variant=`.
Query: `variant` = `summary` (default, two pages), `long` (the full evidence report), `free`, `full`. Header: optional `Accept: application/pdf`.
Response 200 `application/pdf`. Headers: `Content-Disposition: attachment; filename="consiva-privacy-report-<domain>-<date>[-variant].pdf"`, `Cache-Control: private, no-store`.
Errors: 401; 404; 409 (scan not completed, or the report is not approved when `REPORT_REQUIRE_APPROVAL` is on; website code `SCAN_NOT_READY`); 422 (unknown `variant`); 503 (the PDF could not be made; retry).


---

## Field mapping: which API field feeds which part of a result page

`GET /api/v1/consent/scans/{scan_id}/evidence` (or `GET /api/v1/consent-agent/scans/{scan_id}/evidence`) always returns exactly **one** row in `consent_signals`, even when no consent banner was found. A row being present does not mean a banner exists. Read `consent_signals[0].mechanism_type`.

Example for a site with no banner:
```json
"consent_signals": [ { "mechanism_type": "none", "cmp_vendor": null, "has_reject_all": null, "has_granular_choices": null,
  "evidence": { "confidence": 0.0, "detection_source": null, "accept_interaction": "cmp_not_found", "reject_interaction": "cmp_not_found" } } ]
```

| Part of the page | Read this field | Rule |
|---|---|---|
| Consent banner found? | `evidence.consent_signals[0].mechanism_type` (or `GET /api/v1/consent-agent/scans/{scan_id}/summary` field `cmp_detected`) | `"banner"` or `"cmp"` means found. `"none"` means **not found** (and `cmp_detected` is `false`) |
| Accept option, Reject option | `consent_signals[0].evidence.accept_interaction`, `.reject_interaction` | Show these two only when the banner was found. When `mechanism_type` is `"none"`, hide both |
| What an interaction value means | same fields | `clicked` the control was clicked; `click_failed` found but could not be clicked; `cmp_not_automatable` a consent tool was found but could not be operated; `cmp_not_found` there was no banner to click |
| Per-purpose choices | `consent_signals[0].has_granular_choices` | `true` offered, `false` not offered, `null` not known |
| Reject-all option | `consent_signals[0].has_reject_all` | `true`, `false`, `null` (not known) |
| Privacy policy, Terms of use, Cookie policy | `evidence.policies[]` with `policy_type` `privacy_policy`, `terms`, `cookie_policy` | present in the list means found |
| Forms found | `evidence.forms[]` | length |
| Tracking technologies | `evidence.trackers[]` | one entry per request; group by `vendor` and show `category` |
| Score | `GET /api/v1/consent/scans/{scan_id}/assessment` field `score` | whole number out of 100 (for example `15` means 15/100) |
| Met / Partially met / Not met / Unable to verify | `assessment.counts.passed`, `.partial`, `.failed`, `.not_assessed` | |
| Top findings | `assessment.checks[]` where `result` is `Failed` | the check `name` and `explanation` |
| Number of findings by severity | `GET /api/v1/consent-agent/scans/{scan_id}/summary` field `findings` | counts of the AI and rules findings list. This is a different set from the 12 checks, so it will not match "Top findings" |
| Pages found / Pages scanned | `GET /api/v1/consent-agent/scans/{scan_id}/status` fields `pages_discovered` and `pages_scanned` | "found" is everything the crawler discovered (for example 119 or 527); "scanned" is at most 5 |
| Forms found | `evidence.forms[]` (length), or `stages[stage=website_scan].metadata.forms_found` | |
| Security signals | `stages[stage=website_scan].metadata.security_signals` | `pages_https` of `pages_total`, `homepage_https`, and `homepage_headers`: `hsts`, `csp`, `x_frame_options`, `x_content_type_options`, `referrer_policy`, `permissions_policy`. A value of `null` means not observed, not a failure |
| SSL certificate and TLS version | `stages[stage=website_scan].metadata.tls_signals` | `tls.version`, `certificate.status`, `certificate.days_remaining`, `certificate.issuer` |
| Indian-language support | `stages[stage=website_scan].metadata.language_signals` | `html_lang`, `hreflang` (list), `pages_with_indic_script`, `pages_checked`, `switcher_scripts` (scripts found in a language switcher), `indic_scripts` (counts per script) |
