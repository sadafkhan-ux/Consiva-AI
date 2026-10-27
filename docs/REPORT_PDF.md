# The Consent Agent report: web page and PDF

One report, drawn once. The web page `/?report=<scan id>` and the PDF from `GET /api/v1/consent/scans/{scan_id}/report.pdf` are the same
document: the server opens the web page in headless Chromium and prints it. There is no second copy of the layout or of the values.

## How it fits together

```
Dashboard "Download Report (PDF)"  ──►  GET /api/v1/consent/scans/{id}/report.pdf   (backend/app/api/v1/routes/consent_scans.py)
                                              │  checks the caller owns the scan and its website, and that the scan completed
                                              ▼
                              services/report_pdf_service.py  ──►  headless Chromium (already in the image)
                                              │  opens  {REPORT_FRONTEND_URL}/?report=<id>&print=1  with the caller's own token
                                              ▼
                       frontend/src/report/print/ReportPage.tsx   (loads the scan exactly as the console does)
                                              ▼
              frontend/src/report/modelFromState.ts  ──►  buildReportModel(...)  ──►  ReportDocument.tsx + report.css
```

- **One data model.** `reportModelFromState` builds the `ReportModel` (score, evidence, policy analysis, readiness review, insights...). The dashboard,
  the in-browser PDF fallback, the web report and the server's PDF all start from it. Shared wording helpers are in `report/reportText.ts`.
- **One design.** `report/print/report.css` serves screen and print. On screen the report is a centred document with a toolbar; printed, the header,
  footer and page numbers come from the print engine, so they repeat on every page.
- **The hero** is one container with three areas in a row: the circular Compliance Score, what it means, and the website preview with its domain and
  scan time. The print rules never stack it; on a phone-width screen it stacks. **Coverage is not shown in the hero.** When too few requirements
  could be assessed it says "Compliance score unavailable" and gives no number.

## The API

`GET /api/v1/consent/scans/{scan_id}/report.pdf` — `Authorization: Bearer <token>` (the same token as every other endpoint).

| Status | Meaning |
|---|---|
| 200 | `application/pdf`, `Content-Disposition: attachment; filename="consiva-privacy-report-<host>-<date>.pdf"`, `Cache-Control: private, no-store` |
| 401 | no token, or an invalid one |
| 404 | the scan (or its website) is not in the caller's organisation, or does not exist |
| 409 | the scan has not completed, so it has no report |
| 422 | `scan_id` is not a UUID |
| 503 | the PDF could not be made; a plain message, with the cause only in the server log |

Printing uses the caller's own token inside the browser's session storage, so the report page can read exactly what that user could read through the
API. The browser may only reach the one web origin in `REPORT_FRONTEND_URL`; every other request is refused. A finished PDF is kept for
`REPORT_PDF_CACHE_SECONDS` (default 120), keyed by scan and the time it completed; at most `REPORT_PDF_MAX_CONCURRENT` (default 2) browsers run.

## Configuration (`backend/.env`)

| Variable | Default | |
|---|---|---|
| `REPORT_FRONTEND_URL` | `http://frontend:8080` | where the API reaches the web app: the compose service. Use e.g. `http://127.0.0.1:5173` when the API runs outside Docker. |
| `REPORT_PDF_TIMEOUT_SECONDS` | 90 | how long the page may take to draw |
| `REPORT_PDF_MAX_CONCURRENT` | 2 | browsers at once |
| `REPORT_PDF_CACHE_SECONDS` | 120 | 0 turns the cache off |

## Sections (same order on the web and in the PDF)

1 Scan summary · 2 Framework readiness · 3 Privacy areas · 4 Top findings · 5 Consent behaviour (before consent / after Accept / after Reject, with "Not tested"
where a state was not exercised) · 6 Tracking technologies · 7 Privacy elements · 8 Every check behind the score · 9 Prioritised fixes · 10 Grievance officer ·
11 Data Protection Officer · 12 Indian-language support · 13 Children's data and age gate · 14 Observable security signals · 15 Five-page DPDP readiness review ·
16 Website pages scanned · then the evidence appendix (homepage screenshots, sector, policy analysis, consent banner, forms, cookie inventory, third-party
requests, policies, services, compliance findings in full) and the disclaimer. Sections are numbered by what is present, so a report without evidence
has no appendix and no gap in the numbers.

## Limits

- The contents list on the first page has no page numbers (a print engine cannot know them while laying the page out).
- The scanner takes one homepage picture, before any consent choice. The "after Accept" and "after Reject" screenshot slots say "Not captured".
- The Download button falls back to the PDF drawn in the browser (`report/downloadReport.ts`) only if the server cannot be reached or cannot make the
  PDF (network or 5xx). A 404, 409 or 403 is shown as it is.
- The score, the checks and their weights are unchanged: the report only displays what the backend returns.

## Free Scan and Full Scan reports (scope document, 23 Oct 2026)

`GET /api/v1/consent/scans/{id}/report.pdf?variant=summary|free|full|long` (default `summary`, the two-page report; ask for `?variant=long` to get the full evidence report).

| variant | What it is | How it is printed |
|---|---|---|
| `summary` (default) | The two-page report: logo header, one Compliance Score, the checkpoints with their evidence, cookies and consent states, the sign-up link and the disclaimer (`SummaryReport.tsx`) | One Chromium print of `/?report=<id>&print=1&variant=summary&part=pages`, no print-engine header or footer |
| `free` | Two fixed A4 sheets (`FreeReport.tsx`) | One Chromium print of `/?report=<id>&print=1&variant=free&part=pages`, no print-engine header or footer: each sheet carries its own |
| `full` | Eight fixed A4 sheets (`FullReport.tsx`) followed by the evidence appendix | `part=pages` (fixed) and `part=appendix` (flowing, with the print engine's header/footer, numbered "Appendix page n of N") printed separately and joined with pypdf |
| `long` | The earlier single-flow evidence report | As before |

Web report: `/?report=<id>&variant=free|full` (none = the long report). The toolbar switches between them.

All three read one `ReportModel`. The fixed pages take every figure from `report/pageReports.ts`, which reads the same visual, score and finding builders the dashboard uses, so no page counts anything itself. The call to action comes from `report/reportCta.ts` (scope 8.3); the Full report is the subscriber report and gets the operational action, never the acquisition one. Report IDs are `FR-`/`FS-` + the scan date + the first four characters of the scan id.

Decisions where the sources disagree: the corrigendum is dated 10 Dec 2025 (G.S.R. 892(E), as in `scoring/dpdp_score.py`), not 11 Dec as the scope document says. The score is named "Privacy Readiness Score" and shows coverage beside it on the new pages, as the scope requires; the long report's hero still shows the compliance score without coverage.

## Pre-generated reports and speed (2026-10-26)

- **Stored copies.** Each finished PDF is kept in `report_pdfs` (migration 0043) with a fingerprint of everything the report is drawn from: the scan, its analysis runs, stages and findings with their review state, its screenshot, today's date in India, the scoring methodology and weights, and the web app build. A download is answered from the stored copy only while that fingerprint still matches; any change prints it again. The response header `X-Report-Source` says `stored` or `printed`. Copies older than `REPORT_PDF_RETENTION_DAYS` (30) are dropped when the next one is stored.
- **Pre-generation.** When an analysis run ends (including when it pauses for human review) and again when the review finishes, the worker prints the `long`, `full` and `free` reports (job `render_reports`, migration 0044), so the first download is already waiting. Measured: 14-150 ms stored against 1.5-3.5 s printed.
- **One print for many requests.** Identical simultaneous requests share a single print. The request gives its database connection back before the slow print (otherwise ten simultaneous downloads exhausted the 5-connection pool).
- **Scan speed.** The privacy-policy reading runs beside the compliance graph instead of before it; a finished policy reading is reused when the model would be asked exactly the same question for the same site (`input_hash`); the Accept and Reject passes start while the crawl runs (`SCANNER_OVERLAP_CONSENT_PASSES`, default on). No model call was added; the reuse removes one.
