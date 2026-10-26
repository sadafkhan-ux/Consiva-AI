# Scaling the Consent Agent: many users, many scans

What was measured on one 8-CPU, 7.5 GB Docker host (26 Oct 2026), what changed because of it, and what it takes to serve more. Everything marked
"measured" was observed on this stack; everything marked "calculated" is arithmetic from those measurements, not a test at that scale.

## What limits what

| Part | Limit | Measured |
|---|---|---|
| **API** (polling, reading results) | database connections, held per request | 200 simultaneous users refreshing every 2 s: 0 errors, median 336 ms, p95 1.7 s. Before the fix, 5 simultaneous requests for a score failed for 30 s each. |
| **Scans** (a Chromium each) | **CPU**, then memory | Four scans at once peaked at 790% of 8 cores and 2.7 GB (0.5 GB idle worker, about 0.55 GB per scan). |
| **Job queue** | none in practice | 196 jobs claimed by 8 workers at once: 196 distinct, 0 duplicates, median claim 8 ms. |
| **Database** | `max_connections` (100) | backend 10+10, each worker replica 10+5 by default. |

A scan takes 20 to 100 s and about two cores while it runs. So one host of this size finishes roughly 4 scans per minute at full load (calculated).
**Serving 100 to 200 people who all start a scan in the same minute means queueing on one host (about 25 to 50 minutes for 200) or adding workers
(about 3 to 6 minutes with ten 8-CPU workers, calculated).** Nobody's scan fails for waiting; it waits its turn. This was not tested at 200 real scans:
that would mean 200 scans against live third-party sites, and it would only measure those sites' bot protection.

## 100 simultaneous scans: load-test results (27 Oct 2026)

Harness: `backend/loadtest/` with `docker-compose.loadtest.yml` (test-only; not in the production image). A fixture server answers as 100 virtual sites
(`site-NNN.loadtest.example`: different cookie/tracker/consent profiles, plus 5 deliberately difficult ones: very slow pages, HTTP 403, robots.txt block, flaky 503,
blank page). Nothing leaves the machine. 100 scan requests are submitted at once across 10 organisations through the real API, queue, worker and crawler. Each
site is first scanned alone to record a reference; every load result is compared with it. Run it: `... run_load --reference`, then `... run_load --scans 100 --orgs 10`.

| Run | Workers | Wall | Queue wait median / p95 | Scan median | CPU median / max | Accuracy vs scanned alone | Other users |
|---|---|---|---|---|---|---|---|
| Fixed 4 slots (before) | 1 | 220 s | 86 s / 180 s | 5 s | 32% / 93% | 100 of 100 | API 10 ms, 0 errors |
| Auto slots, light pages | 1 (8 slots) | 185 s | 48 s / 98 s | 6 s | 43% / 90% | 100 of 100 | API 10 ms before, 12 ms during, 0 errors |
| Auto slots, 3 workers | 3 | 133 s | 45 s / 91 s | 9 s | 83% / 98% | 100 of 100 | API 13 ms before, 15 ms during, 0 errors |
| Auto slots, heavy pages (600 ms CPU each) | 1 | 462 s | 153 s / 321 s | 27 s | 72% / 99% | 90 of 100 (bug, fixed below) | API 12 ms, 0 errors |
| Same, **re-run after the page-order fix** (27 Oct) | 1 | 348 s | 135 s / 269 s | 22 s | 75% / 98% | **100 of 100** | API 7 ms before, 12 ms during, 0 errors |

In every run all 100 requests were accepted in about 2 s, 98 scans completed and the 2 expected-failure sites failed with their reason (403, robots block), no job was retried
or run twice, and no scan contained anything from another site. The slowest scan in the light runs is the deliberately slow site (123 to 129 s). The heavy run was repeated after the fix below (last row; its reference was re-recorded in heavy mode, so it shows load matching the scan done alone). The 3-worker run used light pages.

Bottlenecks found and fixed:
* **Idle capacity:** fixed 4 slots left the CPU at 32% while scans queued; now slots follow the machine (above). Median queue wait 86 s to 48 s.
* **Slow site holding a slot:** a site whose pages all time out held a slot 184 s; it now stops after 3 consecutive page timeouts (`SCANNER_STOP_AFTER_CONSECUTIVE_TIMEOUTS`) and the report says so (123 s).
* **Wrong pages under load:** in the heavy run 10 scans chose different pages than when run alone, because the crawl filled its pool with sitemap URLs before the entered
  address had revealed its priority links (the privacy page). The entered address is now loaded alone first, so the page choice no longer depends on timing. Regression tests fail without the fix.

Limits of this evidence: fixture pages are lighter than real sites (the heavy mode approximates, not reproduces, them); the 100-scan runs were not repeated against 100 real
sites; no 200-scan run was done; one 8-CPU host was used.

## What changed

* **A request gives its database connection back before slow work.** The score, evidence and comparison endpoints held one connection while opening seven more
  to load evidence, so a handful of simultaneous requests deadlocked the pool (every one waited for connections the others were holding, until the 30 s pool
  timeout). Evidence loading is also bounded to two connections fewer than the pool, so it can never take the connection the next request needs.
* **The API pool is 10 + 10** (`BACKEND_DB_POOL_SIZE`, `BACKEND_DB_MAX_OVERFLOW`), up from 3 + 2 (sized for a 15-connection hosted pooler).
* **One cap on simultaneous page loads per worker process** (`SCANNER_MAX_CONCURRENT_NAVIGATIONS`, 0 = twice the CPU count, 6 to 32). Several scans in one worker
  used to load far more pages than the machine could, and pages timed out on each other; now a navigation waits its turn and its timeout starts when it does.
* **Fair turns between organisations.** The queue used to serve whoever submitted first until they were done. Now every organisation's first job goes before
  anyone's second, and an organisation already running jobs yields to one running none. Measured on the real queue: an organisation with 40 jobs waiting and 2
  running used to take all 8 free slots; it now gets 2 while two smaller organisations get 3 each. With one organisation the order is unchanged (oldest first).
* **Retries fit the failure** (see below), job attempts 3 to 6 with waits doubling from 30 s to 15 min (`JOB_MAX_ATTEMPTS`), and a job that cannot succeed is not run again.
* **The worker runs under an init process** (`init: true`), so the Chromium processes a crashed scan leaves behind are reaped instead of piling up.

## Adding capacity

1. **More workers:** `docker compose -f docker-compose.prod.yml up -d --scale worker=N`. Workers share the queue safely (`SELECT ... FOR UPDATE SKIP LOCKED`).
2. **Keep the connection total under the database limit.** Total = backend pool (20) + N x worker pool (15) + the checkpointer pools. With the defaults that is
   about 5 workers before 100 connections. Beyond that, put PgBouncer in front of the database or raise `max_connections`, and lower `WORKER_DB_POOL_SIZE`
   (a worker needs roughly its `WORKER_CONCURRENCY` plus 2).
3. **Slots per worker:** `WORKER_CONCURRENCY` (default 0 = decide from the machine). A worker runs at most one job per CPU, takes another only while
   the machine's CPU is under `WORKER_TARGET_CPU_PERCENT` (85) and memory remains (`WORKER_MEMORY_PER_JOB_MB`, `WORKER_MIN_FREE_MEMORY_MB`), a step at a time
   (`app/jobs/capacity.py`). An idle worker always takes one job, so a loaded machine still progresses. A number above 0 caps the slots instead.
   Several workers on one host each see the whole host's CPU, so they hold each other back instead of overloading it.
4. **More API processes** (`WEB_CONCURRENCY`) did not raise throughput in the test, because the load generator itself was the limit at about 75 requests per
   second; the server's own ceiling was therefore not measured. Each process has its own connection pools, so divide the pool settings accordingly.

## What a scan now does when a site does not cooperate

Real sites (20 large Indian and international ones) showed these, all now handled in `app/scanner/crawler.py` and `app/scanner/failures.py`:

| Situation | Before | Now |
|---|---|---|
| The site answers HTTP 403 "Access Denied" (tcs.com, infosys.com, india.gov.in, nytimes.com under load) | Scan "completed" and the report scored the error page (55.6%, "no privacy policy found") | Scan fails in seconds with the reason; no report |
| robots.txt forbids the scanner (npci.org.in, reddit.com) | "Completed" with 0 pages, no reason | Fails with the reason |
| The site resets the connection every time (irctc.co.in, makemytrip.com, zomato.com) | 3 blind retries; zomato hung for over 5 minutes | Retried for 10 s, then stops with the reason; no hang |
| Timeouts or "503 try again" | 3 attempts, 3 s of waiting | Doubling waits for up to 120 s for the entered address, 45 s for other pages; HTTP 429/5xx retried, `Retry-After` honoured |
| Certificate error, invalid address | Retried 3 times | Not retried |
| Pages that fail, time out or redirect away | Used up the page budget (hdfcbank.com scanned 1 of 5) | The 5-page limit counts pages actually scanned; attempts have their own ceiling (10) |
| Other pages answer 403 or 404 | Counted as scanned pages | Recorded as failed pages, with the status |
| A page that will not close | Scan stalled until the whole-scan ceiling | Closing is bounded to 10 s |

A site that blocks automated visitors cannot be made to work by retrying, and a scan of it says so rather than producing a report from an error page. Scans
of one site from one address trigger its bot protection sooner when many run at once; hdfclife.com answered 403 on 18 of its pages in the 16-scan test.

## No scan-count limits; robots.txt; why a site could not be reached (27 Oct 2026)

* **No attempt limits by default.** `SCANNER_MAX_SCANS_PER_ORG_PER_DAY` and `CONSENT_API_SCANS_PER_HOUR` now default to 0 (no limit); a burst queues and is worked through fairly instead of
  being refused with HTTP 429. Set a number above 0 to opt back in to a cost ceiling. Measured: 100 scans from one organisation (the old default refused after 50) all accepted, all finished, 0 errors.
  Retries remain bounded by time, with growing waits, and stop at once for failures retrying cannot fix.
* **robots.txt follows RFC 9309** (`app/scanner/robots.py`): `Allow`, wildcards, `$`, the group written for the scanner by name, longest match wins. Pages the site permits are scanned; forbidden pages are recorded as
  skipped ("robots_disallowed") and never assessed; a site that forbids the scanner entirely fails with that reason. A robots.txt that cannot be read stops the scan by default (see the table below).
* **Unreachable sites** say why (DNS, connection refused, proxy, timeout, no route, connection cut, certificate) in the failure message.
* Measured with the fixture harness: 20 simultaneous users from 20 organisations, 20 of 20 completed, identical to scanned alone, 0 errors, API 11 ms during the load.

## robots.txt: exactly what is and is not enforced (audited 27 Oct 2026)

Enforced (`app/scanner/robots.py`, `crawler._fetch_robots`): RFC 9309 `Allow`/`Disallow`, `*` and `$`, longest match with `Allow` winning ties, case-sensitive paths, percent-encoding compared
after normalising, the group named for this scanner (`ConsivaConsentAgent`, an exact token match) before `*`. A robots.txt that redirects (http to https, bare to www) is followed, up to 5 hops,
each hop checked as a navigation is (SSRF guard) and kept on the scanned site; a redirect to another site is not followed. Forbidden pages are recorded as skipped and never assessed;
a site forbidding the whole scan fails with the reason and no report.

Fail-closed handling is a crawler-safety and good-citizenship policy (RFC 9309), not an obligation of the DPDP Act or Rules; nothing in this product describes it as one.

**When robots.txt cannot be read** (policy `SCANNER_ROBOTS_UNREADABLE_POLICY`, default `disallow`):

| robots.txt answer | RFC 9309 | What the scanner does |
|---|---|---|
| 2xx | parse it | obey it (malformed lines are skipped; an HTML page or garbage restricts nothing) |
| 4xx (401, 403, 404, 410) | "unavailable": no rules | scan, nothing restricted |
| more than 5 redirects, or a redirect without a location | MAY treat as unavailable | scan, nothing restricted (logged at info) |
| 5xx, 429, timeout, network error | "unreachable": assume complete disallow | `disallow` (default): the scan fails with a retryable error, the queue retries with growing waits, and if the file stays unreadable the scan ends as failed ("could not read the robots.txt of ..., nothing was assessed"). `allow`: scan unrestricted, WARNING in the worker log |
| redirect to another site, or to an unsafe address | not followed here (scope/SSRF guard) | treated as unreadable, as the row above |

This changed on 27 Oct 2026: before, every unreadable case fell open silently. Consequence of the new default: a site whose robots.txt briefly errors now delays or fails its scan instead of
being scanned unchecked, and a site whose WAF answers robots.txt with HTTP 5xx or 429 for bots cannot be scanned until the setting is `allow` (a 403 is a 4xx and does not block). The
retry costs one cheap request, not a browser crawl. Set `SCANNER_ROBOTS_UNREADABLE_POLICY=allow` to restore the old behaviour, accepting that pages a site would have excluded may then be visited.

## Cost protection when scans are unlimited

With `SCANNER_MAX_SCANS_PER_ORG_PER_DAY=0` and `CONSENT_API_SCANS_PER_HOUR=0` nothing caps how many scans one organisation may queue. What still bounds the system: the worker never runs more
jobs than the CPU/memory governor allows, organisations are served fairly in turn, and a scan has its own time and page limits. What is not bounded: queue length and total browser time (so
cost grows with demand, and a flood from one organisation delays its own later scans but not others'). Recommended, configurable production settings: keep unlimited for normal tenants, but set a
generous ceiling (for example `SCANNER_MAX_SCANS_PER_ORG_PER_DAY=500`, `CONSENT_API_SCANS_PER_HOUR=200`) on API-key/integrator organisations and any free tier, and alert on queue depth. The purpose-classifier
daily limit (`PURPOSE_MAX_ASSESSMENTS_PER_ORG_PER_DAY`, a different agent) and the DSR one-time-code throttle are unrelated to scanning and unchanged. A local `backend/.env` may still carry old values
(this checkout's sets the daily limit to 1000000); `.env.example` and the compose default are 0.

## A domain that does not resolve

`ERR_NAME_NOT_RESOLVED` used to be retried for the whole 120 s window like a timeout. It is now retried for the short 10 s window and then stops with "the domain name did not resolve ... check the
spelling" (`dns_failure`, not retried by the queue). Trade-off: a resolver outage on the scanner's own network now also fails the scan quickly; the user can simply scan again.
`Retry-After` is honoured in both forms, delta-seconds and HTTP-date, capped at 30 s; a malformed value or a date in the past falls back to the normal backoff. Total waiting stays bounded by the retry window.

## Which safeguards exist, and which do not (inspected 27 Oct 2026)

| Safeguard | Exists? | Where |
|---|---|---|
| Jobs running at once per worker | yes, derived from CPU count, bounded by DB connections | `app/jobs/capacity.py`, `WORKER_CONCURRENCY` (0 = auto) |
| CPU and memory governor | yes (stops taking jobs at 85% CPU or low memory; ramps in steps) | `WORKER_TARGET_CPU_PERCENT`, `WORKER_MEMORY_PER_JOB_MB`, `WORKER_MIN_FREE_MEMORY_MB` |
| Fair turns between organisations | yes | `app/jobs/queue.py` |
| Browser time per scan | yes: whole-scan ceiling (`scan_budget_seconds`, profile ceilings), page and navigation windows, stale-job reaper | `app/config.py`, `app/scanner/scan_profiles.py` |
| Retries bounded | yes: 6 job attempts with doubling waits, time-windowed navigation retries | `JOB_MAX_ATTEMPTS` |
| Per-organisation scans per day / hour | yes, **off by default** (0 = unlimited) | `SCANNER_MAX_SCANS_PER_ORG_PER_DAY`, `CONSENT_API_SCANS_PER_HOUR` |
| Queue depth per organisation | yes, **off by default** (0 = no ceiling); above 0 a new scan gets HTTP 429 while that many of the organisation's scans are waiting | `SCANNER_MAX_QUEUED_SCANS_PER_ORG` |
| **Total browser-seconds per organisation per period** | **no** | |

Unlimited organisation scan limits do not mean unlimited processing: the worker never runs more than the machine can take and every scan has its own time ceiling. What is unbounded is the length
of the queue and the total browser time spent, so cost and waiting grow with demand. Risk: one organisation that queues thousands of scans delays only its own later scans (fair turns), but the
backlog, the database table and the cost still grow. `SCANNER_MAX_QUEUED_SCANS_PER_ORG` (added 27 Oct 2026, default 0 = no ceiling, so nothing changes until it is set) answers HTTP 429 once that many of an organisation's scans are waiting; running and finished scans do not count. Recommended: leave 0 for normal tenants; for integrator and free-tier organisations start near 20 to 50 (a few minutes of work for one 8-CPU worker at the measured rates) and tune from the observed queue wait; also alert on total queue depth and oldest-job age. Total browser seconds per organisation per period is still not limited (a product decision: per-plan quota).

## Rebuild and verification plan (not executed; needs authorisation)

Services in `docker-compose.prod.yml`: `db`, `migrate` (one-shot, same image as backend), `backend`, `worker`, `frontend`. Backend, worker and migrate share the image `consiva-backend:latest`; `frontend` is `consiva-frontend:latest`.
No new migration since 0044 (the database is at `0044_render_reports_job_type`), so `migrate` has nothing to apply.

1. Build: `docker compose -f docker-compose.prod.yml build backend frontend`
2. Drain first if scans are running (a worker restart interrupts them; the stale-job reaper requeues them after its threshold, so they are not lost but restart from the beginning).
3. Replace: `docker compose -f docker-compose.prod.yml up -d migrate backend worker frontend` (recreates only containers whose image changed; `db` is untouched). Impact: API unavailable for roughly the backend's start period (about 30 s); frontend a few seconds; scans in progress restart.
4. Verify the code is in the running containers (should print a count above 0 for each): `docker exec consiva-backend-1 grep -c scanner_robots_unreadable_policy /app/app/config.py`; `docker exec consiva-worker-1 grep -c "class RobotsUnreadable" /app/app/scanner/failures.py`; `docker exec consiva-backend-1 grep -c _compliance_status /app/app/services/consent_agent_api_service.py`; `docker exec consiva-backend-1 grep -c scanner_max_queued_scans_per_org /app/app/config.py`. Frontend: `docker exec consiva-frontend-1 sh -c "grep -l 'robots.txt Unreadable' /usr/share/nginx/html/assets/*.js"`.
5. Verify the effective policy (the variable is not set in `backend/.env`, so the code default applies): `docker exec consiva-backend-1 python -c "from app.config import get_settings as g; s=g(); print(s.scanner_robots_unreadable_policy, s.scanner_max_queued_scans_per_org, s.scanner_max_scans_per_org_per_day, s.consent_api_scans_per_hour)"` (expect `disallow 0 ...`). Note `backend/.env` here sets `SCANNER_MAX_SCANS_PER_ORG_PER_DAY` to a very large number; remove it to use the new default.
6. Health: `docker compose -f docker-compose.prod.yml ps` (backend, frontend, db healthy; worker running) and `docker logs consiva-worker-1 | grep "starting: up to"`.

## Queue and daily-limit settings: what each value means, and the recommendation (27 Oct 2026)

Checked in code (`app/services/scan_service.py`, `app/api/v1/routes/consent_agent.py`, `app/config.py`) and by tests (`tests/test_scan_limits.py`):

| Variable | 0 means | Above 0 means | Default |
|---|---|---|---|
| `SCANNER_MAX_SCANS_PER_ORG_PER_DAY` | no limit (the count is not even queried) | scans created by an organisation in the last 24 h; at the limit, HTTP 429 | 0 |
| `CONSENT_API_SCANS_PER_HOUR` | no limit | same, per hour, integration API only | 0 |
| `SCANNER_MAX_QUEUED_SCANS_PER_ORG` | no ceiling | scans of one organisation with status `queued`; at the ceiling, HTTP 429 | 0 |
| `SCANNER_ROBOTS_UNREADABLE_POLICY` | (not numeric) | `disallow` or `allow` | `disallow` |

`backend/.env` in this checkout sets the daily limit to `1000000`, which is "unlimited" in practice and means the safe default of 0 is not in force in that file. Delete the line (do not edit it blindly: it is local, gitignored configuration).
Per-page (30 s), seed (120 s) and page (45 s) navigation windows, the 3-timeout slow-site stop, the whole-scan ceilings and the worker CPU/memory governor are separate and already bounded.

**Tier-specific limits are not supported.** `organisations` has no plan or tier column, so "integrator" and "free tier" cannot be told apart in code. What can be told apart today is the route: the integration API (`POST /consent-agent/scans`) already has its own hourly quota. The smallest safe implementation, if you want it, is
a second opt-in variable read only by that route (for example `CONSENT_API_MAX_QUEUED_SCANS`, 0 = no ceiling) using the same `count_queued_scans`, leaving the browser UI route and every other organisation untouched. A real per-plan limit would need a plan column and a migration: a product decision.

**Recommended values** (evidence: measured fixture throughput on one 8-CPU host was about 0.3 to 0.5 scans per second, and earlier real-site measurements about 4 scans per minute per host, so 20 waiting scans is roughly 5 minutes of work on one real-site host):
`SCANNER_MAX_QUEUED_SCANS_PER_ORG=50` as a global backstop (at most about 12 minutes of one host's work from a single organisation, large enough that a normal user never sees it), and, once the route-specific variable exists, `20` for integration callers.
Leave the daily and hourly limits at 0 unless cost control requires them; if it does, start at `500` per day and `200` per hour and tune from observed use. These numbers are estimates from the measurements above, not tested limits.
