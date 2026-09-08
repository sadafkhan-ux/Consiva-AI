# Consiva — Consent Agent

The first of Consiva's DPDP compliance agents. Given a website URL, it scans the site
(pages, forms, cookies, trackers, third-party services, policies, and consent
mechanism — across three consent states: before interaction, after Accept, after
Reject), classifies what it found via a deterministic lookup + rules engine, retrieves
grounded DPDP Act/Rules context via RAG, drafts findings and recommendations with an
NVIDIA-hosted LLM, and routes everything through human review before anything is
considered final. Every step is audited.

Scope note: **only the Consent Agent** is built here. The other four planned agents
(Data Discovery/ROPA, DSR Fulfillment, Breach Response, Regulatory Watch), the ML
classifier layer, continuous monitoring, and the post-approval Action Module are
explicitly deferred — see [Future work](#future-work).

## Architecture at a glance

```
Website URL
  → SSRF guard + rate limit (services/scan_service.py)
  → Website Scanner (Playwright) — 3 passes: pre_consent / post_accept / post_reject
  → Structured scan evidence → Supabase Postgres
  → Consent Agent (LangGraph):
      normalize → classify (lookup + rules) → retrieve DPDP context (pgvector RAG)
      → NVIDIA LLM (reason/explain/draft) → validate (schema + citation grounding)
      → create findings
  → Human Review (pending/approved/rejected/edited) — the graph pauses here
  → Audit log (append-only)
```

Backend: Python 3.11+ / FastAPI. Orchestration: LangGraph, with a Postgres-backed
checkpointer so a paused (awaiting-review) run survives a process restart. DB:
Supabase Postgres + pgvector, in the same instance as the relational tables.

## Setup

### Prerequisites

- Python 3.11+
- A Supabase project (or any Postgres 15+ with the `vector` and `pgcrypto` extensions
  available) — `pgvector` and RLS are used directly, so a plain Postgres works too as
  long as you skip the RLS policies or adapt them.
- An NVIDIA API key from [build.nvidia.com](https://build.nvidia.com) (starts `nvapi-`).
- Playwright's Chromium browser (installed separately, see below).

### Install

```bash
cd backend
python -m venv .venv
./.venv/Scripts/activate   # or: source .venv/bin/activate on macOS/Linux
pip install -e ".[dev]"
playwright install chromium
```

### Environment variables

Copy `.env.example` to `.env` and fill in real values. **Never commit `.env`** — it's
gitignored; `.env.example` is the template and must stay free of real secrets.

| Variable | Required | Notes |
|---|---|---|
| `NVIDIA_API_KEY` | yes | From build.nvidia.com. |
| `NVIDIA_API_BASE_URL` | no (default set) | `https://integrate.api.nvidia.com/v1` — OpenAI-compatible. |
| `NVIDIA_LLM_MODEL` | yes | A model id from NVIDIA's current catalog — verify against build.nvidia.com before deploying; the catalog changes. |
| `NVIDIA_EMBED_MODEL` | yes | Must stay identical between ingest-time and query-time embeddings. |
| `NVIDIA_EMBED_DIMENSIONS` | no (default `1024`) | Must match the embed model's output dimension **and** the migration's `vector(N)` column — changing the model after the first ingest requires a new migration. |
| `SUPABASE_URL` | yes | Your project's URL. |
| `SUPABASE_SERVICE_ROLE_KEY` | yes | The **service_role** secret (Project Settings → API in Supabase) — not the publishable/anon key. The backend needs this to bypass RLS as a trusted server. |
| `DATABASE_URL` | yes | Direct Postgres connection string (asyncpg driver), used for SQLAlchemy, migrations, and the LangGraph checkpointer. |
| `SUPABASE_JWT_SECRET` | yes | Used by the placeholder auth in `app/core/security.py` — see [Troubleshooting](#troubleshooting). |
| `SCANNER_HEADLESS` | no (default `true`) | |
| `SCANNER_MAX_PAGES` | no (default `25`) | Cap on pages crawled per scan (pre_consent pass only). |
| `SCANNER_TIMEOUT_SECONDS` | no (default `30`) | Per-page navigation timeout. |
| `SCANNER_USER_AGENT` | no | Identify the crawler to site owners. |
| `SCANNER_MAX_SCANS_PER_ORG_PER_DAY` | no (default `50`) | Basic rate limit. |
| `APP_ENV` / `LOG_LEVEL` | no | |

## Database setup

Migrations are plain SQL, additive only, run in order:

```bash
psql "$DATABASE_URL" -f migrations/0001_init.sql
psql "$DATABASE_URL" -f migrations/0002_lookup_provenance_consent_state.sql
```

(Or paste them into the Supabase SQL editor.) `0001` creates the core schema; `0002`
adds the DB-backed cookie/tracker lookup table, purpose taxonomy, classification
provenance, consent-state tracking on cookies/trackers, and the `priority` field on
findings.

### Seed the cookie/tracker lookup table

The Open Cookie Database (2,266 rows, Apache 2.0, commercial use permitted) is the
deterministic lookup layer's data source. Extract `open-cookie-database.csv` from the
project's dataset archive and load it once:

```bash
python -m app.lookup.loader path/to/open-cookie-database.csv
```

### Ingest the DPDP knowledge base

```bash
python -m app.rag.ingest "../knowledge_sources/DPDP Rules.pdf" \
    --title "DPDP Rules" --source-type dpdp_rules --approve
```

Repeat per source document. `--approve` matters: unapproved documents are ingested but
never retrieved (`rag/retriever.py` filters on `is_approved`), so nothing reaches the
LLM until someone has actually reviewed it as an authoritative source. Note: the
project's `knowledge_sources/` currently has the DPDP Rules, corrigenda, board/
enforcement updates, and the IT Act 2000 — **not** the DPDP Act 2023 text itself; add
that before relying on citations for Act-level (as opposed to Rules-level) provisions.

## Running it

```bash
uvicorn app.main:app --reload          # API server
python -m app.jobs.worker              # background worker (scans + agent runs) — run separately
```

The worker polls a DB-backed job queue (`agent_jobs`); run more than one process for
throughput, it's safe (`SELECT ... FOR UPDATE SKIP LOCKED`).

## API examples

All routes require a bearer JWT with an `org_id` claim (see
[Troubleshooting](#troubleshooting) — the current auth is a placeholder).

```bash
# Start a scan (requires explicit authorization attestation — see url_safety.py / §Security)
curl -X POST http://localhost:8000/api/v1/consent/scans \
    -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
    -d '{"url": "https://example.com", "authorized": true}'
# -> {"id": "...", "url": "...", "status": "pending"}

# Check scan status
curl http://localhost:8000/api/v1/consent/scans/$SCAN_ID -H "Authorization: Bearer $TOKEN"

# Trigger analysis once the scan is completed
curl -X POST http://localhost:8000/api/v1/consent/scans/$SCAN_ID/analyze -H "Authorization: Bearer $TOKEN"

# List findings (paused agent runs show up as findings with status="pending")
curl http://localhost:8000/api/v1/consent/scans/$SCAN_ID/findings -H "Authorization: Bearer $TOKEN"

# Approve / reject / edit a finding
curl -X POST http://localhost:8000/api/v1/consent/findings/$FINDING_ID/approve \
    -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d '{"reason": "confirmed"}'

curl -X POST http://localhost:8000/api/v1/consent/findings/$FINDING_ID/reject \
    -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d '{"reason": "false positive"}'

curl -X POST http://localhost:8000/api/v1/consent/findings/$FINDING_ID/edit \
    -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
    -d '{"edited_payload": {"risk_level": "low"}, "reason": "overstated"}'

# Audit trail
curl http://localhost:8000/api/v1/consent/scans/$SCAN_ID/audit -H "Authorization: Bearer $TOKEN"
```

Full request/response shapes: run the server and check `/docs` (FastAPI's generated
OpenAPI UI).

## Testing

```bash
pytest -q       # 64 tests, all offline (no live DB, no live NVIDIA key)
ruff check app tests
```

What's covered without any live infrastructure: the rules engine (including the
consent-state violation rules), the lookup matcher, the chunker's section-heading
detection, LLM schema validation, the NVIDIA client (mocked — no live key needed, per
project convention), the agent graph's routing/validation logic, API request
validation, SSRF guard behavior, and the negative test proving no code path can change
a finding's status outside `review_service.py`'s approve/reject/edit functions.

**Honest limitation**: repository CRUD, RAG retrieval, full approval-workflow
integration (including the LangGraph pause/resume across the human-review gate), and
audit-log persistence need a real Postgres with `pgvector` — this project's sandbox
didn't have one available while these tests were written, so they weren't run
end-to-end here. Point `DATABASE_URL` at a real (ideally disposable/test) Postgres
instance with the migrations applied to exercise those paths.

## Troubleshooting

- **Auth is a placeholder.** `app/core/security.py` verifies an HS256 JWT with a
  shared secret and expects an `org_id` claim — this is explicitly *not* a verified
  integration with Consiva's real auth model (no frontend/auth existed at the time
  this was built). Confirm the real claims shape before relying on it, and note
  Supabase now recommends asymmetric (ES256/RS256) JWKS-based verification over the
  legacy shared-secret approach used here.
- **`AsyncPostgresSaver` setup errors on first run.** `langgraph-checkpoint-postgres`'s
  exact setup call has moved between minor versions — if `graph.py`'s
  `checkpointer.setup()` fails, check the installed version's docs against what's
  pinned in `pyproject.toml`.
- **`psycopg` import errors.** Needs the `[binary]` extra (already in
  `pyproject.toml`) — bare `psycopg` requires a system `libpq` install that most dev
  machines won't have.
- **Playwright browser not found.** Run `playwright install chromium` — it's a
  separate download from the pip package.
- **NVIDIA model 404s or behaves unexpectedly.** NVIDIA's hosted catalog changes;
  re-verify `NVIDIA_LLM_MODEL`/`NVIDIA_EMBED_MODEL` against build.nvidia.com. The free
  tier also has modest rate limits (documented as ~40 req/min at the time this was
  built) — budget for a paid/dedicated deployment before production traffic.
- **Consent-banner click doesn't work on some sites.** `consent_interactor.py` covers
  OneTrust and Cookiebot by CSS selector plus a generic text-match fallback for
  everything else — real-world CMPs vary widely; check
  `consent_signals.evidence.accept_interaction`/`reject_interaction` on the scan
  (`"not_found"` means no control could be located, recorded explicitly rather than
  silently skipped).

## Change summary

**New** (everything — this is a from-scratch build, see the session's architecture
plan for the original scaffold, and this README's own history for the hardening pass
that added SSRF protection, three-pass consent-state scanning, the DB-backed lookup
table, classification provenance, structured DPDP citations, and this test/doc set).

**Reused**: N/A — no pre-existing Consiva codebase existed in this project directory
when work began.

**Modified** (during the hardening pass, on top of the initial scaffold): `crawler.py`
(single-pass → three-pass + SSRF route guard + robots.txt), `consent_rules.py`
(hardcoded catalog → DB-lookup-first classification, `R-00N` rule ids, two new
consent-state violation rules), `llm/schemas.py` (added `priority`, added
`DpdpReference`), `rag/chunker.py` (added section-heading detection), `rag/retriever.py`
(added `document_version`/`section`), `create_findings.py` (citation resolution),
`db/models.py` + migrations (see below).

**DB changes**: migration `0002` — new tables `cookie_lookup`, `purpose_taxonomy`; new
columns `cookies.source`, `cookies.consent_states`, `trackers.source`,
`trackers.consent_states`, `consent_findings.priority`.

**APIs added**: none new in the hardening pass — the 8 routes from the initial build
(`/api/v1/consent/scans[...]`, `/api/v1/consent/findings/{id}/...`) are unchanged;
`FindingResponse` gained `priority` and a richer `dpdp_reference` shape.

**Future work** (explicitly deferred, not forgotten):
- ML classifier (XGBoost on the CookieBlock dataset) — `app/ml/` stays an interface
  stub until enough human-reviewed labels exist.
- Continuous monitoring / diff engine (scheduled re-scans, change detection).
- Action Module (task creation, notifications, staged consent-config deployment).
- Self-hosted Qwen/Ollama — several of the project's planning documents specify this
  instead of NVIDIA; NVIDIA was kept for this build per explicit direction, revisit
  once India-region self-hosting infra actually exists.
- A review UI — only the backend API + the negative bypass test exist; no frontend.
- The other four agents (Data Discovery/ROPA, DSR, Breach Response, Regulatory Watch).
