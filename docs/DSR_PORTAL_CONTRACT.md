# DSR Agent ↔ Portal: status contract

**Audience:** whoever owns the portal that imports requests into the DSR Agent and shows their
status to staff and requesters. The portal's code is not in this repository, so this document
is the DSR side of the contract: what the agent guarantees, what it exposes, and what the
portal has to do.

## Why a "Resolved" request can be "Failed" in the agent

The two systems keep separate statuses and nothing reconciles them. The agent never calls the
portal back, so the portal only learns what it asks. The agent's status moves only when the
portal (or an operator) calls it. A portal that closes a request without calling
`/execution-report` and `/complete` leaves the agent case open or failed.

## The rule

> A portal may show a request as **resolved** only when
> `GET /api/v1/dsr/requests/{id}/outcome` returns `portal_may_show_resolved: true`.

Finding data, drafting a response, or attempting an action is never a resolution.

## Endpoints

All require the caller's signed-in bearer token (`POST /api/v1/auth/login`) -- the same login every
other Consiva screen uses, scoped to the caller's organisation. DSR has no key, token or scope of its own.
A system calling these endpoints (for example the portal) authenticates the same way, with an ordinary
login of the organisation; tokens last 12 hours. Nothing accepts an org id from the caller.

| Purpose | Call |
|---|---|
| Import a request, idempotently | `POST /api/v1/dsr/requests` with `idempotency_key` = **the portal's own request id** |
| Read the verified outcome | `GET /api/v1/dsr/requests/{id}/outcome` |
| Compare portal vs agent (read-only) | `POST /api/v1/dsr/requests/reconcile` |
| Push a lookup result (no DB access needed) | `POST /api/v1/dsr/requests/{id}/evidence` |
| Receive the work to do | `POST /api/v1/dsr/requests/{id}/execute` → `mode: "push"`, `manifest` |
| Report what was done | `POST /api/v1/dsr/requests/{id}/execution-report` |
| Close the case | `POST /api/v1/dsr/requests/{id}/complete` |
| Retry after a failed execution | `POST /api/v1/dsr/requests/{id}/retry-execution`, then `/execute` |
| Open cases for one requester | `GET /api/v1/dsr/requests/active?email=…` |

### Importing

Use the portal's request id as `idempotency_key`. A second import with the same key returns the
existing case, so a retry never creates a duplicate, and `reconcile` can match records by it.
Do not import a request the portal has already closed.

A request created **without** an `idempotency_key` (what the screens do) is refused with `409` when
another case for the same requester email is still open, unless the body carries
`duplicate_override_reason` (at least 5 characters). The reason, and the references of the cases it
duplicates, are written to that case's audit entry. Imports that carry an `idempotency_key` are not
subject to this: the key already says which request it is.

### Identity verification

* `POST /requests/{id}/identity/challenge` generates a code, shown once, in the form `K7QM-4TXR-9BWD`
  (letters and digits without look-alikes; case, spaces and dashes are ignored when it is typed back).
  **No email is sent** -- the response says `"emailed": false`, and whoever called it passes the code
  on. Only a hash is stored.
* Limits, so that asking again is no way round a lockout (`429` when exceeded; every refusal is
  recorded on the case's audit trail as `dsr.identity_challenge_refused`):
  3 codes per case in total; 60 seconds between codes; 15 minutes after a code is locked by wrong
  answers; 6 codes per requester email in 24 hours across all of that requester's cases.
* `POST /requests/{id}/identity/verify` with `{"challenge": "…"}` answers a code. The response carries
  `attempts`, `max_attempts` and `attempts_remaining`; five wrong answers lock that code.
* `POST /requests/{id}/identity/verify` with `{"manual": true, "method": "…", "reference": "…",
  "evidence_note": "…"}` records a verification by a person. `method` is one of `id_document`,
  `account_login`, `known_fact_match`, `in_person`, `other`; `reference` says what was checked;
  `evidence_note` is at least 10 characters. A body without `method` and `reference` is refused
  (`422`). All of it is written to the audit trail as separate fields.

### Responses

`POST /requests/{id}/response` is refused until the requester's identity is verified (`403`) and at
least one search has finished (`409`) -- a letter that says "we searched" or "we found nothing" is
a statement about a search, so there must have been one. A complaint (`grievance`) is the exception:
nothing is searched for it and its letter says so.

### `outcome` — the fields that matter

```json
{
  "public_status": "EXECUTING",
  "search":    { "state": "succeeded|partial|failed|no_match|not_started", "records_found": 3 },
  "approval":  { "required": 2, "approved": 2, "rejected": 0, "pending": 0 },
  "execution": { "executed_verified": 2, "failed": 0, "blocked": 0, "rejected": 0, "awaiting": 0 },
  "closure": "open|verified_complete|completed_nothing_to_do|partially_complete|failed|waiting_for_setup|ended_unfulfilled",
  "portal_may_show_resolved": false,
  "identity_status": "verified"
}
```

Search success, approval, execution and verification are reported **separately**.
`executed_verified` counts only actions whose read-back passed.

### Status mapping

`public_status` is a read-only projection of the stored status. No transition depends on it.

| Agent `public_status` | Meaning | Portal should show |
|---|---|---|
| `PENDING` | Received / classified | Open |
| `VERIFICATION_REQUIRED` | Waiting for the requester to prove identity | Open — identity not verified |
| `SEARCHING` | Searching connected systems | In progress |
| `AWAITING_APPROVAL` | A person must decide (includes grievances and unclassifiable requests) | Decision required |
| `EXECUTING` | Approved work in progress, or response not yet sent | In progress |
| `COMPLETED` | Verified outcome, requester answered | Resolved |
| `PARTIALLY_COMPLETED` | Closed, but not everything was done | Partially resolved |
| `FAILED` | An error stopped the case (may be retryable) | Failed |
| `WAITING_FOR_SETUP` | No data source connected; an administrator must set one up | Failed — setup required |
| `REJECTED`, `CANCELLED`, `EXPIRED`, `ESCALATED` | Ended or escalated | As named |

The machine-checked version of this table is `PORTAL_EXPECTATIONS` in
`backend/app/agents/dsr/services/outcome_service.py`. **The portal's own status names are not
known to this repository**; the mapping above uses the names seen in the live app (`Resolved`,
`Partially resolved`, `Decision required`, `Not verified`). Anything else is reported by
`reconcile` as *unmapped* rather than guessed. Please confirm or correct the table.

### Reconciliation (read-only)

`POST /requests/reconcile` takes up to 200 `{client_reference, portal_status,
portal_identity_verified}` items and returns, per item, whether the agent agrees and why not.
It **never changes either side**: which system is right is a human decision, and overwriting a
record would destroy the evidence of the mismatch. Run it on a schedule and alert on
`mismatches > 0`.

### Identity

The portal and the agent each store an identity status. `reconcile` flags a difference
(`identity differs: portal says not verified, agent says verified`). The agent's
`identity_status` in `outcome` is the one to display.

## What the portal must do

1. Import with the portal request id as `idempotency_key`; never import a closed request.
2. After a push-mode `/execute`, carry out each manifest action and call `/execution-report`
   with `done`, `failed` or `skipped`. A later `done` supersedes an earlier `failed`; a recorded
   `done` is never overwritten.
3. Call `/complete` only after reporting every action. The agent refuses while any approved
   action has no recorded outcome.
4. Show **Resolved** only when `portal_may_show_resolved` is true.
5. Poll `outcome` (or run `reconcile`) — the agent does not call the portal back.

### Reading cases

`GET /requests` and `GET /requests/{id}` return the same `identity_status` for a case. `GET
/requests/{id}/results` returns each evidence record with the `search_run_id` of the search that
found it; a record is only meaningful under a search that ran, and an empty `search_runs` list means
**no search has run** -- never "searched and found nothing".

## Known limits

- No outbound notification from the agent to the portal. Polling is the only mechanism.
- A case that closes `PARTIALLY_COMPLETED` is terminal; a failed action in a partially
  completed case cannot be reopened. Only a case that failed *entirely* during execution can be
  retried with `retry-execution`.
- The portal's status vocabulary is unconfirmed (see above).
