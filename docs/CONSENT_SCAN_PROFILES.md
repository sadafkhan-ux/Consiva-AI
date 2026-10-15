# Consent scan profiles

Two scan modes share **one scanning engine**. They differ only in the budget they are given.
The **server** picks the profile from the endpoint called and the caller's authorization; nothing
in a request body can choose or widen it.

| | `web` | `application` |
|---|---|---|
| Endpoint | `POST /api/v1/consent-agent/scans/web` (also the original `POST /scans`) | `POST /api/v1/consent-agent/scans/application` |
| Pages per scan | at most `CONSENT_WEB_MAX_PAGES` unique pages (default **5**, never above 25) | **no fixed page-count cap** when `CONSENT_APP_MAX_PAGES=0` |
| Wall-clock bound | page limit × per-page timeout | `CONSENT_APP_MAX_SECONDS` (default 3600) for the crawl, plus a hard overall timeout |
| Who may call | any signed-in user, or a key with `consent:scan` | an **organisation admin**, or a service key with `consent:scan:application` |
| Domain | existing deployment policy (`SCANNER_REQUIRE_DOMAIN_VERIFICATION`) | **must have passed DNS ownership verification**, always |
| SSRF / same-site / robots / de-dup / concurrency / per-navigation timeout | applied | applied, identically |

`POST /scans` (the original endpoint) is now the **web** profile. It can never start an
application scan, so existing integrations get the 5-page limit and nothing else changes for them.
Console scans and scheduled monitoring scans also run as `web`.

## Configuration (`backend/.env`; documented in `backend/.env.example`)

```
CONSENT_WEB_MAX_PAGES=5      # 1-25. A request can only lower it.
CONSENT_APP_MAX_PAGES=0      # 0 = no fixed page-count cap; a positive number = an explicit cap
CONSENT_APP_MAX_SECONDS=3600 # 60-86400. What keeps "no page cap" finite.
```

All three are validated when settings load. An empty, non-numeric, negative or out-of-range value
**stops the app from starting**; a missing variable takes the default shown. Nothing here ever
means "unlimited" by omission or typo. `SCANNER_MAX_PAGES` is now only the fallback for code that
calls the crawler directly without a profile.

## What "no page-count cap" still bounds

- **Time:** the crawl stops *starting* pages after `CONSENT_APP_MAX_SECONDS` and the scan completes
  with what it found (`stop_reason: "deadline"`); a hard overall timeout aborts anything still running.
- **Cancellation:** `POST /scans/{id}/cancel` is honoured between batches. The crawl stops starting
  pages, finishes the batch in flight, keeps its evidence, and the scan stays `cancelled`; no
  analysis is queued. The browser is never killed mid-page.
- **Scope:** same site only, robots.txt respected, URLs de-duplicated, concurrency bounded
  (`SCANNER_MAX_CONCURRENT_PAGES`), per-navigation timeout, SSRF guard on every navigation.
- **Worker lease:** an application scan's job is given a lease longer than its own timeout, so the
  stale-job reaper does not reclaim a healthy scan and crawl the site a second time.

**Not bounded:** memory and database volume grow with the number of pages scanned inside the time
window, and one application scan occupies a worker slot (`WORKER_CONCURRENCY`) for up to the full
budget. Size `CONSENT_APP_MAX_SECONDS` for the host, or set `CONSENT_APP_MAX_PAGES` to a number.

Private, loopback and link-local targets are refused for both profiles. "Internal application"
means your own application on a routable address.

## Verify locally

```bash
cd backend
# unit + integration tests (synthetic sites, fakes; no network)
python -m pytest tests/test_scan_profiles.py -q

# invalid configuration refuses to start -- it must raise a ValidationError.
# Set the variable for ONE command, in whichever shell you use:
#   bash / Git Bash:   CONSENT_WEB_MAX_PAGES=0 python -c "from app.config import get_settings; get_settings()"
#   Windows cmd:       set CONSENT_WEB_MAX_PAGES=0 && python -c "from app.config import get_settings; get_settings()"
#                      (then: set CONSENT_WEB_MAX_PAGES=   to clear it in that window)
#   PowerShell:        $env:CONSENT_WEB_MAX_PAGES="0"; python -c "from app.config import get_settings; get_settings()"
#                      (then: Remove-Item Env:CONSENT_WEB_MAX_PAGES)

# API (replace TOKEN; use an admin token for /application)
curl -s -X POST http://127.0.0.1:8000/api/v1/consent-agent/scans/web \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"website_url":"https://example.com","authorized":true,"scan_options":{"max_pages":100}}'
# -> {"profile":"web","max_pages":5,...}   (the 100 was ignored)

curl -s -X POST http://127.0.0.1:8000/api/v1/consent-agent/scans/application \
  -H "Authorization: Bearer $ADMIN_TOKEN" -H "Content-Type: application/json" \
  -d '{"website_url":"https://app.example.com","authorized":true}'
# -> 403 unless the caller is an admin (or a key with consent:scan:application)
#    and the domain is DNS-verified; then {"profile":"application","max_pages":null,"max_seconds":3600,...}
```

To mint a key for the application profile, grant it the `consent:scan:application` scope.
