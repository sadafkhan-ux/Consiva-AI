# Release checklist (run before every release and before any report goes to a customer)

1. **One code version everywhere.** Deploy with `scripts/deploy.ps1 -Service backend` (it rebuilds backend and worker together). Then open `GET /api/v1/system/version` on every deployment (local, staging, consiva.ai) and confirm `scorecard`, `scanner_score`, `requirements`, `scoring_engine` are identical. The worker logs the same line at start ("Worker code: ...").
2. **Scoring engine** is `scorecard` on every deployment (`SCORING_ENGINE` unset or `scorecard`).
3. **Reference sites.** Scan one banner site, one banner-less site, one with a bare "Accept/Decline" notice (greenply.com) and confirm banner detected, scores stable between two runs, screenshot present in the PDF.
4. **Tests** (live-AI files excluded): backend `pytest`, frontend `npx vitest run` and `tsc -p tsconfig.app.json`.
5. **Quality gate** passes on a printed report: `python backend/tools/report_qc.py REPORT.pdf --site DOMAIN --pages OUT`.
6. **Approval.** With `REPORT_REQUIRE_APPROVAL=true`, an administrator approves each report before a customer downloads it.

## Note for the consiva.ai web page team (separate frontend)
The free-scan page showed "Consent banner: Found" for greenply.com while the PDF said "None detected". The page treats the presence of a consent record as a banner. The scan always writes one record; the banner exists only when `consent_signals[0].mechanism_type` is `banner` or `cmp`. Show "Found" only then, and show "Not found" for `none`. For Accept and Reject, `accept_interaction` / `reject_interaction` of `cmp_not_found` means "no banner to click", not "could not verify". Better still, render from the same endpoint and fields as the PDF (`/assessment` and `/evidence`).
