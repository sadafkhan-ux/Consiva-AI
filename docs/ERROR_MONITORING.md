# Error monitoring

One central record of every important error of both agents (Enterprise and the five-page agent) and the platform.

## What is recorded
Table `error_events` (migration 0061): one row per distinct problem (fingerprint of agent + service + category + HTTP status). Repeats raise `occurrences`; they do not create rows or emails.
Fields: error id (`ERR-YYYYMMDD-XXXXXX`), agent, module, service, group, category, severity (info/warning/critical), HTTP status, message, scan id, website URL, request id, notification status, resolution status (open / acknowledged / resolved). No keys, tokens or personal data are stored (`scrub()`).

## Sources
- `ErrorMonitorMiddleware` (every API response >= 400 and unhandled exceptions; adds `X-Request-ID`; unhandled errors answer JSON 500 with the reference).
- Worker job failures, Enterprise LLM calls, policy KB sync/retrieval, crawler (bot blocks), periodic system checks (database, Claude, NVIDIA, gateway, vector DB, embedding API), and failed five-page `agent_runs` (watcher).
- Normal validation errors (400/401/404/409/422 from our own API) are not emailed.

## Categories
Upstream 401/403/404/410 of an AI provider are configuration problems (critical, e.g. `model_unavailable` when the embedding model is retired). 429 = rate limit, 5xx/timeouts = temporary. Own-API statuses are classified separately from upstream ones. See `backend/app/monitoring/classify.py`.

## Email
Reuses the existing mailer/SMTP settings (`ALERT_SMTP_*`, `ALERT_EMAIL_TO`). Critical events (or those in `ALERT_EVENT_EMAIL_STATUSES`) are emailed once; repeats are suppressed for `ALERT_EVENT_COOLDOWN_MINUTES` (default 60); a recovery email follows when the service works again. If the database is down, a direct email is sent. Status is shown as "sent" only when the mail server accepted it. Without SMTP credentials nothing is sent and the event shows that.

## Bot / WAF blocks
`backend/app/monitoring/blocks.py` recognises challenge pages and 401/403/429/451 from the first pages. Retries stop, no score or report is produced, the scan ends with "Website blocked our scanner." (`BOT_BLOCKED` / `ACCESS_BLOCKED`), the dashboard shows a "Blocked" status, and an event/email is raised. The scanner never tries to bypass a CAPTCHA or WAF.

## Policy KB / RAG
The `/policy-kb` endpoints never answer an empty list on failure: they answer 503 with the error id, and the failure is recorded.

## Admin API and screen
`GET /api/v1/system/monitoring/errors[/summary|/{id}]`, `POST .../{id}/acknowledge|resolve` (administrators only). Screen: System health > Error monitoring (filters: agent, category, status, website, date).

## Gateway
The frontend nginx resolves `backend` per request (resolver 127.0.0.11), so recreating the backend no longer causes 502. After editing `nginx.conf`, rebuild the frontend image (a changed file without a reload is not applied).
