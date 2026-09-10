# Consiva ROPA Integration Adapter — PrepMyEvent

This adapter lets PrepMyEvent contribute to a GDPR/DPDP **Record of Processing
Activities (ROPA)** without exposing your database, your credentials, or any
customer data.

**It sends column names and data types. It never sends row values.**

---

## 1. What this actually does

```
Your VM                                          │  Consiva
                                                 │
backend.database (your existing SQLAlchemy)      │
        │                                        │
        ├─ read system catalog (no row reads)    │
        ├─ filter to the approved allow-list     │
        ├─ transform to curated JSON             │
        └─ POST over outbound HTTPS ─────────────┼──▶ /api/v1/ropa/evidence
                                                 │         │
                                                 │         └─▶ classification →
                                                 │             purpose → ROPA →
                                                 │             human review
```

Your PostgreSQL stays on `localhost:5432`. Nothing is opened. No new listener,
no firewall change, no new database user required.

---

## 2. Installation

Copy two files into your backend:

```
backend/
  ropa_integration/
    __init__.py
    ropa_adapter_sdk.py          ← generic SDK
    prepmyevent/
      __init__.py
      adapter.py                 ← your configuration
      .env.example
```

No new Python packages required. The SDK uses only the standard library plus
SQLAlchemy, which you already have.

---

## 3. Configuration

```bash
cp ropa_integration/prepmyevent/.env.example .env.ropa
# edit .env.ropa — set CONSIVA_INTEGRATION_KEY and CONSIVA_BASE_URL
```

| Variable | Required | Notes |
|---|---|---|
| `CONSIVA_INTEGRATION_KEY` | yes | Issued by Consiva. Shown once. Format `csv_<prefix>_<secret>` |
| `CONSIVA_BASE_URL` | yes | Must be `https://` — plain HTTP is refused |
| `DATABASE_URL` | no | Leave blank to reuse `backend.database` (recommended) |

---

## 4. Run the dry run FIRST

**Do this before anything is sent anywhere.** It needs no key and transmits
nothing:

```bash
cd backend
python -m ropa_integration.prepmyevent.adapter --dry-run
```

It prints the exact JSON that *would* be sent. Read it. Confirm you are happy
with every table and column listed. You will see `"sample_pattern": null` on
every column — that is the field that would hold data, and it is always null.

---

## 5. Enable delivery

```bash
set -a && . ./.env.ropa && set +a
python -m ropa_integration.prepmyevent.adapter
```

Schedule it weekly with a systemd timer (matches how your backend already runs):

```ini
# /etc/systemd/system/consiva-ropa.service
[Unit]
Description=Consiva ROPA metadata push
After=network-online.target

[Service]
Type=oneshot
WorkingDirectory=/opt/prepmyevent/backend
EnvironmentFile=/opt/prepmyevent/backend/.env.ropa
ExecStart=/opt/prepmyevent/venv/bin/python -m ropa_integration.prepmyevent.adapter
User=prepmyevent
```

```ini
# /etc/systemd/system/consiva-ropa.timer
[Unit]
Description=Weekly Consiva ROPA push
[Timer]
OnCalendar=Sun 03:00
Persistent=true
[Install]
WantedBy=timers.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now consiva-ropa.timer
```

---

## 6. Database permissions

**Recommended: none.** Reuse `backend.database` and no new grant is needed —
reading the system catalog requires no special privilege.

If you prefer an isolated role:

```sql
CREATE USER ropa_readonly WITH PASSWORD '<generated>';
GRANT CONNECT ON DATABASE event_flow TO ropa_readonly;
GRANT USAGE ON SCHEMA public TO ropa_readonly;
-- Catalog access only. No table SELECT is needed: the adapter reads
-- information_schema, never your data.
```

Do **not** grant `INSERT`, `UPDATE`, `DELETE`, `DROP`, `ALTER` or `TRUNCATE`.
The adapter contains no code path that issues any of them.

---

## 7. What is sent

Controlled entirely by `ALLOW_LIST` in `adapter.py`:

| Sent | Not sent |
|---|---|
| Table names | Row values of any kind |
| Column names | Email addresses, names, phone numbers |
| Data types (`text`, `uuid`, …) | Encrypted PII blobs or keys |
| Nullability | OAuth / SMTP tokens |
| Foreign keys between approved tables | Message bodies |
| — | Your DB host, credentials, or `DATABASE_URL` |

The payload is a versioned contract (`schema_version: "1.0"`) carrying a
`correlation_id` you can quote to Consiva support. Consiva accepts any `1.x`, so
an added optional field will never break your installed adapter; a breaking
change would be `2.0` and would be announced before it ships. The exact JSON
shape, every field, and every error code are in **`DATA_CONTRACT.md`**.

To narrow it further, replace a table entry with an explicit column list:

```python
TableAllowList(table="attendees", columns=("id", "email", "created_at"))
```

Anything not listed then stays invisible to Consiva, even if it exists.

### Optionally: declare what you already know

If your team already knows a column's purpose or retention, declare it — Consiva
treats your declaration as authoritative and will not overwrite it with a guess:

```python
TableAllowList(
    table="attendees",
    business_owner="Events Team",
    retention="24 months",
    fields=(
        FieldDeclaration("email", is_personal_data=True,
                         personal_data_category="Contact Data",
                         data_subject="Attendee",
                         purpose="Event Attendee Management"),
        FieldDeclaration("internal_score", is_personal_data=False),
    ),
)
```

Add these to `DECLARED_TABLES`. Anything you leave out stays "unknown" and is
flagged for human review rather than guessed.

---

## 8. Testing

```bash
python -m ropa_integration.prepmyevent.adapter --dry-run    # no key, no send
python -m ropa_integration.prepmyevent.adapter --dry-run | python -m json.tool
```

To verify a real push end to end, run once manually and ask Consiva to confirm
the run appeared with your correlation ID (printed in the adapter's log line).

---

## 9. If Consiva is down

**Nothing happens to PrepMyEvent.** The adapter catches every error, logs it,
and exits non-zero. It never raises into your application, never retries
forever (3 attempts with backoff, then gives up), and never holds a database
connection open. A failed weekly push simply means the next one carries the
current state.

---

## 10. Disabling or removing the integration

```bash
# Pause
sudo systemctl disable --now consiva-ropa.timer

# Remove entirely
sudo rm /etc/systemd/system/consiva-ropa.{service,timer}
sudo systemctl daemon-reload
rm -rf backend/ropa_integration backend/.env.ropa
```

Then ask Consiva to revoke the integration key. Nothing in your schema or
business logic was ever modified, so there is nothing to roll back.

---

## 11. Support

Every push logs a correlation ID:

```
INFO ropa_adapter_sdk: ROPA adapter [pme-a1b2c3d4e5f6a7b8]: pushed 10 tables / 47 columns
```

Quote that ID to Consiva and they can trace the exact run end to end.
