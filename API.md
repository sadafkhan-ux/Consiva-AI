# Privacy Policy Agent API (integration)

The APIs a website calls to scan a site with the Privacy Policy Agent and get the score, the findings and the PDF report. Every route below was called against the running backend on 17 Nov 2026 with a real scan (swaransoft.com, 5 pages, score 94) unless it says "not verified". The examples are real responses; long lists and texts are cut and marked.

**Name.** This was called the "Consent Agent". It is now the **Privacy Policy Agent**. Only the label changed: the URL path is still `/api/v1/consent-agent/...`.

All paths start with `/api/v1/consent-agent`. Base URL: the gateway, `http://localhost:8100` here (production hosts are not recorded in this repo).

## 1. Authentication

Every call needs `Authorization: Bearer <service key>`. A service key looks like `csv_...`. It is created once by an admin and is shown only at creation:

1. Log in as the admin: `POST /api/v1/auth/login` with `{"email": "...", "password": "..."}`. Copy `access_token` (valid 12 hours).
2. Create the key: `POST /api/v1/ropa/integration-keys` with header `Authorization: Bearer <access_token>` and body `{"name": "consiva-io", "scopes": ["consent:scan"]}`.
3. Copy `api_key` from the response and keep it in the website server's secrets. Never put it in browser code.

A login token also works on these routes. The key needs the scope `consent:scan`. The whole-site route (`/scans/application`) needs the extra scope `consent:scan:application`.

## 2. The four calls that matter

```
1  POST /scans                          start the scan            -> scan_id
2  GET  /scans/{scan_id}/status         poll until "completed"    (about 40 to 70 seconds for a normal site)
3  GET  /scans/{scan_id}/assessment     the score and the 12 checks
4  GET  /scans/{scan_id}/report.pdf     the PDF report
```

The analysis (rules, legal search, AI reading of the privacy notice) runs by itself after the crawl. You do not call anything to start it.

**Important:** scan data (results, findings, evidence, screenshot and the stored PDF) is deleted **15 minutes** after the scan ends. Fetch the score, the findings and the PDF within that time and store them on your side.

## 3. All routes

| Method and path | What it returns | Verified |
|---|---|---|
| `POST /scans` | start a scan (web profile, up to 5 pages) | yes |
| `POST /scans/web` | same, explicit web profile | not verified |
| `POST /scans/application` | whole-site scan, no page cap (admin, or scope `consent:scan:application`) | refusal shape only; a scan was not started |
| `GET /scans` | list the organisation's scans | yes |
| `GET /scans/{scan_id}/status` | status and progress | yes |
| `GET /scans/{scan_id}` | the full result (evidence, findings, stages) | yes |
| `GET /scans/{scan_id}/summary` | short dashboard summary | yes |
| `GET /scans/{scan_id}/assessment` | **score and the 12 checks** | yes |
| `GET /scans/{scan_id}/findings` | findings with evidence and recommendations | yes |
| `GET /scans/{scan_id}/report.pdf?variant=` | **the PDF report** | yes (all four variants) |
| `GET /scans/{scan_id}/stages` | the 12 pipeline stages and their status | yes |
| `GET /scans/{scan_id}/evidence` | pages, forms, cookies, trackers, third-party services, policies, consent signals | yes |
| `GET /scans/{scan_id}/requirements` | the DPDP requirements reading | yes |
| `GET /scans/{scan_id}/policy-analysis` | the reading of the privacy notice | yes |
| `GET /scans/{scan_id}/screenshot` | homepage screenshot | yes |
| `GET /scans/{scan_id}/audit` | audit trail | yes |
| `POST /scans/{scan_id}/cancel` | cancel a queued or running scan | yes |

The last seven read routes return the same JSON as the console's matching routes (checked identical for assessment, stages, evidence, policy-analysis, requirements, screenshot and audit).

## 4. Start a scan: `POST /scans`

**Headers:** `Authorization: Bearer <key>`, `Content-Type: application/json`. Optional `Idempotency-Key: <string>`: a retry with the same key returns the same scan instead of starting a second one (`idempotent_replay: true`).

**Body**

| Field | Type | Required | Note |
|---|---|---|---|
| `website_url` | string | yes | absolute `http://` or `https://` address |
| `authorized` | boolean | **yes** | must be `true`: you attest that you own the domain or may scan it. Without it the call is refused (403) |
| `scan_options` | object | no | `max_pages` can only lower the 5-page limit, never raise it. Other fields are for whole-site scans |
| `webhook_url`, `callback_url`, `callback_secret` | string | no | webhooks: not verified, and refused with 501 where the deployment has no signing secret |

```json
{ "website_url": "https://swaransoft.com/", "authorized": true }
```

**Response `202`** (real)

```json
{
  "scan_id": "b1429afa-9236-4219-96c8-122a83c413ed",
  "status": "queued",
  "message": "Scan queued successfully",
  "idempotent_replay": false,
  "profile": "web",
  "max_pages": 5,
  "max_seconds": null
}
```

## 5. Status: `GET /scans/{scan_id}/status`

`status` is one of `queued`, `running`, `completed`, `failed`, `cancelled`. Poll every few seconds and stop at the last three.

**Response `200`** (real, a finished scan)

```json
{
  "scan_id": "c950acef-c7b3-4d10-bd41-f0c3251b5f8f",
  "website_url": "https://swaransoft.com/",
  "status": "completed",
  "progress": 100,
  "pages_discovered": 119,
  "pages_scanned": 5,
  "pages_failed": 0,
  "current_stage": "human_review_gate",
  "started_at": "2026-11-17T06:31:19.483886Z",
  "completed_at": "2026-11-17T06:32:09.554789Z",
  "error": null,
  "outcome": "COMPLETED",
  "failure": null
}
```

A `failed` scan has the reason in `error` (and `failure`). A site that refuses the scanner (access denied, bot protection) ends `failed` with that reason and has no score.

## 6. The score: `GET /scans/{scan_id}/assessment`

Ask after `status` is `completed`. The score is final when `final` is `true` and `policy_analysis` is `done`. If `policy_analysis` is `not_run`, `pending` or `running`, wait a few seconds and ask again (checks P7 to P9 need it).

**Response `200`** (real, trimmed). Top-level fields: `scan_id`, `methodology_version`, `scan_status`, `policy_analysis`, `final`, `score`, `score_text`, `total_text`, `awarded`, `applicable_max`, `counts`, `incomplete`, `limited_basis`, `summary`, `formula`, `checks`, `recommendations`, `pages`, `limitations`, `engine`, `scanner`.

```json
{
  "scan_id": "c950acef-c7b3-4d10-bd41-f0c3251b5f8f",
  "methodology_version": "scorecard-3.4.0",
  "scan_status": "completed",
  "policy_analysis": "done",
  "final": true,
  "score": 94,
  "score_text": "94/100",
  "awarded": 94,
  "applicable_max": 100,
  "counts": { "passed": 10, "partial": 2, "failed": 0, "not_assessed": 0, "not_applicable": 0, "beyond_scan_type": 0 },
  "incomplete": 0,
  "checks": [
    {
      "id": 1, "ref": "P1", "category": "Cookie Consent",
      "name": "Consent banner shown on first visit",
      "result": "Passed", "awarded": 8, "max": 8, "marks_text": "8 of 8 pts",
      "evidence": "Consent mechanism detected on the home page (Consiva Consent SDK)",
      "source_url": "https://swaransoft.com/",
      "explanation": "A consent banner was shown on first visit and on every inner page read.",
      "legal": "DPDP Act s.4(1), s.6(1)",
      "completed": true, "limitation": null, "basis": "Verified", "quality": "Specific"
    },
    "... 11 more checks (P2 to P12)"
  ]
}
```

**The 12 checks and their marks (total 100).** Cookie Consent (48): P1 banner shown on first visit 8, P2 Accept and Reject offered with equal ease 8, P3 no non-essential cookies or trackers before a choice 12, P4 Accept triggers trackers and Reject blocks them 12, P5 consent can be withdrawn 8. Privacy Notice (36): P6 notice published and reachable 6, P7 notice lists data and purposes 10, P8 DPO or grievance contact 10, P9 Data Principal rights 10. Forms and Security (16): P10 forms have consent and a notice link 8, P11 SSL certificate valid more than 30 days 4, P12 secure TLS version 4.

Each check's `result` is `Passed`, `Partially Passed`, `Failed` or `Not Assessed`. Pass is full marks, partial is half, fail is 0. A `Not Assessed` check is left out of the total, so a check that could not run is not counted as a failure. **Score = marks awarded / marks in play x 100.** Bands: 90 and above "Substantially addressed", 75 "Largely addressed", 50 "Partially addressed", below that "Needs attention".

## 7. The PDF: `GET /scans/{scan_id}/report.pdf?variant=`

Returns `application/pdf`. Headers: `Content-Disposition: attachment; filename="consiva-privacy-report-<domain>-<date>[-variant].pdf"`, `Cache-Control: private, no-store`. Ask after the analysis has finished, so checks P7 to P9 are in the report.

| `variant` | What it is | Real size |
|---|---|---|
| `summary` (default) | the two-page report | 935 KB |
| `long` | the full evidence report | 1.7 MB |
| `free` | the two-page Free Scan report | 236 KB |
| `full` | the full report | 1.7 MB |

The PDF is printed by a browser on the server, so the first request can take several seconds. The server gives up after 90 seconds and answers 503; retry. Send `Accept: application/pdf` if you like; it is optional.

## 8. Findings, summary and the full result

**`GET /scans/{scan_id}/findings`** returns `{"scan_id", "findings": [...]}`. Each finding has `id`, `severity` (`high`, `medium`, `low`), `category`, `title`, `description`, `evidence`, `confidence`, `status`, `requires_human_review`, `recommendation`, `dpdp_references`, `created_at`.

**`GET /scans/{scan_id}/summary`** (real)

```json
{
  "scan_id": "c950acef-c7b3-4d10-bd41-f0c3251b5f8f",
  "website": "https://swaransoft.com/",
  "status": "completed",
  "pages_scanned": 5,
  "cookies_detected": 3,
  "trackers_detected": 49,
  "cmp_detected": true,
  "cmp_vendor": "Consiva Consent SDK",
  "cmp_confidence": 0.95,
  "consent_states_tested": { "before": true, "accept": true, "reject": true },
  "findings": { "critical": 0, "high": 0, "medium": 1, "low": 0 },
  "compliance_status": "review_required"
}
```

`compliance_status` is `compliant`, `review_required`, `issues_found` or `unknown`. It is not the score: use `assessment` for the score.

**`GET /scans/{scan_id}`** returns the full result: `scan_id`, `website_url`, `status`, `scanner_version`, `scan_options`, `started_at`, `completed_at`, `duration_ms`, `page_stats`, `consent_mechanism`, `consent_states`, `evidence_counts`, `cookies`, `trackers`, `third_party_services`, `forms`, `policies`, `findings`, `stages`, `token_metrics`, `errors`.

## 9. List and cancel

**`GET /scans`** (query: `limit`, `offset`, `status`) returns `{"scans": [{"scan_id", "website_url", "status", "findings_count", "started_at", "completed_at", "created_at"}]}`.

**`POST /scans/{scan_id}/cancel`** returns `200` `{"scan_id", "status": "cancelled", "message"}` for a queued or running scan. A scan that is already completed, failed or cancelled answers 409 `SCAN_CONFLICT`.

## 10. Errors

Errors from this API have one shape:

```json
{ "error": { "code": "SCAN_NOT_FOUND", "message": "Scan 0000... not found", "scan_id": "0000..." } }
```

Branch on `code`; `message` is for people and may change wording. A request that fails validation has `code: "VALIDATION_ERROR"` and a `fields` list: `{"error": {"code": "VALIDATION_ERROR", "message": "The request body failed validation.", "fields": [{"field": "website_url", "message": "Field required"}]}}`.

| HTTP | `code` | When |
|---|---|---|
| 400 | `INVALID_URL` | the address cannot be parsed |
| 403 | `NOT_AUTHORIZED` | `authorized` is not `true` (verified live) |
| 403 | `APPLICATION_SCAN_NOT_PERMITTED` | a whole-site scan without admin or the extra scope |
| 404 | `SCAN_NOT_FOUND` | unknown scan, or a scan of another organisation (verified live) |
| 409 | `SCAN_NOT_READY` | the scan is not completed yet, for the PDF (verified live) |
| 409 | `SCAN_CONFLICT` | cancelling a scan that has already ended (verified live) |
| 409 | `ANALYSIS_IN_PROGRESS` | an analysis of this scan is already running |
| 422 | `VALIDATION_ERROR` | missing or invalid field, an unknown `variant`, a scan id that is not a UUID (verified live) |
| 429 | `RATE_LIMIT_EXCEEDED` | the organisation's scan limit |
| 501 | `WEBHOOK_NOT_CONFIGURED` | a webhook was asked for on a deployment that cannot sign one |
| 502 | `ANALYSIS_FAILED` | the AI model's answer failed validation |
| 503 | none | the PDF could not be made (retry; the scan is fine) |
| 504 | `SCAN_TIMEOUT` | the scan exceeded its time limit |
| 500 | `INTERNAL_ERROR` | anything unexpected |

Two errors use a different shape because they come from the sign-in check, before this API runs: **401** `{"detail": "Missing bearer token"}` (no key, or an invalid or revoked key), and **403** `{"detail": "..."}` when the key lacks the scope `consent:scan`.

## 11. Example: the whole flow with curl

```bash
BASE=http://localhost:8100/api/v1/consent-agent
KEY=<your csv_ key>

# 1. start
SCAN=$(curl -s -X POST $BASE/scans -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
        -d '{"website_url":"https://example.com","authorized":true}' | jq -r .scan_id)

# 2. poll until it ends
while true; do
  STATUS=$(curl -s -H "Authorization: Bearer $KEY" $BASE/scans/$SCAN/status | jq -r .status)
  case "$STATUS" in completed|failed|cancelled) break ;; esac
  sleep 5
done
echo "scan $STATUS"

# 3. the score (repeat if policy_analysis is not "done" yet)
curl -s -H "Authorization: Bearer $KEY" $BASE/scans/$SCAN/assessment | jq '{score, final, policy_analysis}'

# 4. the PDF
curl -s -H "Authorization: Bearer $KEY" "$BASE/scans/$SCAN/report.pdf?variant=long" -o report.pdf
```

## 12. Not verified

- `POST /scans/web` and whole-site scans (`POST /scans/application`): only the refusal for a bad address was tested; no whole-site scan was started.
- Webhooks (`webhook_url`, `callback_url`, `callback_secret`): not tested.
- The Consiva.io website's own code is not in this repository, so how it calls these routes is not checked here.
- The deployment that produces your customers' reports must run this code and a working Claude key; if its key is rejected, checks P7 to P9 show "Not Assessed".

## 13. Console routes (not needed for a website)

The Consiva console uses a second set of routes under `/api/v1/consent/scans/...` with a login token only. They return the same score and PDF, but a website does not need them.
