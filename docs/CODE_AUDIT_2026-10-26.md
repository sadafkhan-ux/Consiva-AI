# Code audit, 2026-10-26

Ten reviewers each read every line of one part of the code (scanner and classification; consent agent, LLM and scoring; ROPA and Purpose; DSR; Breach and
Regulatory Watch; API routes; core, database and jobs; migrations and deployment; frontend components; frontend report, API and hooks). A finding is listed only
where a reviewer traced a concrete trigger. **FIXED** items were checked in the code and have a test; **OPEN** items were reported and are not yet fixed or
not yet re-checked. Nothing here was run against the live stack (Docker was stopped), so the deployment edits are unverified at run time.

Earlier audit: `docs/CODE_AUDIT_2026-10-16.md` (its C1 is the first item below and was still open on 10-26).

## Fixed on 2026-10-26

| # | What was wrong | Fix | Test |
|---|---|---|---|
| 1 | **Environment-variable leak (ROPA, DSR, Regulatory Watch).** A source's `credential_ref` could name any environment variable, including the JWT secret or database URL, and the server sent its value to the source's host. | `app/core/credential_refs.py` refuses the platform's own variables at every place a ref is read, and at the Regulatory Watch route. Optional `CREDENTIAL_REF_REQUIRED_PREFIX` makes it an allowlist. Error text no longer repeats the name. | `test_audit_fixes_2026_10.py` |
| 2 | A link with a malformed port (`:99999`) raised and failed a finished scan. | `normalize_url` returns the link unchanged. | same |
| 3 | The job reaper treated API scans (`consent_api_chain`) as short jobs and could start a second crawl of a scan that was still running. | Same lease as a scan, plus the analysis budget. | same |
| 4 | A reviewer's `reclassify_cookie` edit was not validated and rewrites a table shared by every organisation. | Shape, length and the four categories are validated before anything is written. | same |
| 5 | OpenAI-shaped usage counted cached tokens twice (usage panel total). | Input is `prompt_tokens` minus cached tokens. | `test_llm_usage_tracking.py`, `test_audit_fixes_2026_10.py` |
| 6 | The integration API reported 0 tokens for Claude scans and ignored retries. | Both usage shapes read; every analysis attempt counted. | `test_audit_fixes_2026_10.py` |
| 7 | `promote-baseline` accepted any scan id (another tenant's, another site's, unfinished). | Must be a completed scan of this website in this organisation, else 404. | route logic; no DB test |
| 8 | A cancelled or partial scan could become the monitoring baseline. | Only completed scans are diffed or used as a baseline. | no DB test |
| 9 | Cookie lookup order was undefined where a name appears twice. | Ordered: human-confirmed first, then oldest. | no DB test |
| 10 | Negative `limit`, huge `interval_hours`, `LOG_LEVEL=info`, ROPA confidence 0 shown as null, CORS blocked PUT and DELETE. | Bounds and fixes. | partly |
| 11 | ROPA run and DSR case tabs stayed empty after the status changed (records, evidence, plan not reloaded). | Reload on a status change. | no UI test |
| 12 | "Open web report" opened a tab with no session. | The report page signs in (bypass mode) or shows the sign-in form. | existing report page tests |
| 13 | The "critical finding" call-to-action rule could never fire (the score has no Critical). | A confirmed High finding triggers it. | `pageReports.test.tsx` |
| 14 | Full report: "+N more technologies" over-counted; the Full tile said "Free scan limit"; the policies list and form field lists could push content off the sheet. | Counts per report, labels per variant, lists capped. | `pageReports.test.tsx` |
| 15 | nginx refused evidence pushes over 1 MiB; the backend container had no shared memory for Chromium. | `client_max_body_size 12m`; `shm_size: 1gb`. | not run (Docker stopped) |

## Open: needs a decision

- **Credential refs, full fix.** The guard above blocks the platform's own variables. The audit's recommendation stands: admin-only source creation, a mandatory prefix,
  an internal-address check on the ROPA connectors (`assert_safe_host`) and no redirects.
- **Any member can mint integration keys with any scope**, including `consent:scan:application` (no page cap), and can list and revoke every key (`routes/ropa.py`).
- **Production runs as the database owner**, with the boot guard switched off in `docker-compose.prod.yml`, so row-level security is never consulted.
- **`APP_ENV=development` passes the deploy check** and turns on the no-password dev login.
- **After-Accept / After-Reject passes tag requests made before the click** (`scanner/crawler.py` interaction pass), so "continued after Reject" (R-003) can fire for a
  site that stopped tracking. Changing it changes the counts the reports show.
- **Cookie lookup mislabels first-party cookies** through short wildcard prefixes (`_s`, `CT`, `tp`, `cap`) and generic names (`uid`, `token`, `ID`), ignoring the domain.
- **Prefixes in the vendor list** (`IDE`, `li_`, `ph_`), case sensitivity, and URL-substring matching.
- **Monitoring diff ignores consent state**, so a tracker that moves to "before consent" is not a change.

## Open: correctness, high or medium

- Retried jobs append a second copy of a scan's evidence (`save_scan_result`); an analysis that fails early leaves its run "running" and blocks later analysis; two concurrent first scans create two website rows and break that domain.
- Manual-upload Regulatory Watch sources create a finding every 30 s; the sweep re-enqueues a source each tick until the job runs; a failed collection with an unset credential is never recorded; a Bearer token is re-sent on cross-host redirects.
- Purpose agent: a DSN, token and CSV are stored in plain text in the job queue; a multi-host DSN passes the internal-address check; declared purposes are read from rejected and draft ROPA records; the "no consent mechanism" case is worded as "found but not working"; cookie lifetimes are measured against today.
- ROPA: personal columns classified as non-personal (`birth_date`, `marital_status`, `phone_num`, `state`); schema-qualified new tables over-count changes; an LLM suggestion can overwrite a human-declared classification; a job delivered twice duplicates findings.
- DSR: search matches on unverified identifiers (phone, reference); all sources failing leaves a case stuck at RESPONSE_PENDING; execution builds a write connector for read-only operations; an expired approval strands an action; a write that commits before the job's own commit can be reported failed on retry; push evidence is not de-duplicated.
- Breach: the "unknown confidence goes to a human" safeguard is dead; `/analyse` has no status guard; a naive `detected_at` returns 500; the incident-type regex has no word boundaries.
- Policy analysis: a date is accepted as a phone number; "forty-five days" parses as 5; a policy fetch has no size or time bound.
- Integration API: rejected findings and earlier runs' findings are counted; list and detail status disagree.
- Frontend: stale responses overwrite newer ones in the consoles; typed text is cleared before a save succeeds; drafts carry over between records; some pollers never stop; report builders disagree on whether a page loaded; the readiness score line counts unscored rows; the browser-drawn PDF omits the provisional notice.

## Open: lower priority

CI lock file lacks `cryptography`; production image installs unpinned packages; three tables lack FORCE row-level security; SQL default conflicts with a CHECK on `regwatch_findings.relevance`; `updated_at` is not maintained on most tables; `/docs` is public through nginx; no login throttling; no body-size cap before buffering in the API; unbounded request fields; the scheme check on policy links in the report; accessibility of sortable headers and tab lists.
