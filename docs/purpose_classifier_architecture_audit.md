# Purpose Classifier — Architecture Discovery Audit

**Scope:** read-only. No production code was modified, created or deleted.
**Method:** every claim below is traced to a file, table or endpoint that exists in the
repository or the live database. Where something does *not* exist, that is stated
rather than inferred from documentation.
**Date:** 2026-09-25

---

## 0. The finding that should shape the decision

**A purpose classifier already exists inside Agent 2 (ROPA).** It is not a stub.

| What | Where | Lines |
|---|---|---|
| Deterministic purpose rules | `app/agents/ropa/rules/purpose_rules.py` | 136 |
| Purpose + data-subject mapping | `app/agents/ropa/services/purpose_service.py` | 80 |
| Purpose → processing-activity grouping | `app/agents/ropa/services/processing_activity_service.py` | 127 |
| Controlled purpose vocabulary | table `purpose_taxonomy` (4 rows) | — |

`purpose_rules.RULES` already classifies ten business purposes:

```
Customer Account Management   Payment Processing        Recruitment
Employee Administration       Payroll / Compensation    Support / Ticketing
Event Attendee Management     User Account Management   Vendor / Supplier Management
Marketing Communications
```

And Agent 1 independently classifies a second, narrower purpose vocabulary —
`trackers.category` and `cookies.category` — whose values (`analytics`, `marketing`,
`functional`) I verified are **a strict subset of `purpose_taxonomy.code`**.

So the question this audit has to answer is not "where do we put a new classifier"
but **"is a new agent the right shape, given two already classify purpose?"**

My recommendation is in §14. It is not "build a new agent".

---

## 1. Repository architecture

| Component | Path | Purpose | Used by | Dependencies |
|---|---|---|---|---|
| FastAPI app | `app/main.py` | App, CORS, error envelopes, static mount | all | `api/v1/router` |
| Versioned router | `app/api/v1/router.py` | Registers every route; binds org scope once via `Depends(bind_request_scope)` | all routes | `core/security` |
| Routes | `app/api/v1/routes/*.py` | 11 modules, 103 endpoints | frontend, integrators | services |
| Auth | `app/core/security.py` | `CurrentUser`, `get_current_user`, `bind_request_scope` | every route | `core/tokens` |
| Integration auth | `app/core/integration_auth.py` | API-key auth for machine callers | ROPA evidence push | `ropa_integration_keys` |
| DB session | `app/db/session.py` | Engine, `async_session_factory`, `set_org_scope`, `apply_org_scope` | everything | `config` |
| ORM models | `app/db/models.py` | All tables | repositories | SQLAlchemy |
| Repositories | `app/db/repositories/*.py` | Query layer | services | models |
| Migrations | `backend/migrations/*.sql` (24) | Schema, RLS | `migrate.py` | — |
| RLS enforcement | `app/db/privilege_check.py` | Refuses to boot on a privileged role | startup | — |
| Job queue | `app/jobs/queue.py` | `enqueue`, `dequeue_batch`, retry/backoff, `cancel_jobs_for_scan` | all agents | `agent_jobs` |
| Worker | `app/jobs/worker.py` | Polls and dispatches 10 job types | all agents | services |
| LLM client | `app/llm/client.py` | Provider abstraction, self-hosted → Groq fallback, grammar-constrained output | agents 1, 2, 5 | `config` |
| Prompts | `app/llm/prompts.py` | Evidence compaction, token budgeting | agent 1 | `llm/schemas` |
| RAG | `app/rag/retriever.py`, `embedder.py`, `ingest.py`, `chunker.py` | pgvector retrieval over `knowledge_chunks` | agents 1, 5 | NVIDIA embeddings |
| Approval / review | `app/services/review_service.py` | Resumes a paused LangGraph run on a human decision | agents 1, 2 | `approvals` |
| Action tracking | `app/services/action_service.py` | Approved finding → tracked work, status machine | agents 1, 5 | `actions` |
| Audit | `app/services/audit_service.py` | Append-only `audit_logs` | all | — |
| Observability | `app/observability/stage_tracker.py` | Per-stage timing/status/metadata | agents 1, 2, 5 | `agent_run_stages` |
| Notifications | `app/services/notification_service.py` | Outbound webhooks (generic) | agents 3, 4 | — |
| Config | `app/config.py` (Pydantic `Settings`) | All env | everything | `.env` |
| Frontend | `frontend/src/components/*.tsx` | One console per agent | — | `api/client.ts` |

**Tenancy.** One model throughout: `org_id` on every tenant table, Postgres RLS forced
on 61 tables (`migrations/0018_rls_activation.sql`), `current_org_id()` reading the
`app.org_id` GUC. The API binds it per request; the worker connects as the owner and
bypasses RLS deliberately (`WORKER_DATABASE_URL` in `app/jobs/worker.py:54`).

---

## 2. Existing agents

| | Agent 1 Consent | Agent 2 ROPA | Agent 3 DSR | Agent 4 Breach | Agent 5 RegWatch |
|---|---|---|---|---|---|
| **Code** | `app/agents/consent_agent/` | `app/agents/ropa/` | `app/agents/dsr/` | `app/agents/breach/` | `app/agents/regwatch/` |
| **Router** | `routes/consent_scans.py`, `consent_findings.py`, `consent_agent.py` | `routes/ropa.py` | `routes/dsr.py` | `routes/incidents.py` | `routes/regwatch.py` |
| **Endpoints** | 17 + 6 | 13 | 17 | 24 | 20 |
| **Service** | `services/scan_service.py`, `analysis_service.py` | `services/ropa_run_service.py` | `services/dsr_run_service.py` | `services/incident_run_service.py` | `services/regwatch_run_service.py` |
| **Job types** | `scan`, `analyze`, `consent_api_chain`, `consent_webhook` | `ropa_discovery` | `dsr_search`, `dsr_execute` | `incident_analysis` | `regwatch_collect`, `regwatch_assess` |
| **Tables** | 14 | 7 | 11 | 12 | 8 |
| **LLM** | yes — `nodes/llm_reasoning.py` | yes — `services/enrichment_service.py` | no | no | yes — `services/assessment_service.py` |
| **RAG** | yes | no | no | no | yes |
| **Connectors** | Playwright scanner | postgres, rest_api | postgres | no | http, rss, manual_upload |
| **Frontend** | `ConsentAgentView.tsx` + 8 components | `RopaDashboard.tsx` | `DsrEmailFirst.tsx`, `DsrConsole.tsx`, `DsrDashboard.tsx` | `BreachConsole.tsx` | `RegWatchConsole.tsx` |
| **Input** | website URL | connector config / evidence push | email address | incident description | source URL |
| **Output** | findings + evidence | ROPA records + findings | action plan + response | incident report | findings + actions |
| **Orchestration** | LangGraph (checkpointed) | service pipeline | service pipeline | service pipeline | service pipeline |

Only Agent 1 uses LangGraph. The others are plain async service pipelines driven by the
worker — which matters for §10: a new agent does **not** need LangGraph.

---

## 3. Database architecture

69 tables. Those relevant to purpose classification:

| Table | Key columns | Relationships | Used by | Purpose-classifier relevance |
|---|---|---|---|---|
| `purpose_taxonomy` | `code`, `label`, `description` | none (no FKs) | seed data | **The controlled vocabulary already exists.** 4 rows: analytics, marketing, functional, other. Nothing references it by FK. |
| `ropa_records` | `processing_activity`, `payload` jsonb, `edited_payload`, `confidence`, `review_required`, `status`, `version`, `supersedes_id` | → `ropa_discovery_runs` | Agent 2 | **Declared purpose lives here**, inside `payload`. Versioned and human-editable. |
| `ropa_discovery_runs` | `status`, `source_id`, `started_at` | → `ropa_data_sources` | Agent 2 | Run container; a classifier run could reuse this shape. |
| `ropa_findings` | `finding_type`, `severity`, `status`, `evidence` | → run | Agent 2 | Finding shape for purpose mismatches. |
| `ropa_schema_baselines` / `ropa_schema_changes` | baseline vs observed | → source | Agent 2 | **Declared-vs-observed comparison already implemented** for *schema*. Same pattern applies to purpose. |
| `ropa_data_sources` | `connector`, `config`, `credential_ref` | → org | Agent 2 | Where a classifier would read source metadata. |
| `trackers` | `script_src`, `vendor`, **`category`**, `consent_states`, `source` | → `consent_scans` | Agent 1 | **Observed purpose per tracker.** Values verified ⊆ `purpose_taxonomy.code`. |
| `cookies` | `name`, `domain`, **`category`**, `vendor`, `consent_states`, `expiry` | → `consent_scans` | Agent 1 | Same. Plus retention signal via `expiry`. |
| `third_party_services` | `service_name`, **`category`**, `domains` | → `consent_scans` | Agent 1 | Vendor/processor mapping with purpose. |
| `cookie_lookup` | vendor/category reference data | none | Agent 1 | Pre-built purpose lookup (Open Cookie Database). |
| `consent_signals` | `mechanism_type`, `cmp_vendor`, `evidence` | → scan | Agent 1 | **Whether consent was actually obtained** — the lawful-basis half. |
| `policies` | `url`, `policy_type` | → scan | Agent 1 | Declared purpose source. **Note:** URL + type only, no extracted text. |
| `dsr_retention_rules` | retention config | → org | Agent 3 | **Retention policy already modelled.** |
| `actions` | `action_type`, `status`, `due_at` | → finding | Agents 1, 5 | Remediation tracking, reusable. |
| `approvals` | decision, reason | polymorphic | Agents 1, 2 | Human review, reusable. |
| `audit_logs` | `action`, `before`, `after`, `actor_user_id` | polymorphic | all | Audit, reusable. |
| `agent_jobs` | `job_type` (CHECK: 10 values), `payload`, `attempts` | → org | all | **Queue — would need one new `job_type` value via migration.** |
| `agent_runs` / `agent_run_stages` | `stage`, `status`, `duration_ms`, `metadata` | → scan | Agents 1, 2, 5 | Run/stage observability, reusable. |

**Tables that do NOT exist** (checked, not assumed): no `purposes`, no
`purpose_assignments`, no `data_flows`, no `lineage`, no `access_logs`, no
`processing_activities` table (processing activities live in `ropa_records.payload`).

---

## 4. ROPA agent — end to end

```
Connector config OR evidence push
  → POST /api/v1/ropa/sources/{id}/discover   (routes/ropa.py)
  → ropa_run_service.py                        enqueue job_type="ropa_discovery"
  → worker.py:112                              dispatch
  → services/discovery_service.py              connector reads schema
  → services/classification_service.py         personal-data detection
       ├─ rules/personal_data_rules.py
       ├─ rules/column_context.py
       └─ rules/purpose_rules.match_table()    ← purpose inference
  → services/purpose_service.py                purpose + data-subject per table
  → services/processing_activity_service.py    group tables BY PURPOSE
  → services/dataflow_service.py               source → storage → vendor path
  → services/risk_service.py                   findings
  → services/enrichment_service.py             LLM (optional)
  → ropa_records + ropa_findings
  → GET /api/v1/ropa/runs/{id}/records
```

**What ROPA already provides**
- Table→purpose classification, 10 purposes, confidence 0.9 exact / 0.7 token
- Data-subject mapping
- Purpose-grouped processing activities
- Data-flow mapping (source → storage → vendor)
- Declared-vs-observed comparison **for schema** (`ropa_schema_baselines` vs `ropa_schema_changes`)
- Versioned, human-editable records with `review_required`

**What ROPA does NOT provide**
- No *observed usage* input — purpose is inferred from **table names only**
  (`match_table(table_name)`), never from access logs, query logs or traffic
- No link to Agent 1's observed purposes — `trackers.category` and
  `ropa_records.payload.purpose` are never compared; nothing joins them
- No retention evaluation against purpose
- No declared-vs-observed comparison for **purpose** (only for schema)
- No FK to `purpose_taxonomy` — the vocabulary sits unused by ROPA

**Duplication risk: high.** A standalone Purpose Classifier that classifies tables would
reimplement `purpose_rules.py` + `purpose_service.py` + `processing_activity_service.py`.

---

## 5. Consent agent — end to end, and what is consumable

```
URL → POST /api/v1/consent-agent/scans → consent_api_chain
  → scanner/crawler.py  (pre-consent, post-accept, post-reject)
  → rules/consent_rules.py  classify_tracker / classify_cookie
  → nodes/retrieve_rag.py → nodes/llm_reasoning.py
  → consent_findings + evidence tables
```

**Consumable by a purpose classifier, via existing endpoints:**

| Data | Table | Endpoint |
|---|---|---|
| Tracker purpose + consent state | `trackers.category`, `.consent_states` | `GET /consent/scans/{id}/evidence` |
| Cookie purpose + retention | `cookies.category`, `.expiry` | same |
| Vendor/processor + purpose | `third_party_services` | same |
| Whether consent was obtained | `consent_signals.evidence.accept_interaction` | same |
| Declared policy documents | `policies.url`, `.policy_type` | same |
| Compliance findings | `consent_findings` | `GET /consent-agent/scans/{id}/findings` |

**The most valuable signal Agent 1 holds** is not the category — it is
`consent_states`. A tracker categorised `marketing` that fired with
`consent_states: ["pre_consent"]` is **observed processing without a lawful basis**.
That is precisely a declared-vs-observed purpose gap, and nothing currently computes it
across agents.

**Limitation:** `policies` stores a URL and a type. There is **no extracted policy
text** in the database — `extracted_text_ref` exists on the record but is excluded from
prompts and nothing dereferences it. So "declared purpose from the privacy policy"
cannot be read today without new extraction.

---

## 6. Connectors

| Connector | Path | Read | Write | Auth | Tenant scoping | Reusable? |
|---|---|---|---|---|---|---|
| PostgreSQL (ROPA) | `app/agents/ropa/connectors/postgres.py` | schema + samples | no | `credential_ref` | via `ropa_data_sources.org_id` | **Yes** |
| REST API (ROPA) | `app/agents/ropa/connectors/api.py` | endpoint reads | no | config | same | **Yes** |
| PostgreSQL (DSR) | `app/agents/dsr/connectors/postgres.py` | subject search | yes (erase/rectify) | `dsr_source_authorizations` | yes | Yes, with care |
| HTTP page (RegWatch) | `app/agents/regwatch/connectors/` | fetch + diff | no | optional token | `regwatch_sources.org_id` | Yes |
| RSS/Atom (RegWatch) | same | parse entries | no | same | same | Yes |
| Manual upload (RegWatch) | same | — | — | — | same | Yes |
| Browser scanner (Consent) | `app/scanner/crawler.py` | Playwright crawl | no | attestation + SSRF guard | `consent_scans.org_id` | Situational |

**Do NOT exist** — verified by inspecting `connectors/factory.py` in both agents:
MySQL, SQL Server, MongoDB, CSV, Excel, file storage, Google Drive, SharePoint,
S3/object storage, CRM.

The factory raises `ConnectorError(f"unsupported connector {connector!r}")` for anything
outside `postgres` / `rest_api`.

---

## 7. Usage / lineage evidence — what actually exists

This section matters most, because the conceptual flow in §9 depends on it.

| Capability | Status | Where |
|---|---|---|
| Audit logs | **EXISTS** | `audit_logs` + `app/services/audit_service.py`. Records *Consiva user actions*, not customer data access. |
| Agent run/stage logs | **EXISTS** | `agent_runs`, `agent_run_stages`, `app/observability/stage_tracker.py` |
| Webhook delivery log | **EXISTS** | `webhook_deliveries` |
| Vendor / processor mapping | **EXISTS** | `third_party_services` (Agent 1), `dataflow_service.py` (Agent 2) |
| Data-flow mapping | **PARTIAL** | `app/agents/ropa/services/dataflow_service.py` — source → storage → vendor, built only from evidence. **Not** column-level lineage. |
| Processing activity records | **EXISTS** | `ropa_records.processing_activity` + `payload` |
| Schema-change tracking | **EXISTS** | `ropa_schema_baselines`, `ropa_schema_changes` |
| **Customer API / access logs** | **DOES NOT EXIST** | No table, no ingestion path, no connector |
| **Query / usage logs** | **DOES NOT EXIST** | Purpose is inferred from table *names* only |
| **Data export logs** | **DOES NOT EXIST** | — |
| **Column-level lineage** | **DOES NOT EXIST** | — |
| **ETL/ELT metadata** | **DOES NOT EXIST** | — |
| **Event tracking** | **DOES NOT EXIST** | — |

**This is the decisive gap.** A Purpose Classifier that distinguishes *declared* from
*observed* purpose needs observed-usage evidence. The only observed-usage signal in the
entire platform is **Agent 1's `consent_states`** — what actually fired in a browser,
before and after consent. There is no server-side usage evidence at all.

---

## 8. Where a purpose classifier would live

Following the existing conventions exactly:

| Concern | Convention in this repo | For a purpose classifier |
|---|---|---|
| Agent package | `app/agents/<name>/` with `rules/`, `services/`, `schemas/`, `connectors/` | `app/agents/purpose/` |
| Router | `app/api/v1/routes/<name>.py`, prefix `/api/v1/<name>` | `routes/purpose.py` |
| Run service | `app/services/<name>_run_service.py` | `purpose_run_service.py` |
| Worker dispatch | `elif job.job_type == "..."` in `app/jobs/worker.py` | new branch |
| Job type | `agent_jobs.job_type` CHECK (currently 10 values) | **migration required** — the CHECK rejects unknown values (see `migrations/0023`) |
| Tables | `<name>_*` prefix, `org_id`, forced RLS, `grant ... to consiva_app` | new migration |
| Review | `app/services/review_service.py` + `approvals` | reuse |
| Actions | `app/services/action_service.py` + `actions` | reuse |
| Audit | `app/services/audit_service.py` | reuse |
| Frontend | one console component | `PurposeConsole.tsx` |

**Three traps this codebase has already fallen into**, all of which a new agent would hit:

1. **Enumerated CHECK constraints are invisible to Python.** Three migrations
   (0021, 0023, 0024) exist solely to widen one. Guards: `tests/test_job_types_match_schema.py`,
   `test_status_values_match_schema.py`, `test_stage_names_match_schema.py`.
2. **New tables must opt into RLS explicitly.** Migration 0018 forced it on existing
   tables; anything added later needs its own `enable`/`force`/`policy`/`grant`
   (see `migrations/0022` for the pattern).
3. **Background jobs have no org scope bound.** They work because the worker runs as
   owner. Any code path that might run under `consiva_app` must call `apply_org_scope`
   (see `app/services/webhook_service.py:_save`).

---

## 9. Required flow, mapped to the real codebase

| Step | Status | Reusable from | New work |
|---|---|---|---|
| Data Source | **EXISTS** | `ropa_data_sources`, `connectors/factory.py` | none |
| Consent / privacy context | **EXISTS** | `trackers`, `cookies`, `consent_signals`, `third_party_services` | a read adapter |
| ROPA / processing context | **EXISTS** | `ropa_records.payload`, `processing_activity_service.py` | a read adapter |
| **Usage / lineage evidence** | **DOES NOT EXIST** | only `consent_states` (browser-side) | **ingestion + storage + connector — the largest gap** |
| Purpose identification | **EXISTS** | `purpose_rules.py`, `purpose_service.py` | vocabulary reconciliation only |
| Current usage identification | **PARTIAL** | `consent_states` for web; nothing server-side | depends on the gap above |
| Processing activity mapping | **EXISTS** | `processing_activity_service.py` | none |
| **Declared vs observed** | **DOES NOT EXIST** | pattern exists for *schema* in `ropa_schema_changes` | **new comparison service** |
| Retention / reconsideration | **PARTIAL** | `dsr_retention_rules`, `cookies.expiry` | **evaluation against purpose** |
| Finding | **EXISTS** | `ropa_findings` / `consent_findings` shape | new table or reuse |
| Human review | **EXISTS** | `review_service.py`, `approvals` | none |
| Remediation | **EXISTS** | `action_service.py`, `actions` | none |
| Risk / audit | **EXISTS** | `audit_logs`, `audit_service.py` | none |

**Nine of thirteen steps already exist.** Two are genuinely missing: observed-usage
evidence, and the declared-vs-observed comparison.

---

## 10. Integration design

**Reuse, do not rebuild:**

| Need | Existing component |
|---|---|
| Purpose rules | `app/agents/ropa/rules/purpose_rules.py` |
| Purpose mapping | `app/agents/ropa/services/purpose_service.py` |
| Activity grouping | `app/agents/ropa/services/processing_activity_service.py` |
| Connectors | `app/agents/ropa/connectors/factory.py` |
| Queue + retries | `app/jobs/queue.py` |
| Review | `app/services/review_service.py` |
| Actions | `app/services/action_service.py` |
| Audit | `app/services/audit_service.py` |
| Stage timing | `app/observability/stage_tracker.py` |
| LLM (ambiguity only) | `app/llm/client.py` |
| Vocabulary | `purpose_taxonomy` |

**Reuse these tables:** `ropa_records`, `ropa_data_sources`, `trackers`, `cookies`,
`third_party_services`, `consent_signals`, `approvals`, `actions`, `audit_logs`,
`agent_jobs`, `agent_runs`, `agent_run_stages`, `purpose_taxonomy`.

**New tables likely required:**
- `purpose_assessments` — one declared-vs-observed comparison per activity/source
- `purpose_findings` — mismatches (or extend `ropa_findings.finding_type`)
- `usage_evidence` — **only if** server-side usage ingestion is in scope

**Must remain independent:** the comparison logic and any usage ingestion. Everything
else should be a caller of existing services.

**Avoid:** a second purpose vocabulary, a second connector factory, a second review
engine, a second audit trail. All four exist.

---

## 11. Dependency map

The requested chain, with the direction the code should actually take:

```
Agent 1 Consent ──┐
                  ├──► Purpose Context ──► Purpose Classifier ──► Processing Activity ──► ROPA
Agent 2 ROPA ─────┘
```

| Edge | Mechanism | Why |
|---|---|---|
| Consent → Purpose Context | **shared database model** (read `trackers`, `cookies`, `consent_signals`) | Agent 1 must not import purpose code. Its evidence tables are already the contract. |
| ROPA → Purpose Context | **service call** (`purpose_service`, `processing_activity_service` are pure functions over `DiscoveryEvidence`) | Reuses logic without duplicating it. |
| Purpose Classifier → Processing Activity | **shared data contract** (`ropa_records.payload`) | Read the declared purpose; do not write it. |
| Purpose Classifier → ROPA | **must NOT be a direct dependency** | Writing back to `ropa_records` creates ROPA → purpose → ROPA. |
| Classifier → findings | **new table**, or `ropa_findings.finding_type` | Keeps the write direction one-way. |
| Trigger | **event via `agent_jobs`** | Already how every agent is triggered; no new mechanism. |

**Circularity risk:** real. ROPA already computes purpose. If a classifier writes purpose
back into `ropa_records`, ROPA's next run overwrites it, and the two disagree silently.
The classifier must **read** declared purpose and **write** assessments elsewhere.

---

## 12. Gap analysis

**Already exists**
- Purpose vocabulary (`purpose_taxonomy`)
- Purpose inference from table names (10 purposes, confidence-scored)
- Purpose → processing activity grouping
- Observed web-side purpose with consent state (`trackers`, `cookies`)
- Vendor/processor mapping
- Data-flow mapping (source → storage → vendor)
- Declared-vs-observed pattern for *schema*
- Job queue, review, actions, audit, stage observability, RLS, connectors

**Can be reused**
- `purpose_rules.match_table()` — as-is
- `purpose_service.map_purposes_and_subjects()` — as-is
- `processing_activity_service.build_activities()` — as-is
- `connectors/factory.build_connector()` — as-is
- `queue.enqueue` / worker dispatch — one new job type + migration
- `review_service`, `action_service`, `audit_service`, `stage_tracker` — as-is
- `RopaDashboard.tsx` — as a UI pattern

**Must be built**
1. **Usage evidence ingestion** — nothing server-side exists. Largest item by far.
2. **Declared-vs-observed comparison service** — the actual new intelligence.
3. **Retention evaluation against purpose** — `dsr_retention_rules` and `cookies.expiry`
   exist but are never compared to a purpose.
4. **Cross-agent purpose reconciliation** — ROPA's 10 business purposes and Agent 1's
   4 taxonomy codes are different vocabularies at different granularities and nothing
   maps between them.
5. New tables + migration (RLS, grants, CHECK values).
6. Router, run service, worker branch, frontend console.

---

## 13. Final architecture

```
                    Frontend  PurposeConsole.tsx                         [NEW]
                        │
                    FastAPI  app/main.py                                 [EXISTING]
                        │
                    api/v1/router.py  (bind_request_scope → RLS)         [EXISTING]
                        │
                    routes/purpose.py                                    [NEW]
                        │
                    services/purpose_run_service.py                      [NEW]
                        │
                    jobs/queue.py → agent_jobs (new job_type)            [EXISTING + migration]
                        │
                    jobs/worker.py  (new dispatch branch)                [EXISTING + 1 branch]
                        │
        ┌───────────────┼────────────────────────┐
        ▼               ▼                        ▼
  DECLARED          OBSERVED                 CONTEXT
  ropa_records      trackers.category        purpose_taxonomy   [ALL EXISTING]
  .payload          cookies.category         ropa_data_sources
  [EXISTING]        consent_signals          dsr_retention_rules
                    .consent_states
                    [EXISTING]
                    usage_evidence           [NEW — does not exist]
        │               │                        │
        └───────────────┴────────────────────────┘
                        ▼
        agents/ropa/rules/purpose_rules.py       [EXISTING — reuse, do not fork]
        agents/ropa/services/purpose_service.py  [EXISTING — reuse]
                        ▼
        agents/purpose/services/comparison_service.py   [NEW — the real work]
                        ▼
        agents/ropa/services/processing_activity_service.py  [EXISTING — reuse]
                        ▼
        retention evaluation                     [NEW]
                        ▼
        purpose_assessments / purpose_findings   [NEW tables]
                        ▼
        services/review_service.py → approvals   [EXISTING]
                        ▼
        services/action_service.py → actions     [EXISTING]
                        ▼
        services/audit_service.py → audit_logs   [EXISTING]
```

---

## 14. PREFERRED PURPOSE CLASSIFIER INTEGRATION

**Do not build a fifth-agent-shaped classifier. Build a reconciliation service.**

The reasoning is concrete, not stylistic:

1. **Purpose classification already exists twice** — `purpose_rules.py` (10 business
   purposes, table-name driven) and `trackers/cookies.category` (4 taxonomy codes,
   vendor-signature driven). A third classifier makes three disagreeing answers.

2. **The missing capability is not classification — it is comparison.** Nine of the
   thirteen steps in §9 already exist. What does not exist is anything that puts
   *declared* purpose next to *observed* purpose and reports the gap.

3. **The pattern is already in the codebase.** `ropa_schema_baselines` vs
   `ropa_schema_changes` does exactly this for schema. Purpose is the same shape.

4. **The one finding nobody can currently produce** is available today with no new
   ingestion: a tracker categorised `marketing` whose `consent_states` include
   `pre_consent` is *observed processing without a lawful basis*. Both halves are in the
   database. Nothing joins them.

### Recommended shape

```
app/agents/purpose/
├── rules/reconciliation.py     vocabulary mapping: ROPA's 10 ↔ taxonomy's 4
├── services/
│   ├── declared_service.py     reads ropa_records.payload      (no new logic)
│   ├── observed_service.py     reads trackers/cookies/signals   (no new logic)
│   ├── comparison_service.py   declared vs observed             ← THE NEW WORK
│   └── retention_service.py    purpose vs cookies.expiry / dsr_retention_rules
└── schemas/
```

- **Router:** `routes/purpose.py`, prefix `/api/v1/purpose`
- **Trigger:** `agent_jobs` job type `purpose_assessment` (needs a CHECK migration)
- **New tables:** `purpose_assessments`, `purpose_findings` — with RLS, grants and
  enumerated CHECKs added in the same migration
- **Reuse unchanged:** `purpose_rules`, `purpose_service`, `processing_activity_service`,
  `review_service`, `action_service`, `audit_service`, `stage_tracker`, `queue`
- **Direction:** read-only against ROPA and Consent; writes only to its own tables

### Phasing I would argue for

**Phase 1 — no new ingestion.** Reconcile what two agents already hold. Ships the
pre-consent-marketing finding described above, and proves the vocabulary mapping.

**Phase 2 — retention.** `cookies.expiry` and `dsr_retention_rules` against purpose.

**Phase 3 — server-side usage evidence.** The genuinely large piece: a new connector
family, ingestion, storage, and a defensible way to infer purpose from access patterns.
This is where most of the effort is, and it should not gate phases 1 and 2.

### The risk worth stating

Inferring purpose from usage means being wrong sometimes, and this codebase has scar
tissue from exactly that failure mode: a keyword match against a user's forum comment
produced a high-priority finding about a consent banner that did not exist
(`tests/test_consent_banner_false_positive.py`).

`purpose_rules.py` already takes the right stance — it returns `None` rather than
guessing, and the caller emits `"Unknown"` with `review_required=True`. **Any new
comparison must inherit that discipline.** A declared-vs-observed mismatch reported
confidently, when the real cause is a vocabulary mapping artefact, is worse than no
finding at all.
