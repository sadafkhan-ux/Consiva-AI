# Enterprise Scanner API (for integration)

Base: `/api/v1/enterprise`  |  Auth: `Authorization: Bearer <service key csv_...>`

| Purpose | Method and path |
|---|---|
| Start scan | `POST /scans`  body `{"website_url": "https://example.com", "authorized": true}` |
| Status | `GET /scans/{scan_id}/status` |
| Coverage | `GET /scans/{scan_id}/coverage` |
| Score | `GET /scans/{scan_id}/summary` |
| DPDP matrix | `GET /scans/{scan_id}/parameters` |
| Findings | `GET /scans/{scan_id}/findings` |
| Create PDF | `POST /scans/{scan_id}/report` |
| Download PDF | `GET /scans/{scan_id}/report.pdf` |

## Get the Bearer token (service key), once

1. Log in as the admin: `POST /api/v1/auth/login` with `{"email": "...", "password": "..."}` and copy `access_token`.
2. Create the key (admin only):
   `POST /api/v1/ropa/integration-keys` with header `Authorization: Bearer <access_token>` and body
   `{"name": "consiva-io-enterprise", "scopes": ["consent:scan", "consent:scan:application"]}`
3. The response has `api_key` (starts with `csv_`). **It is shown only once**, so copy it now and keep it in the Consiva.io server secret store, never in browser code. Use it as the Bearer token above.

`consent:scan` is enough for the read endpoints. `consent:scan:application` is also needed for Start scan and Create PDF.
To list keys: `GET /api/v1/ropa/integration-keys`. To replace a lost key, create a new one.

Note: scan data (score, matrix, findings) is cleared 15 minutes after the scan finishes, so read and store it before then.
