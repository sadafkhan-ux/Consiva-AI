# Consent scan profiles

Two scan modes share **one scanning engine**. They differ only in the budget they are given.
The **server** picks the profile from the endpoint called and the caller's authorization; nothing
in a request body can choose or widen it.

| | `web` | `application` |
|---|---|---|
| Endpoint | `POST /api/v1/consent-agent/scans/web` (also the original `POST /scans`) | `POST /api/v1/consent-agent/scans/application` |
| Pages per scan | at most `CONSENT_WEB_MAX_PAGES` unique pages (default **3**, never above 3 -- a larger configured value is clamped; the main domain and any subdomain share this one budget) | **no fixed page-count cap** when `CONSENT_APP_MAX_PAGES=0` |
| Wall-clock bound | **no crawl deadline**; per-page timeout only; fixed 30 min ceiling for a hung scan | **no crawl deadline** by default (`CONSENT_APP_MAX_SECONDS=0`); optional deadline if set; fixed 24 h ceiling for a hung scan |
| Who may call | any signed-in user, or a key with `consent:scan` | an **organisation admin**, or a service key with `consent:scan:application` |
| Domain | existing deployment policy (`SCANNER_REQUIRE_DOMAIN_VERIFICATION`) | **must have passed DNS ownership verification**, always |
| SSRF / same-site / robots / de-dup / concurrency / per-navigation timeout | applied | applied, identically |

`POST /scans` (the original endpoint) is now the **web** profile. It can never start an
application scan, so existing integrations get the 3-page limit and nothing else changes for them.
Console scans and scheduled monitoring scans also run as `web`.

## Configuration (`backend/.env`; documented in `backend/.env.example`)

```
CONSENT_WEB_MAX_PAGES=3      # 1-3. A request can only lower it.
CONSENT_APP_MAX_PAGES=0      # 0 = no fixed page-count cap; a positive number = an explicit cap
CONSENT_APP_MAX_SECONDS=0    # 0 = no crawl deadline; 60-86400 = an optional deadline.
```

All three are validated when settings load. An empty, non-numeric, negative or out-of-range value
**stops the app from starting**; a missing variable takes the default shown. `CONSENT_APP_MAX_PAGES=0`
and `CONSENT_APP_MAX_SECONDS=0` are the explicit "no cap" / "no deadline" values. `SCANNER_MAX_PAGES` is now only the fallback for code that
calls the crawler directly without a profile.

## What "no page-count cap" still bounds

- **Time:** by default there is **no crawl deadline** for an application scan. If you set
  `CONSENT_APP_MAX_SECONDS` (60-86400) the crawl stops *starting* pages after that and the scan
  completes with what it found (`stop_reason: "deadline"`). A fixed 24 hour overall ceiling still
  aborts a scan that is hung, so a worker is never held forever.
- **Cancellation:** `POST /scans/{id}/cancel` is honoured between batches. The crawl stops starting
  pages, finishes the batch in flight, keeps its evidence, and the scan stays `cancelled`; no
  analysis is queued. The browser is never killed mid-page.
- **Scope:** same site only, robots.txt respected, URLs de-duplicated, concurrency bounded
  (`SCANNER_MAX_CONCURRENT_PAGES`), per-navigation timeout, SSRF guard on every navigation.
- **Worker lease:** an application scan's job is given a lease longer than its own timeout, so the
  stale-job reaper does not reclaim a healthy scan and crawl the site a second time.

**Not bounded:** memory and database volume grow with the number of pages scanned inside the time
window, and one application scan occupies a worker slot (`WORKER_CONCURRENCY`) for up to the full
ceiling. With no deadline, a very large site can run a long time: set `CONSENT_APP_MAX_SECONDS` or
`CONSENT_APP_MAX_PAGES` if you want a bound.

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
# -> {"profile":"web","max_pages":3,...}   (the 100 was ignored)

curl -s -X POST http://127.0.0.1:8000/api/v1/consent-agent/scans/application \
  -H "Authorization: Bearer $ADMIN_TOKEN" -H "Content-Type: application/json" \
  -d '{"website_url":"https://app.example.com","authorized":true}'
# -> 403 unless the caller is an admin (or a key with consent:scan:application)
#    and the domain is DNS-verified; then {"profile":"application","max_pages":null,"max_seconds":3600,...}
```

To mint a key for the application profile, grant it the `consent:scan:application` scope.

## What counts as a page (public scans)

A page is something the scanner navigated to. Skipped pages are not pages: links that `robots.txt` disallows, duplicate
spellings of a page, links left in the queue when the limit was reached, and redirects that land on an already-scanned
page or on another site are reported in the scan diagnostics (`skipped_by_reason`, and up to 50 URLs in `skipped_pages`)
with the reason, and never added to the pages list. Third-party requests are trackers, not pages.

A public scan stays on the entered host (with or without `www.`). Links to a subdomain of it, links to other sites, and
sites a scanned page displays in a frame are **not crawled**. They are recorded once per host in the scan diagnostics
(`discovered_domains`: host, `subdomain` or `external`, how often it was linked, one sample URL, whether it was framed) and
shown in the report under "Subdomains & External Domains — Review", together with the requests the scanned pages made to them
and the findings that involve them. To assess a subdomain, enter it as the address to scan.

**How the report keeps them apart.** The Trackers and Cookies sections, the three consent-state counts and the PDF summary
cover the scanned site's own evidence: requests to the site itself and to *named* analytics or advertising services it loaded,
and cookies set for its host, a parent domain, or by such a named service. Requests and cookies from a subdomain, or from a
separate website or service that is not a named tracker (a booking widget, a payment frame, a font host, a site shown in a
frame), are counted under "Discovered Subdomains & Websites" instead; nothing is dropped, each item is on exactly one side.
A finding whose evidence is all from discovered domains is listed in its own highlighted group in the findings section
(it still needs a review decision); a finding citing both is marked "also involves discovered subdomains / websites".
