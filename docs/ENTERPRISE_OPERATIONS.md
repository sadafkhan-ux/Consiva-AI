# Enterprise DPDP scanner: how it works and how to run it

The Enterprise agent is a separate product from the frozen 5-page agent. It has its own crawler, evidence store, assessors, scoring engine, findings, report and screens, all under
`backend/app/enterprise/` and `frontend/src/enterprise/`. It reads the 5-page scanner's browser setup, URL safety and consent-banner helpers and changes none of them
(`backend/frozen/five_page_agent.manifest.json` and its guard test enforce that).

## The pipeline

```
Authorised website
  -> crawl            crawl/runner.py     frontier in the database, deep inspection, resume
  -> evidence         evidence/store.py   page text and html, cookies, requests, forms, headers, TLS, consent tests, documents (PDF, DOCX, OCR)
  -> rule assessors   assess/             TS1-TS6 technical, C4-C8 consent: fixed rules, no model
  -> text analysis    analysis/           notice, contact, rights, cross-checks: model-assisted, every fact tied to a verbatim quote
  -> score            scoring/            100 points over 33 parameters, reproducible, history kept
  -> findings         findings.py         one per parameter not fully met, with law, evidence, URLs and a recommendation
  -> report           report/             Enterprise PDF
  -> screens          frontend/src/enterprise/
```

`pipeline.py` runs everything after the crawl. A step that cannot run leaves its parameters NOT ASSESSED and the scan's analysis `partial`, with the reason in `analysis_error`. A model or
API failure is never turned into a failure of the website.

## Status model

| Field | Values | Meaning |
|---|---|---|
| `status` (crawl) | queued, running, completed, failed, cancelled | the crawl |
| `analysis_status` | not_started, running, completed, partial, failed | the analysis phase |
| `completion` (derived) | queued, crawling, crawled, analysing, partial, complete, failed, cancelled | one word for where it stands; `complete` only when nothing is outstanding |

A crawl that stopped on a limit, or an analysis with failed or pending chunks, is `partial`. `GET /{id}/declaration` gives the actual counts (see `docs/ENTERPRISE_API_CONTRACT.md`).

## Limits and safe defaults (ENTERPRISE_* settings, stored on each scan)

| Setting | Default | Effect |
|---|---|---|
| `ENTERPRISE_MAX_URLS` | 100000 | ceiling on URLs recorded; reaching it sets `stop_reason = url_limit` and lists the rest as pending |
| `ENTERPRISE_MAX_DEPTH` | 50 | deepest link level |
| `ENTERPRISE_CONCURRENCY` | 4 | pages in flight per scan |
| `ENTERPRISE_PER_DOMAIN_RPS` | 2 | navigation starts per second against one domain |
| `ENTERPRISE_MEMORY_BUDGET_MB` | 4096 | this process plus its browsers; over it, new pages wait, and after five minutes the crawl stops with `memory_limit` |
| `ENTERPRISE_MAX_RUNTIME_SECONDS` | 43200 | the crawl stops with `time_limit`; pending URLs stay pending and the scan can be resumed |
| `ENTERPRISE_CLAIM_LEASE_SECONDS` | 300 | a URL claimed and not finished in this time is taken back (a crashed worker) |
| `ENTERPRISE_MAX_ATTEMPTS` | 3 | tries per URL before it is recorded as failed |
| `ENTERPRISE_MAX_DOCUMENT_MB` | 25 | a larger document is recorded as too large, never cut |
| `ENTERPRISE_MAX_EVIDENCE_BYTES` | 5000000 | larger content is refused, never silently truncated |

## Resume and recovery

Everything that must survive a crash is in the database: the frontier (`ent_frontier`), the evidence (`ent_evidence`) and the analysis chunks (`ent_analysis_chunks`). Calling the job again for the same scan
continues it: URLs already done are not visited again, URLs a dead worker held are taken back, and analysis chunks already done are not redone. `POST /{id}/resume` queues a crawl again; `POST /{id}/analyze`
queues the analysis again.

## History is kept

Parameter assessments are superseded, never edited (`ent_assessments`, one `current` row per parameter). Every score calculation is a row (`ent_score_results`). Reports are versioned (`ent_reports`). Every
create, cancel, resume, review, analysis and report is an `ent_audit_events` row. A new scan of the same site can be compared with an earlier one (`GET /{id}/changes?against=`).

## Tenant isolation

Every route loads its scan through the organisation-scoped lookup, and a scan of another organisation is "not found" (a structural test checks that no route skips this). The tables also carry row-level
security policies. In a deployment whose application database role is a superuser those policies are not enforced (the application logs this at start); the application-level scoping is then the only
isolation. Completing the role cutover is an operations task: connect as a non-superuser role that is granted the tables, and the policies apply.

## What a score means

`Website-observable DPDP readiness score`. It is not a compliance certificate. Most duties start on 13 May 2027, so until then the score is labelled `readiness`. The legal registry is a draft until a named
person signs it off (`LEGAL_REVIEW_STATUS`). Method, points and approvals: `docs/ENTERPRISE_SCORING_METHOD.md`.
