# Consiva AI — code audit

**Date:** 2026-10-16 · **Branch:** `amd-testing` @ `1f3a647` plus the uncommitted Docker/migration work · **Scope:** whole repository (backend, six agents, frontend, Docker/CI, dependencies)

## How to read this report

Every finding carries one of two evidence tags:

- **[V] Verified** — I confirmed it myself by running code, running a scanner, or reading the exact lines. Where I ran something, the observed result is given.
- **[R] Reported** — raised by a read-only review of that area, backed by file:line references, but **not independently re-verified** by me. Treat as likely, and confirm before acting.

Findings I could not confirm, or that proved wrong, are listed under *Retracted*. Nothing here was exploited against a running deployment; no live database, third-party site or LLM was contacted. The DSR agent was audited in depth earlier in this engagement and is not re-audited here.

## Executive summary

The codebase is, in many respects, carefully built: tenancy filters are consistent, passwords and keys are handled correctly, the migration and deployment path is now safe, and the compliance engines have real grounding checks. The serious problems are concentrated in **authorization (who may do what)** and **outbound credential handling**, not in the core data model:

1. **A single logged-in member can take over the platform.** The ROPA source feature lets any member point the server at any host while attaching any environment variable (e.g. the JWT signing secret) as a credential header [V]. Separately, any member can mint non-expiring service keys with admin-equivalent scopes [V].
2. **Tenant isolation in production rests on application code alone.** Row-level security exists but is switched off by configuration [V].
3. **Several compliance outputs can be wrong without any warning**: a blocked page reads as "no privacy policy", "tracker fired after Reject" is asserted without timing evidence, and a breach can be closed with no notification decision [V/R].
4. **Known vulnerable dependencies** (PyJWT, pypdf, urllib3) are installed in the shipped image [V].

## Findings

### Critical

**C1. Any org member can exfiltrate any process environment variable, and use the server for SSRF, via a ROPA data source.** [V]
- `backend/app/agents/ropa/connectors/factory.py:115-125` resolves `credential_ref` with `os.getenv()`; the only check is that the name is alphanumeric/underscore. For REST sources the value is sent as `Authorization: Bearer <value>` (`connectors/api.py:129`) to a caller-chosen `base_url`, with `follow_redirects=True` (`api.py:145`). `POST /ropa/sources` and `POST /ropa/sources/test-connection` require only `get_current_user` — no admin check (`routes/ropa.py:329-381`).
- **Verified:** with `SENTINEL_SECRET_FOR_AUDIT=audit-canary-value` set, `build_connector(rest_api, credential_ref="SENTINEL_SECRET_FOR_AUDIT")` produced the header `Bearer audit-canary-value`. `grep assert_safe` under `app/agents/ropa` returns **0** references — no SSRF guard at all (the DSR, Purpose and Consent paths have one; ROPA is the outlier).
- **Impact:** the backend container's environment includes `CONSIVA_JWT_SECRET`, `ROPA_CREDENTIAL_ENCRYPTION_KEY`, `DATABASE_URL` and LLM keys. A member can send any of them to their own server. With the JWT secret an attacker can sign tokens for **any organisation and role** (HS256 with `org_id`/`role` claims) — a consequence that follows from the token code but was not exercised end to end. The same primitive reaches `169.254.169.254`, localhost and internal services, and `test-connection` returns the raw error text, which makes it an oracle.
- Regwatch has the same pattern (`regwatch/services/collection_service.py:66-80`; any UPPERCASE name accepted) and additionally re-sends the credential on cross-host redirects (`connectors/http_source.py:140-175`) [R].
- **Fix:** admin-only source CRUD; replace `os.getenv(credential_ref)` with a **prefix allowlist** (e.g. only `CONSIVA_SRC_*`) or drop the env path in favour of the encrypted stored credential; apply `assert_safe_url`/`assert_safe_host` on every connect; `follow_redirects=False`; never forward credentials across hosts. **Check your audit log** (`ropa_data_source.created`, test-connection calls) for unusual `credential_ref` values.

### High

**H1. A plain member can mint service keys with any scope, bypassing every admin gate — including the one I added for application scans.** [V]
- `POST /ropa/integration-keys` has only `Depends(get_current_user)` (`routes/ropa.py:523-547`); scopes are validated against `KNOWN_SCOPES`, which includes `dsr:config`, `dsr:operate` and `consent:scan:application`. Keys can be non-expiring.
- **Verified by running the route function:** a `role="member"` user minted a key with `['dsr:config','dsr:operate','consent:scan:application']`, `expires_at=None`. A principal built from such a key **passes** `dsr._require_admin` and `consent_agent._require_application_permission`, while the member themself is refused.
- **Fix:** require admin to mint/revoke/list keys; restrict which scopes a non-admin may ever request.

**H2. Row-level security is inert in production.** [V]
- `docker-compose.prod.yml` runs the API and worker as the table owner `consiva` and sets `ALLOW_PRIVILEGED_DB_ROLE: "i-accept-no-database-tenant-isolation"`, which skips the boot refusal in `app/db/privilege_check.py`. The policies exist (a by-id AST scan found org filters consistently applied) but protect nothing today; the worker is cross-tenant by design even after an API cutover.
- One real gap that RLS would not catch anyway: **`promote_baseline`** accepts a `scan_id` from the request body and stores it without checking it belongs to the caller's org (`services/monitoring_service.py:55-62`); the later diff reads that scan's evidence with no org filter [V by reading; not exercised]. Needs the victim's scan UUID.
- **Fix:** complete the documented `consiva_app` cutover; validate `scan_id` ownership; `FORCE` RLS on `dsr_evidence_pushes` [R].

**H3. `OPEN_ACCESS=true` is accepted in production with no guard.** [V]
- `config.py:255` `auto_login_enabled = app_env == "development" or open_access`; nothing at startup refuses `APP_ENV=production` + `OPEN_ACCESS=true`. That setting signs every anonymous visitor in as the oldest user (likely an admin). One typo in the server `.env` is a full authentication bypass.
- **Fix:** refuse to start (or loudly warn) when both are set. **Check the deployed value now.**

**H4. Authorization is authentication-only outside three places.** [V by reading; R for the full list]
- Role checks exist only for user creation, DSR configuration and the application-scan profile. Any member can approve/reject consent findings, create and close breach incidents, register Regwatch/ROPA sources and set credentials, and execute DSR erasures (with `dsr:operate`).
- **Fix:** define an explicit role matrix; at minimum gate configuration, credential and destructive routes behind admin.

**H5. Unauthenticated requests can block API worker threads for 30 s each.** [V]
- The legacy Supabase branch in `core/security.py:99-109` runs for any bearer token with an `RS256`/`ES256` header when `SUPABASE_URL` is set: it calls `PyJWKClient.get_signing_key_from_jwt`, a **blocking** HTTP fetch, inside a sync dependency (runs in the threadpool). Supabase was retired from this deployment, so the endpoint is dead.
- **Measured:** four concurrent requests with a junk RS256 token against a non-responding "JWKS endpoint" each blocked **30.3 s**. About 40 such requests would exhaust the default threadpool and stall the whole API. Your local `.env` has `SUPABASE_URL` set; the deployed value is unknown.
- **Fix:** unset `SUPABASE_URL`/`SUPABASE_JWT_SECRET` on the server and remove the legacy path.

**H6. The shipped image contains packages with known vulnerabilities and is not the tested set.** [V]
- `pip-audit` on `requirements.lock.txt`: **PyJWT 2.13.0 — 14 advisories** (fixes in 2.14/2.15; several concern the JWKS client used in H5, others signature parsing and algorithm confusion), **pypdf 6.16.2 — 8** (DoS/memory via crafted PDFs; the RAG ingest reads PDFs), **urllib3 2.7.0 — 3** (fix 2.8.0), plus one transitive langgraph-sdk advisory. `npm audit`: **0 vulnerabilities**.
- The Dockerfile installs `requirements.txt` (floor pins like `fastapi>=0.115`), while CI tests `requirements.lock.txt`, so production resolves different versions from the ones tested. `uv.lock` is a third, disagreeing source [R].
- **Fix:** upgrade the three packages; build the image from one pinned lock; add `pip-audit`/`npm audit` to CI.

**H7. Compliance outputs that can be wrong without a warning (Consent Agent).**
- **Scan completeness never reaches the rules or the LLM.** [V] `stop_reason`, `pages_failed`, `pages_timeout` are consumed only by the API schema; `_navigate_with_retry` returns `"success"` for any HTTP status. A 403/503/bot-challenge on the seed yields a "completed" scan with 0 trackers, and rule R-006 ("No privacy policy page was discovered", high risk) can fire because the page was simply never reached [R for the R-006 consequence].
- **R-003 ("fired after Reject") does not establish timing.** [V by reading] In the Reject pass `request_records` is one list attached at page creation and never reset at click time, so anything that fired during load is tagged `post_reject`. A high-risk accusation rests on an unsupported temporal claim.
- **Retries duplicate evidence.** [V by reading] `save_scan_result` only inserts, and `execute_scan_and_persist` guards only against `cancelled`; a retried or reaped job re-inserts pages, cookies, trackers and a second `consent_signals` row (rules read `consent_signals[0]`).
- **Stale-job reaper vs long jobs.** [V partly] The reaper uses a flat 10-minute lease measured from a `locked_at` that is never refreshed, and `mark_done`/`mark_failed` are unconditional. A slow chain job (crawl + a 480 s LLM deadline) can be requeued while still running and double-processed. My scan-profile work extended the lease for application scans only.
- **Duplicate webhooks across retries** (failed, then completed, with a fresh `event_id` each time) [R].

**H8. Breach agent: legal-process and classification defects.**
- Classification ignores negation: `classify("No credential compromised", "we confirmed no unauthorized access occurred")` → `credential_compromise`, 0.9, deterministic. [V]
- An incident with **no evidence at all** (`incident_type='other'`) is assessed `low`, score 8, and routed onward. [V]
- An incident can be closed without any recorded notification decision; completed actions can be flipped to failed and failed ones re-approved, so `closing_status` can read CLOSED instead of PARTIALLY_COMPLETED. [R, with transition checks exercised by the reviewer]
- The default external communication draft says containment steps "have been taken" regardless of action state and pastes the internal description. [R]

### Medium

- **SSRF guard gaps.** `_is_blocked_ip` returns **False** for `100.64.0.1` and `100.100.100.200` (CGNAT; also Alibaba's metadata address) and `fec0::1` [V]; it enumerates `is_*` properties instead of using `not is_global`. DNS-rebinding TOCTOU on every httpx/asyncpg path (the crawler pins DNS only for the root host); the crawler guard covers navigations only, so page JavaScript can make subresource requests to internal addresses; response bodies are downloaded in full before the size cap on several fetchers [R].
- **No login brute-force protection; no JWT secret strength validation; no token revocation.** [V by grep/reading] bcrypt cost 12 (~0.28 s) is the only brake; a weak `CONSIVA_JWT_SECRET` forges any token; role and `is_active` are baked into a 12-hour token, so a deactivated or demoted user keeps access until expiry.
- **Consent LLM layer.** Free-text citation check is substring-based: "Section 9", "Rule 17(1)(b)", "Article 21", "u/s 4" all pass unflagged against a pool containing "Section 99" [V]. Untrusted text is inlined unfenced and a newline in a form-field name forges a `## Deterministic rule findings` header inside the evidence block [V]. On the success path only the LLM's findings are stored, so a confused or injected model can omit or downgrade a high-confidence rule finding, and `evidence` ids are never validated [R]. RAG retrieval has no error handling, so an embeddings outage fails the whole run with zero findings [V: no `try` in `retrieve_rag.py`].
- **ROPA classifier** marks `birth_date`, `birthday`, `cc_num` and `biometric_template` as **not personal data with no review** (confidence 0.82) [V]; `existing_classification` in a pushed payload is accepted at 1.0 with no review [R].
- **Purpose agent persists credentials.** The DSN and `auth_header` go into `agent_jobs.payload` and `mark_done` never clears it, contradicting the "never stored" comments [V]. `read_csv` ignores its column cap [R].
- **Regwatch:** unbounded fetch memory and silent truncation at 8 MiB; jurisdiction matching is exact-string only (`India (MeitY)` vs `India` → not relevant); `/summary` counts only the latest 200 findings; a finding can be closed with every action cancelled [R]. Feed XML is parsed with stdlib `xml.etree` (bandit B314 HIGH confidence) [V].
- **Job/state integrity.** Cancel can be overwritten by `mark_scan_running`/`save_scan_result` races; dead jobs leave scans and agent runs wedged (only ROPA has a reconciler) and a stuck `agent_run` blocks all later analysis of that scan; `_run_job` is not fully exception-safe [R].
- **CI and edge.** CI never runs the frontend tests (the 8 vitest files) or lint, has no `permissions:` block, pins actions to tags, and has no dependency/secret scanning or Docker build [V for the absences]. `nginx.conf` sets no CSP, X-Frame-Options, nosniff, Referrer-Policy or HSTS while the token lives in `sessionStorage`; `/docs` and `/openapi.json` are proxied publicly [R].
- **Repository hygiene.** On `main` (not this branch) `backend/logs/llm_usage.jsonl` is tracked [V]: 20 lines, no prompt text, but 5 lines contain a username-bearing filesystem path; `.gitignore` ignores `*.log` but not `*.jsonl`. Stale duplicated `consiva_full_database.sql` (schema 0001–0006 only), a binary zip and working docs are tracked [R]. `backend/README.md`'s migration section is out of date [R].
- **Breach/Regwatch/ROPA logic:** 72 h clock hard-coded and measured from a backdate-able `detected_at`; naive `detected_at` causes a 500; findings lifecycle quirks (details in the agent notes) [R].

### Low / informational

- Legacy Supabase path would accept any Supabase-issued token carrying an `org_id` claim, with no `iss` check, if `SUPABASE_URL` is set [R].
- `auth_bootstrap_read` RLS policies expose `users`/`ropa_integration_keys` to any unscoped connection [R].
- Stdlib XML, `PLW0603` globals, 52 `except Exception` (13 annotated), 27 `PERF401`, blocking `os.path`/`open` in async functions in the crawler (some introduced by my cancel-signal code; tiny, but real) [V].
- Unpinned base images by tag, no multi-stage build; Python 3.11 floor but only 3.13 tested; example env keys in tests are fixtures [R].
- Frontend: no error boundary; `BASE_URL` copy-pasted in 7 files [R].
- ROPA/Purpose notification and agent-webhook URLs logged at warning level; a few routes return `str(exc)` as `detail` [R].

## Retracted / corrected

- **"Unvalidated `href` allows `javascript:` XSS" (frontend review) — not a vulnerability.** React 19's production bundle contains its `javascript:` URL blocker (`"React has blocked a javascript: URL"`), which I confirmed by searching the shipped `react-dom` build.
- **`llm_usage.jsonl`** is tracked on `main` only, not on `amd-testing`; the pipeline review believed it did not exist at all (true for this branch).

## What is sound

By-id lookups are org-scoped throughout (AST scan of all repositories/services/routes; foreign ids return 404); org comes from the token, never the body. bcrypt with a dummy-hash path equalises timing for unknown users and identical 401 text. JWT: HS256 only, `aud`/`iss`/`exp` required. Integration keys: `secrets` entropy, SHA-256 storage, `compare_digest`, strict scope list, a key cannot mint a key. `/dev/*` token routes are registered only in development or with `OPEN_ACCESS`, and the demo-token verifier is development-only. No cookies (no CSRF); no wildcard CORS in production. No `eval`/`exec`/`pickle`/`yaml.load`/`shell=True`. DSR and Purpose connectors do apply the SSRF guard. Job claiming (`FOR UPDATE SKIP LOCKED`) is correct and poison jobs are capped at 3 attempts. Structured DPDP citations are grounded to retrieved chunks and hallucinated ones hard-fail. The migration runner and Docker flow (this engagement) are tested end to end. Compose secrets fail closed; the image runs as non-root. `npm audit`: no vulnerabilities.

## Measured

| Check | Result |
|---|---|
| Routes / unauthenticated | 149 / 4 (`/auth/login`, `/health`, `/dev/auto-login`, `/dev/demo-token` — both dev routes gated by config) |
| Backend suite | 2339 passed, 3 failed (pre-existing: 2 scanner page-limit-default tests, 1 migration-numbering gap awaiting upstream 0036), 46 skipped |
| Frontend suite | 26 failures, all pre-existing: `frontend/.env` sets `VITE_API_BASE_URL=…:8010`, tests expect `:8000` |
| `ruff` (project config) | 24 in `app` (15 auto-fixable), 123 in tests — style only |
| `bandit` (medium+) | 3: two DSR quoted-identifier SQL (reviewed; false positives), one stdlib XML on untrusted feeds |
| `pip-audit` / `npm audit` | 26 advisories in 4 Python packages / 0 |
| Tests | 99 backend files / ~1576 tests; 8 frontend test files for 45 source files; **Purpose agent has 1 test file** |
| **Coverage (statements)** | **73%** overall (15,219 statements; 2339 tests passing). Lowest: `ml` 0%, `lookup` 31%, `rag` 55%, **Regwatch 58%**, `services` 58%, `jobs` 61%, `db` 67%, `api` 67%, Consent agent 68%. Highest: DSR 92%, Breach 91%, `core` 86%, `rules` 85%. Least-covered large files: `ropa_run_service` 16%, Regwatch `collection_service` 21%, `purpose_run_service` 22%, `ropa_repository` 25%, Regwatch `assessment_service` 26%, `incident_repository` 27%, ROPA `connectors/api.py` 34% — the orchestration and persistence layers where several findings above sit. |

## Test gaps worth closing (from the reviews)

No test for role gating on any non-DSR agent; none for a member minting scoped keys; none for ROPA connector SSRF or `credential_ref`; no live cross-tenant tests for ROPA/Regwatch/Purpose beyond `test_rls_enforcement`; none for negation in breach classification, empty-evidence risk, naive datetimes, or the ROPA sensitive-column cases above; none that Purpose job payloads are scrubbed.

## Recommended order of work

**This week (security, small changes):**
1. Fix C1: admin-only source CRUD, `credential_ref` prefix allowlist, SSRF guard and no redirects in ROPA and Regwatch.
2. Fix H1: admin-only key minting/revocation and a scope ceiling for non-admins.
3. Check the **deployed** values of `OPEN_ACCESS`, `SUPABASE_URL`, `CONSIVA_JWT_SECRET` (strength) and the DB role's superuser flag; add the `OPEN_ACCESS`-in-production startup refusal; unset the Supabase settings (H5).
4. Upgrade PyJWT ≥ 2.15, pypdf ≥ 6.19, urllib3 ≥ 2.8; build the image from the lock file.
5. Search the audit log for unusual `credential_ref` values and `test-connection` calls since launch.

**Next:** complete the RLS cutover; role matrix for mutating routes; login rate limiting and JWT-secret validation; propagate scan completeness into the rules, fix R-003 timing and retry idempotency, add a job heartbeat; breach close/notification gate, negation handling and zero-evidence routing; harden the ROPA classifier; `not is_global` in the SSRF guard; CI (frontend tests, `permissions:`, audit steps); nginx security headers.

**Later:** DNS pinning for all outbound connections, bounded streaming downloads, dead-job reconcilers for all agents, pagination/limits on remaining list endpoints, documentation refresh.

## Limits of this audit

Static review plus targeted execution of pure functions and local reproductions; no penetration test, no load test, and no review of the deployed server's actual configuration. Coverage is line coverage only; it says what ran, not that it was asserted. Items tagged [R] come from focused read-only reviews and should be confirmed before remediation. The DSR agent and the Docker/migration work were covered in earlier tasks in this session.
