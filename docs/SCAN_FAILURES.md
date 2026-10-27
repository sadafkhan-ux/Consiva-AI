# Why a scan could not be completed: one vocabulary for every website

Written 27 Oct 2026. What is verified by tests is marked **tested**; what was observed on live sites is marked **observed**; what is not yet running in a deployed service is marked **not deployed**.

## The shared pieces

| Piece | File | Decides |
|---|---|---|
| Failure catalogue | `backend/app/scanner/failure_catalog.py` | the 14 categories; for each a stable code, explanation, whether retrying can help, who must act, the next step; `scan_outcome` (the lifecycle word) |
| Retry classification and the scanner's exceptions | `backend/app/scanner/failures.py` | transient, refused, permanent; `ScanNotPossible`, `RobotsUnreadable`; `Retry-After` (seconds and HTTP-date) |
| robots.txt | `backend/app/scanner/robots.py`, `crawler._fetch_robots` | RFC 9309 matching; redirects; what an unreadable file means |
| Certificate facts | `backend/app/scanner/tls_diagnostics.py` | which certificate failed and why, for the explanation only |
| API and dashboard | `consent_agent_api_service.failure_and_outcome` -> `outcome` and `failure` on the status responses | the same description everywhere |

Nothing new is stored: the description is derived from the scan's error text and the crawl's recorded page counts, so scans stored earlier are described the same way. **No migration is needed.**

## Categories

`ROBOTS_TXT_DISALLOWED`, `ROBOTS_TXT_UNAVAILABLE`, `TLS_CERTIFICATE_EXPIRED`, `TLS_CERTIFICATE_INVALID`, `DNS_RESOLUTION_FAILED`, `CONNECTION_FAILED`, `REQUEST_TIMEOUT`, `HTTP_ACCESS_DENIED`,
`AUTHENTICATION_REQUIRED`, `RATE_LIMITED`, `SERVER_ERROR`, `BROWSER_NAVIGATION_FAILED`, `PARTIAL_SCAN`, `UNKNOWN_SCAN_ERROR`.

Permanent (retrying unchanged cannot help; the website's owner must act): robots disallow, expired or invalid certificate, access denied, authentication required, DNS failure, connection refused.
Transient (retried with bounded growing waits): unreadable robots.txt, timeout, rate limit (HTTP 429, honouring `Retry-After`), server error, unrecognised navigation failures.

## The lifecycle word (`outcome`)

`QUEUED`, `RUNNING`, `COMPLETED`, `COMPLETED_WITH_WARNINGS`, `PARTIALLY_COMPLETED`, `BLOCKED`, `FAILED` (and `CANCELLED`). Derived from the stored status, the error and what was assessed:

* a completed scan that assessed **no page** is never COMPLETED: it is BLOCKED (robots.txt, access denied, login) or FAILED;
* requested pages that failed, timed out or were refused by robots.txt make it PARTIALLY_COMPLETED, and `failure` (category PARTIAL_SCAN) carries the counts;
* COMPLETED_WITH_WARNINGS: it completed but could not test everything (Accept or Reject untested, or it stopped early on a slow site);
* BLOCKED means access was withheld (robots.txt disallows or cannot be read, HTTP 401/403), FAILED means something broke.

The existing stored `status` values (`pending`, `running`, `completed`, `failed`, `cancelled`) and the public API's `status` and `error.code` are unchanged; `outcome` and `failure` are additions.

## Retries

Navigation retries are time-windowed with exponential backoff and jitter (seed 120 s, other pages 45 s, a short window for a refusing or non-resolving site), at most 10 attempts, honouring `Retry-After`
(either form, capped at 30 s). The job queue retries a scan at most `JOB_MAX_ATTEMPTS` (6) times with doubling waits, and never retries a `ScanNotPossible`. A retry re-runs the same scan record, so it creates no second
scan, and evidence is saved once, when a scan completes. A scheduled scan is **not started again for `SCHEDULED_SCAN_PERMANENT_FAILURE_PAUSE_HOURS` (default 24, 0 = off)** after a permanent failure of the website's last
scan; the schedule moves on and tries again after the pause. A scan someone requests by hand is never held back. **tested**

## robots.txt

* Matching is RFC 9309 (longest match, `Allow` wins ties, `*` and `$`, the group named for the scanner, percent-encoding normalised). **tested**
* Explicit disallow -> `ROBOTS_TXT_DISALLOWED`, scan stops, nothing assessed. Unreadable (network error, timeout, 5xx, 429, redirect to another site) -> `ROBOTS_TXT_UNAVAILABLE`, scan stops and is retried; it is **not** treated as permission
  (`SCANNER_ROBOTS_UNREADABLE_POLICY=disallow`, the default; `allow` is a deliberate, logged operator choice). A 4xx means the site publishes no rules.
* robots.txt is **fetched fresh for every scan and not cached**: a scan fetches it once, so there is no stale rule to invalidate, and an owner who corrects the file sees the effect on the next scan.
* A URL fragment (`#/inbox/...`) is never sent to a server, so robots.txt is judged on the path alone; the browser still receives the full address, so a client-side route is loaded. The crawl identity drops fragments,
  so two fragment routes of one page count as one page. **tested**

## Certificates

Verification is never disabled for a scan. When verification fails, the certificate the server presents is read (public fields only: names, issuer, dates), by a handshake that sends no request, only to say which certificate
failed and since when. An expired or invalid certificate is a permanent failure and is never reported as a consent finding. **tested** (with generated certificates)

## The two known failures (observed 27 Oct 2026)

**`https://uat.consiva.ai/`** (and `consiva.ai`, `api.consiva.ai`, `uatapi.consiva.ai`): one wildcard certificate, `*.consiva.ai` (GoDaddy TLS Intermediate CA DV - R1v1), valid 2026-07-28 to **2026-10-26**, has expired. It names
all four hosts, so renewing that one certificate fixes all of them. It also explains why the Consiva consent banner never appeared on swaransoft.com, gignaati.com, hireflow.gignaati.com and masterclass.gignaati.com: the SDK is served from
`api.consiva.ai` (`masterclass` from `uatapi.consiva.ai`), so every visitor's browser also rejects the script. **Owner action: renew the wildcard certificate and install its full chain.** The scanner will not bypass it.

**`https://swaransoft.keka.com/`**: its robots.txt is `User-agent: *`, `Disallow: /`, `Allow: /careers`, `Allow: /careers/`. The whole site is disallowed for crawlers except the careers pages, so the scanner correctly refuses the root.
Whether the root page is public or behind a login was **not** checked, because that would mean requesting a disallowed page. Options: scan `https://swaransoft.keka.com/careers` (allowed by robots.txt, with your authorisation),
or, if Swaran Soft administers the Keka tenant and wants the root scanned, add an `Allow` for the scanner there. Keka is a hosted HR product; whether it lets a tenant edit robots.txt is for Keka to say.

Other sites in the same run: `facebook.com` disallows the scanner in robots.txt (ROBOTS_TXT_DISALLOWED: correct, nothing to fix); `masterclass.gignaati.com` lists sitemap pages that answer HTTP 404 (dead addresses, recorded as failed
pages; they no longer use up the attempt ceiling).

## What this does not do

It does not fix an external site's certificate or robots.txt, and does not make a technical failure into a compliance result. A scan that could not look says so (`Unable to Verify`), and no score, finding or report is produced from it.

## Not deployed

All of the above is in the working tree. The backend, worker and frontend images have **not** been rebuilt, so none of it runs in the services until they are (commands in `docs/SCALING.md`).

## Deployment setting for an unreadable robots.txt (27 Oct 2026)

`docker-compose.prod.yml` now sets `SCANNER_ROBOTS_UNREADABLE_POLICY` to `allow` for the backend and worker by default (override with the same variable in the environment). With `allow`, a robots.txt that cannot be read (timeout, 5xx, 429) logs a warning and the scan proceeds. A robots.txt that can be read and forbids the scanner is **always** obeyed, and the code default remains `disallow`.
