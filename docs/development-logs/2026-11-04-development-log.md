# Development log, 2026-11-04

## Session information
- Date: 2026-11-04. Start time not recorded. Timezone: Asia/Kolkata (UTC+05:30).
- Branch: `feature/privacy-policy-agent`. Latest commit `63a281b`. Phase 2 work from 2026-11-03 is still uncommitted in the working tree, together with the fix below.
- Read first: `2026-11-03-development-log.md` (Phase 2: URL inventory and crawl controls, uncommitted).

## Development work
**Bug: the same site scored 95 on the live agent and 70 locally (digitalanumati.com).**
Evidence: the two PDFs in `Task01/`. Same five pages. Checks 4 (sharing) and 5 (retention) were quoted from the cookie policy in one run and "not found" in the other; the second report said "the text was cut at the storage limit" about a privacy policy of 7,808 characters. The stored local scan was not available on this machine (neither the Docker database nor the host database has it; its newest scan is from 2026-11-03), so the cut document was not identified directly. The causes below are established from the two reports and the code.
1. `frontend/src/report/tenChecks.ts`: any cut document was blamed for a missing requirement, including a long page the scan filed as a policy. Now only a cut privacy or cookie policy page (by its address) counts (`cutPolicyDocs`).
2. Same file: a requirement not found in a policy page that really was cut is now Not Assessed (no marks, left out of the maximum) and names the page and how much was read; it was Failed with 0 marks. Applies to checks 2 to 7.
3. `backend/app/policy/analysis.py`: the fixed-pattern second look (used only when the model says "not found") now also covers `data_sharing` and `retention`: an exact sentence of a document that names a recipient or a period or criterion; legal pages searched first; never replaces a model quote.
Docs: `docs/PRIVACY_READINESS_TEN_CHECKS.md` (new section), `docs/POLICY_CONTENT_ANALYSIS.md`.
Not changed: the model's own variation on requirements the pattern does not cover; documents longer than the 60,000-character storage limit are still cut (now Not Assessed, not Failed).

## Files changed
Modified: `frontend/src/report/tenChecks.ts`, `frontend/src/report/tenChecks.test.ts`, `backend/app/policy/analysis.py`, `backend/tests/test_policy_analysis.py`, the two docs above. Added: this log. Deleted: none.
Database, API: no changes.

## Testing (executed)
- Backend, full suite excluding the two live-model tests: 4,523 passed, 0 failed, 48 skipped.
- Frontend: 1,141 passed; type check clean.
- Lint on `analysis.py`: same three findings as at HEAD (none added).
- New backend tests: 16 (sharing and retention rescue, exact-quote, no invention, determinism, legal pages first, per-page truncation). New frontend tests: 4. Three existing backend tests were changed because their fixture policy states a retention period, which is now correctly found by the pattern; they now use a copy of the policy without that sentence.
- Docker images rebuilt; backend healthy.

## Scanning and validation
No live scans and no requests to digitalanumati.com. Not verified: the fix against that domain's real pages.

## Later the same day: score shown two ways (dashboard 66.7%, PDF 94.4/100)
Cause 1: the dashboard hero computed the ten-check score as soon as the scan's evidence loaded, while the policy analysis was still running, so checks 2 to 7 were left out and it showed 2 of 3 (66.7%) where the finished PDF had 8.5 of 9 (94.4). Cause 2: the hero grid was laid out by window width, but the dashboard sits beside a side panel, so the text column collapsed to one word per line.
Fixes (frontend only): the hero shows no number while the run is in progress ("Assessing") and the final score once it ends, with a note of how many checks were left out; the hero stacks by the width of its own column (container query); every score is written one way, `N/100` (new `report/scoreFormat.ts`, used by the ten-check engine, the weighted DPDP score, the score comparison, all rings and the fallback PDF), with the ring showing the number and a small "/100" beneath it. Coverage and share percentages are not scores and keep their `%`.
Tests: 16 new (`scorePattern.test.tsx`); about 30 existing assertions changed from `%` to `/100` or to the ring's number-only text; the ring's baseline test was rewritten for the two-line ring. Frontend 1,158 passed; type check clean. Verified by re-rendering a stored scan (total row "95/100", ring "95" and "/100") and by rendering the hero in a 560px column with the real CSS (stacks correctly; pending state shows "…").
Not verified: the live dashboard against a running scan.

## Pending work
Confirm against the real digitalanumati.com pages (needs authorization to scan it, or the stored scan's policy-analysis JSON). Decide whether long policies should be analysed in chunks instead of cut at 60,000 characters. Commit the Phase 2 work and this fix (ideally as separate commits).

## Git information
Branch `feature/privacy-policy-agent`; latest commit `63a281b`; uncommitted: Phase 2 (2026-11-03) and this fix; not pushed.
