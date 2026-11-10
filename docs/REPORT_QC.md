# Report quality control and release approval

A DPDP scan report is a customer-facing deliverable. Nothing is released straight after automated generation.

## The four checks (every print, automatic) — `backend/app/services/report_qc.py`
| Check | What it verifies | Where it runs |
|---|---|---|
| Calculation QC | rows, categories and points reconcile; the score is awarded ÷ assessed marks, rounded half up (or held back for a stated reason) | `scorecard.validate_scorecard`, on every result |
| Content QC (rows) | every row agrees with the marking rules: pass = full marks, partial = half, fail = 0, not assessed = no marks; each asserting row has evidence; a blocked row scores 0; a partial or failed row states its finding; the band matches the score (or is held one level lower) | same, on every result |
| Content QC | page count (summary and Free: exactly 2); no internal check code (P1 to P12) in customer text; one overall score on the page | on every printed PDF, before it is stored or served |
| Visual basics | A4 pages, each carrying real text; at most 20 soft masks in the document (blurred shadows printed as pictures, the source of patches in some viewers; the current design prints 5, the earlier one printed 70) | same |
| Functional QC | every link goes to Consiva or the scanned site; the first page names the site, the scan date and time in IST, and a report ID | same |

A report that fails is neither stored nor served; the reason is logged (`Report of scan ... failed quality control`) and the caller gets 503.

## Visual review (a person, each release)
Open every page at 100% and check: icons and tiles have no rectangular patch or halo; badges and marks are aligned; no text is clipped or overlapping; the disclaimer is readable; the two pages hold. A report with a visual defect is not approved.

## Release approval — migration 0048, `report_approval_service.py`
* Off by default. Set `REPORT_REQUIRE_APPROVAL=true` to make it mandatory: a report PDF is then downloadable only after approval; an administrator can always download a draft to review it (`X-Report-Release: draft`).
* Owner: the administrator who approves. The approval records who, when, the scorecard version, and a note. A change of scoring rules makes it stale; it can be withdrawn.
* `GET /api/v1/consent/scans/{id}/report-approval` shows the state. `POST` approves (administrators only; the report is printed through the gate first). `DELETE` withdraws.

## Run the checks by hand (repeatable)
```
cd backend
python tools/report_qc.py REPORT.pdf --site poolscapes.in [--variant summary] [--pages OUTDIR]
```
Exit code 0 means every automatic check passed. `--pages` renders each page to a PNG (needs `pdftoppm`) and prints the visual checklist for the reviewer.

## Ownership
`REPORT_QUALITY_OWNER` (free text, for example `Name <email>`) names the person accountable for report quality. It is shown in the approval response and in `GET /api/v1/system/version`. It is empty until you set it.

## How the gate was tested (10 Nov 2026)
Each of these real or altered PDFs was run through `check_pdf`: the good Poolscapes report passes; the same report checked against the wrong site, with a page removed, with a link to an unknown host, with a `javascript:` link, resized to Letter, and the earlier blurred-shadow design (70 soft masks) were each blocked with the reason stated.
