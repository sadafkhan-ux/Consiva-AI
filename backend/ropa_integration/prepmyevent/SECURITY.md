# Security Statement — Consiva ROPA Integration

For PrepMyEvent's engineering and security reviewers. Every claim below is
enforced in code, not by policy, and each names the mechanism so you can verify
it yourself.

---

## 1. PostgreSQL is never exposed

| Requirement | How it holds |
|---|---|
| No public database access | The adapter runs **on your VM**. Consiva never opens a connection to you — traffic is outbound only. |
| `listen_addresses` unchanged | Nothing in this integration touches PostgreSQL configuration. |
| Port 5432 stays closed | Never referenced from outside your host. |
| No new database user required | The recommended path reuses your existing `backend.database` engine. |

---

## 2. No credentials are shared with Consiva

Consiva never receives:

- your `DATABASE_URL`
- your database password
- `PII_ENCRYPTION_KEY` / `PII_HMAC_KEY` or any key material
- OAuth or SMTP tokens
- your internal hostnames or IPs (`sources[].location` is hardcoded `null`)

The **only** secret in the integration is the Consiva integration key, which
travels *from* you *to* Consiva as an `Authorization` header. It grants exactly
one permission: `evidence:write`. It cannot read your ROPA, cannot mint another
key, and cannot reach any other endpoint.

---

## 3. The database is read-only, structurally

The adapter issues **no SQL of its own**. All metadata comes from SQLAlchemy's
`Inspector`, which reads the system catalog.

An automated test asserts the SDK source contains no `insert into`, `update`,
`delete from`, `drop`, `alter`, or `truncate` statement:

```
tests/test_ropa_integration_adapter.py::test_adapter_module_issues_no_write_sql
```

On Consiva's own direct-connection connector (a different, optional path), the
session additionally sets `default_transaction_read_only = on`, so PostgreSQL
itself rejects writes. All seven forbidden operations were verified blocked
against a live database **while connected as a superuser**.

---

## 4. No personal data leaves your system

This is the guarantee that matters most, so it has three independent layers:

1. **No row reads.** The adapter never issues a `SELECT` against your tables.
2. **`sample_pattern` is hardcoded `null`.** The one field that could carry a
   value shape is never populated by this adapter.
3. **A test proves it.** `test_payload_contains_no_row_values` reads real values
   from a live database and asserts none appear anywhere in the payload.

Encrypted PII stays encrypted and stays where it is. Consiva has no key, no
ciphertext, and no need for either — a ROPA records *that* you process email
addresses, not *which* email addresses.

---

## 5. Allow-listed by default

Nothing is exported unless it is named in `ALLOW_LIST` in `adapter.py`.

- A table added to your schema later is **invisible** to Consiva until someone
  approves it.
- Column-level restriction is supported and stricter.
- A foreign key pointing at an unapproved table is **dropped**, so even the
  existence of an unapproved table isn't disclosed.
- Credential stores, token stores, and message bodies are deliberately excluded,
  with a test asserting they stay out.

Run `--dry-run` to see the exact payload before anything is transmitted. It
requires no credential and sends nothing.

---

## 6. Transport security

- **HTTPS enforced in code.** A non-`https://` base URL raises before any
  connection attempt.
- TLS verification is on by default.
- Bearer-token authentication on every request.
- Timeouts bounded (30s default).
- Retries only on transient failures (`429`, `5xx`, timeouts) — a `401`/`422` is
  never retried, so a bad credential can't become a hammering loop.

---

## 7. Secrets hygiene

- No secret is hard-coded. The key comes from the environment.
- **No secret is ever logged.** Log lines carry the correlation ID and counts.
- `.env.example` contains placeholders only.
- On Consiva's side the integration key is stored **only as a SHA-256 hash**;
  the plaintext is shown once at creation and never persisted. A database dump
  yields no usable credential.
- Configured data sources store a `credential_ref` — the *name* of an
  environment entry — never the secret. Rotation is an environment change with
  no database write.

---

## 8. Auditability

Every push is recorded on Consiva's side with the correlation ID, the
integration key that authenticated it, table/column counts, and the schema
version. Every subsequent human decision (approve / reject / edit) is a separate
audit row. Nothing an AI infers becomes an authoritative ROPA record without a
person approving it.

---

## 9. Availability isolation

**If Consiva is unavailable, PrepMyEvent is unaffected.** `run_adapter()`
catches every exception, logs it, and returns `None`. It cannot raise into your
application. Verified by
`test_run_adapter_never_raises_into_host_app`.

---

## 10. Reversibility

Disable the timer, delete `ropa_integration/`, ask Consiva to revoke the key.
No schema was altered, no business logic touched, no data modified. There is
nothing to roll back.

---

## Reviewer checklist

| Claim | Verify by |
|---|---|
| Sends no row values | `--dry-run`, inspect for any real value |
| Issues no writes | read `ropa_adapter_sdk.py`; it has no SQL |
| Only approved tables | `--dry-run`, compare against `ALLOW_LIST` |
| HTTPS enforced | set `CONSIVA_BASE_URL=http://…` → refused |
| No secret logged | run at `DEBUG`, grep output for the key |
| Survives outage | block the domain, run it, app keeps working |
