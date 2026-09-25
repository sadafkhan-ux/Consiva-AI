# Consiva Consent Agent API

Scan a website for DPDP consent compliance. Four endpoints.

**Base URL:** `https://<host>/api/v1/consent-agent`
**Interactive docs (Swagger):** `https://<host>/docs` — *confirm this loads before
relying on it; on some deployments the reverse proxy serves the console app at that
path instead. This document is the authoritative reference either way.*

---

## How it works

```
POST /scans          →  scan_id, immediately (the scan runs in the background)
GET  /scans/{id}/status    →  poll until status is "completed"
GET  /scans/{id}/findings  →  the compliance findings
GET  /scans/{id}           →  everything (evidence, stages, timings)
```

A scan takes **10 seconds to ~3 minutes** depending on site size. It crawls the site,
clicks Accept, clicks Reject, and compares what tracking fires in each state.

---

## Authentication

Every request needs a bearer token from the normal Consiva login.

```http
Authorization: Bearer <access_token>
```

```bash
curl -X POST https://<host>/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"you@example.com","password":"..."}'
```

The token identifies your organisation. You can only ever see your own scans — another
organisation's `scan_id` returns 404.

---

## 1. Start a scan

```http
POST /api/v1/consent-agent/scans
Authorization: Bearer <token>
Content-Type: application/json
Idempotency-Key: <any unique string>     ← optional, recommended
```

```json
{
  "website_url": "https://example.com",
  "authorized": true
}
```

**`authorized` must be `true`.** It means "I own or am allowed to scan this domain".
Leave it out and the request is refused.

**Response — `202 Accepted`**

```json
{
  "scan_id": "430b561b-3a5a-48b2-9212-ab2fd57de200",
  "status": "queued",
  "message": "Scan queued successfully",
  "idempotent_replay": false
}
```

### Optional: limit the scan

```json
{
  "website_url": "https://example.com",
  "authorized": true,
  "scan_options": { "max_pages": 10 }
}
```

`max_pages` is capped at the server's limit (25). Ask for more and you get 25, not an
error.

### Idempotency-Key

Send one and a repeated request returns the **same** scan instead of starting a second
one. The replay returns `200` with `"idempotent_replay": true`.

Use it on every retry path — a network timeout on this call tells you nothing about
whether the scan started.

---

## 2. Poll status

```http
GET /api/v1/consent-agent/scans/{scan_id}/status
```

```json
{
  "scan_id": "430b561b-3a5a-48b2-9212-ab2fd57de200",
  "website_url": "https://example.com",
  "status": "running",
  "progress": 82,
  "pages_discovered": 25,
  "pages_scanned": 25,
  "pages_failed": 0,
  "current_stage": "llm_analysis",
  "started_at": "2026-09-24T09:46:18Z",
  "completed_at": null,
  "error": null
}
```

`status` → `queued` · `running` · `completed` · `failed` · `cancelled`

**Poll every 5 seconds.** It's a cheap query.

`progress` is real — it comes from which pipeline stages have finished, not a timer.
It reaches 100 only when the scan is finished.

### One thing to handle

You can get `status: "completed"` **with** an `error` object:

```json
"error": {
  "code": "STAGE_FAILED",
  "message": "Stage 'llm_analysis' failed: Request timed out.",
  "recoverable": true
}
```

This means the scan **did** find real problems, but the step that writes the
plain-English explanation didn't finish. The findings are measured facts; they just
have no written narrative and no legal citations.

Show them, but say they're unexplained. Don't present them as a normal result, and
don't hide them — the underlying violations are real.

```js
if (state.status === "completed" && state.error) {
  showBanner("Findings are available but not yet explained — re-run the analysis for detail.");
}
```

---

## 3. Get findings

```http
GET /api/v1/consent-agent/scans/{scan_id}/findings
```

```json
{
  "scan_id": "430b561b-3a5a-48b2-9212-ab2fd57de200",
  "findings": [
    {
      "id": "c56a2eac-b965-4168-bc6d-e04c4db334f6",
      "severity": "high",
      "category": "marketing",
      "title": "Marketing trackers continue to fire after the visitor clicks Reject",
      "description": "Marketing trackers continue to fire after the visitor clicks Reject (e.g. ad.doubleclick.net, bat.bing.com), indicating the reject choice is not honoured.",
      "evidence": [{ "type": "evidence_ref", "local_id": "t28" }],
      "confidence": null,
      "status": "pending",
      "requires_human_review": true,
      "recommendation": "Block marketing tags until consent is given, and wire Reject to your tag manager's consent mode.",
      "dpdp_references": [
        { "chunk_id": "9bf02e1d-…", "source_doc": "DPDP Rules", "section": null, "version": "2025" }
      ],
      "created_at": "2026-09-24T09:47:20Z"
    }
  ]
}
```

| Field | Notes for the UI |
|---|---|
| `severity` | `high` · `medium` · `low` — use for sort order and colour |
| `title` | Short. Safe for a card heading. |
| `description` | Full text. Safe to render as a paragraph. |
| `recommendation` | What to do about it. Can be `null`. |
| `requires_human_review` | **Always `true` for high severity.** Show a "needs review" marker. |
| `confidence` | **Always `null` today.** The pipeline doesn't record one. Don't render a confidence bar. |
| `status` | `pending` until someone reviews it in the console |
| `dpdp_references` | Legal citations. Empty when the analysis degraded (see §2). |

---

## 4. Get the full result

```http
GET /api/v1/consent-agent/scans/{scan_id}
```

Returns the complete scan. Top-level keys:

| Key | What's in it |
|---|---|
| `status`, `duration_ms`, `started_at`, `completed_at` | Run metadata |
| `scan_options` | What actually ran (after clamping) |
| `page_stats` | `pages_discovered`, `pages_scanned`, `pages_failed`, `pages_timeout`, `pages_blocked_robots` |
| `consent_mechanism` | `mechanism_type`, `cmp_vendor`, `confidence`, `detection_source` |
| `consent_states` | `accept_interaction`, `reject_interaction`, plus per-state cookie/tracker counts |
| `evidence_counts` | Totals per type |
| `cookies`, `trackers`, `third_party_services`, `forms`, `policies` | The itemised evidence |
| `findings` | Same shape as §3 |
| `stages` | Per-stage name, status, duration — good for a progress timeline |
| `token_metrics` | LLM cost for this scan |
| `errors` | Any stage errors |

### Reading consent state correctly

```json
"consent_states": {
  "accept_interaction": "clicked",
  "reject_interaction": "click_failed",
  "trackers_by_consent_state": { "pre_consent": 863, "post_accept": 107, "post_reject": 103 }
}
```

Only `"clicked"` means the button was really pressed. On any other value
(`click_failed`, `cmp_not_automatable`, `cmp_not_found`, `page_unreachable`) that pass
**did not establish** the consent state it's named after — so don't tell the user
"tracking continued after Reject" based on it.

`scanner_version` is currently always `null`.

---

## Errors

Every error uses one shape. **Branch on `code`**, not on `message`.

```json
{ "error": { "code": "SCAN_NOT_FOUND",
             "message": "Scan 430b561b-… not found",
             "scan_id": "430b561b-…" } }
```

| Code | HTTP | Meaning |
|---|---|---|
| `VALIDATION_ERROR` | 422 | Bad body. `fields` lists what. |
| `NOT_AUTHORIZED` | 403 | Missing `authorized`, or the URL points somewhere we won't fetch |
| `SCAN_NOT_FOUND` | 404 | No such scan **in your organisation** |
| `RATE_LIMIT_EXCEEDED` | 429 | 20 scans/hour per organisation |
| `SCAN_CONFLICT` | 409 | Operation clashes with the scan's state |
| `SCAN_TIMEOUT` | 504 | Scan exceeded its maximum duration |
| `INTERNAL_ERROR` | 500 | Unexpected. Never contains a stack trace. |

Private, internal and cloud-metadata addresses are rejected with `NOT_AUTHORIZED` —
this is deliberate SSRF protection, not a bug.

---

## Limits

| | Default | Server setting (ask us to change) |
|---|---|---|
| Scans per organisation, per hour | 20 | `CONSENT_API_SCANS_PER_HOUR` |
| Scans per organisation, per day | configured | `SCANNER_MAX_SCANS_PER_ORG_PER_DAY` |
| Pages per scan | 25 | `SCANNER_MAX_PAGES` |
| Per-page timeout | 30s | `SCANNER_TIMEOUT_SECONDS` |

A scan is a real browser crawl plus a model call, so it costs real time and money.
Poll an existing scan rather than re-submitting — that's what `Idempotency-Key` is for.

---

## Complete frontend example

```js
const API = "https://<host>/api/v1/consent-agent";
const headers = {
  "Authorization": `Bearer ${token}`,
  "Content-Type": "application/json",
};

async function scanWebsite(url, onProgress) {
  // 1. Start
  const { scan_id } = await fetch(`${API}/scans`, {
    method: "POST",
    headers: { ...headers, "Idempotency-Key": crypto.randomUUID() },
    body: JSON.stringify({ website_url: url, authorized: true }),
  }).then(r => r.json());

  // 2. Poll
  let state;
  do {
    await new Promise(r => setTimeout(r, 5000));
    state = await fetch(`${API}/scans/${scan_id}/status`, { headers })
      .then(r => r.json());
    onProgress(state.progress, state.current_stage);
  } while (state.status === "queued" || state.status === "running");

  if (state.status !== "completed") {
    throw new Error(state.error?.message ?? "Scan failed");
  }

  // 3. Findings
  const { findings } = await fetch(`${API}/scans/${scan_id}/findings`, { headers })
    .then(r => r.json());

  return {
    findings,
    // Non-null means the findings are real but unexplained -- see §2.
    degraded: state.error ?? null,
  };
}
```

**CORS:** your origin must be allow-listed server-side (`CORS_ALLOWED_ORIGINS`). Send
us the exact origins (e.g. `https://consiva.ai`) before you start. There is no wildcard.

---

## Appendix A — Summary endpoint

Pre-counted numbers for a dashboard card. Everything here can also be derived from
§3 and §4; this just saves you doing it.

```http
GET /api/v1/consent-agent/scans/{scan_id}/summary
```

```json
{
  "scan_id": "430b561b-…",
  "website": "https://example.com",
  "status": "completed",
  "pages_scanned": 25,
  "cookies_detected": 124,
  "trackers_detected": 857,
  "cmp_detected": true,
  "cmp_vendor": null,
  "cmp_confidence": 0.5,
  "consent_states_tested": { "before": true, "accept": true, "reject": true },
  "findings": { "critical": 0, "high": 2, "medium": 1, "low": 0 },
  "compliance_status": "issues_found"
}
```

`compliance_status` → `compliant` · `review_required` · `issues_found` · `unknown`

`consent_states_tested.accept` / `.reject` are `true` **only when the control was
actually operated.** An attempted click that failed reads `false` — its observations
don't establish the state they're named after.

---

## Appendix B — Cancel a scan

```http
POST /api/v1/consent-agent/scans/{scan_id}/cancel
```

```json
{
  "scan_id": "430b561b-…",
  "status": "cancelled",
  "message": "Scan cancelled; 1 queued job(s) removed. Work already in flight finishes its current step and is then discarded."
}
```

Queued jobs are removed immediately. A crawl **already running** finishes its current
step first — the browser runs in an isolated subprocess under its own time budget, and
killing it mid-navigation is what leaves orphaned Chromium processes behind. Evidence
already collected is kept; no further analysis is queued.

Cancelling an already-finished scan returns **409 `SCAN_CONFLICT`**.

---

## Appendix C — Webhooks (optional)

> **Check with us first.** Webhooks only work when the server has a signing secret
> configured. Without one, `POST /scans` with a `webhook_url` returns
> **`501 WEBHOOK_NOT_CONFIGURED`** — we refuse to send a callback the receiver
> couldn't authenticate. **Polling (§2) is the supported path today.**

Pass `webhook_url` on `POST /scans` and you get one POST when the scan finishes.

```json
{
  "website_url": "https://example.com",
  "authorized": true,
  "webhook_url": "https://your-system.example/webhooks/consent-scan"
}
```

The callback:

```json
{
  "event": "consent_scan.completed",
  "scan_id": "430b561b-…",
  "status": "completed",
  "website_url": "https://example.com",
  "result_url": "/api/v1/consent-agent/scans/430b561b-…",
  "completed_at": "2026-09-24T09:33:11Z"
}
```

`event` is `consent_scan.completed` or `consent_scan.failed`.

**The payload carries no findings or evidence** — deliberately. A webhook is a
notification; fetch the result back over the authenticated API. That way a mis-typed
webhook URL leaks only *that* a scan happened, not what it found.

### You must verify the signature

```http
X-Consiva-Event: consent_scan.completed
X-Consiva-Timestamp: 1790242263
X-Consiva-Signature: v1=e3e29c961d73…
```

HMAC-SHA256 over `"{timestamp}.{raw_body}"`, hex, prefixed `v1=`.

```python
import hashlib, hmac

def verify(raw_body: bytes, timestamp: str, signature: str, secret: str) -> bool:
    expected = "v1=" + hmac.new(
        secret.encode(), f"{timestamp}.".encode() + raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)
```

```js
import crypto from "node:crypto";

function verify(rawBody, timestamp, signature, secret) {
  const expected = "v1=" + crypto
    .createHmac("sha256", secret)
    .update(`${timestamp}.`).update(rawBody)
    .digest("hex");
  return crypto.timingSafeEqual(Buffer.from(expected), Buffer.from(signature));
}
```

Use the **raw** body bytes, not a re-serialised object — re-serialising changes key
order and whitespace and the signature won't match.

Reject anything whose timestamp is older than your tolerance (5 minutes is reasonable).
The timestamp is inside the signed material precisely so replays can be stopped.

### Delivery behaviour

- 10-second timeout; redirects are **not** followed
- Retried up to 3 times with linear backoff
- Any 2xx counts as delivered
- Every attempt is recorded server-side with its HTTP status and error
- Your URL must be `https` and publicly routable. Private, loopback, link-local and
  cloud-metadata addresses are refused at registration **and again at delivery** —
  DNS can be repointed in between.

---

## Not available yet

So you don't design around a gap:

- **No PDF/report export endpoint.** Build reports from `GET /scans/{id}`.
- **No unauthenticated scanning.** A pre-login "scan your site" homepage flow needs its
  own quota and abuse controls and hasn't been built.
- **No pagination.** A large site returns a large document.
- **`confidence` is always null.**
