# Data Contract — PrepMyEvent → Consiva ROPA

**Schema version: `1.0`**

Compatibility: this backend accepts any `1.x`. New optional fields may be added
in a minor version and must never break an existing sender. A major version bump
means a breaking change and is rejected rather than parsed on a guess.

---

## Transport

| | |
|---|---|
| Method | `POST` |
| URL | `{CONSIVA_BASE_URL}/api/v1/ropa/evidence` |
| Auth | `Authorization: Bearer csv_<prefix>_<secret>` |
| Content type | `application/json` |
| Extra header | `X-Correlation-Id: <same as body>` |
| Direction | Outbound HTTPS only. Consiva never connects to PrepMyEvent. |

---

## Request body

```jsonc
{
  "schema_version": "1.0",
  "source_name": "prepmyevent.com",
  "correlation_id": "pme-a1b2c3d4e5f6a7b8",   // trace id, echoed in Consiva's audit log
  "generated_at": "2026-09-10T03:00:00+00:00", // when the SENDER collected it
  "adapter_version": "1.0.0",
  "idempotency_key": null,                     // optional; a repeat returns the SAME run
  "evidence": {
    "org_id": "external",
    "discovery_run_id": "adapter-1789023629",
    "sources": [
      {
        "local_id": "source-1",
        "name": "prepmyevent.com",
        "source_type": "database",
        "connector": "integration_adapter",
        "location": null                       // internal host is never disclosed
      }
    ],
    "tables": [
      { "local_id": "table-1", "source_local_id": "source-1",
        "schema_name": null, "table_name": "attendees" }
    ],
    "columns": [
      { "local_id": "column-1", "table_local_id": "table-1",
        "column_name": "email", "data_type": "VARCHAR(255)",
        "nullable": false,
        "sample_pattern": null,                // ALWAYS null from this adapter
        "existing_classification": "Contact Data",   // only if YOU declared it
        "existing_data_subject": "Attendee",         // only if YOU declared it
        "existing_purpose": "Event Attendee Management" }
    ],
    "relationships": [
      { "local_id": "rel-1",
        "from_table_local_id": "table-1", "from_column": "event_id",
        "to_table_local_id": "table-2",   "to_column": "id",
        "constraint_name": "fk_attendee_event" }
    ],
    "business_metadata": [
      { "local_id": "meta-1", "subject_local_id": "table-1",
        "business_owner": "Events Team", "retention_policy": "24 months",
        "department": null }
    ]
  }
}
```

### Field semantics

| Field | Meaning |
|---|---|
| `sample_pattern` | Would hold a redacted value shape. **This adapter always sends `null`.** |
| `existing_*` | Your own declarations. Consiva treats these as authoritative and never overwrites them with inference. Omitted = genuinely unknown. |
| `relationships` | Only between two **approved** tables. An FK pointing at an unapproved table is dropped so its existence isn't disclosed. |
| `local_id` | Stable within one push, used to cross-reference before anything has a database id. |

---

## Response — `201 Created`

```json
{
  "id": "f41ecef3-7560-4aae-9af6-674b714fcc34",
  "source_name": "prepmyevent.com",
  "ingest_mode": "evidence_push",
  "status": "completed",
  "tables_scanned": 10,
  "columns_scanned": 47,
  "personal_data_elements": 17,
  "overall_confidence": 0.94,
  "summary": { "processing_activities": 4, "risk_findings": 6, "detected_changes": 0 },
  "error": null
}
```

## Errors

| Status | Meaning | Adapter behaviour |
|---|---|---|
| `401` | Key invalid, revoked, or expired | **No retry** — fix the key |
| `403` | Key lacks `evidence:write` scope | **No retry** |
| `422` | Payload failed validation (unsupported version, size cap, bad timestamp) | **No retry** |
| `429`, `5xx` | Transient | Retries 3× with 1s / 2s / 4s backoff |
| timeout / connection refused | Consiva unreachable | Retries, then gives up and logs |

A failure never raises into PrepMyEvent and never leaves a partial run.

---

## Size limits

| | |
|---|---|
| Tables per push | 2,000 |
| Columns per push | 50,000 |

---

## Idempotency

Send the same `idempotency_key` to make a retry safe — Consiva returns the
**existing** run instead of starting a duplicate. Omit it and every push starts
a new run (correct for a scheduled weekly job).
