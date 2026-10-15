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

All require an integration key with `dsr:operate` (or a signed-in user). Tenancy comes from the
credential; nothing accepts an org id from the caller.

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

### Importing

Use the portal's request id as `idempotency_key`. A second import with the same key returns the
existing case, so a retry never creates a duplicate, and `reconcile` can match records by it.
Do not import a request the portal has already closed.

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

## Known limits

- No outbound notification from the agent to the portal. Polling is the only mechanism.
- A case that closes `PARTIALLY_COMPLETED` is terminal; a failed action in a partially
  completed case cannot be reopened. Only a case that failed *entirely* during execution can be
  retried with `retry-execution`.
- The portal's status vocabulary is unconfirmed (see above).
