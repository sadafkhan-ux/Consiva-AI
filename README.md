# Consiva-AI

DPDP (Digital Personal Data Protection Act, 2023) compliance platform. Two agents,
one console.

**Consent Agent** scans a website for consent/tracking evidence, reasons over it
against a RAG knowledge base built from the actual DPDP Act and Rules, and produces
grounded, citation-validated findings a human reviews before they count.

**Data Discovery / ROPA Agent** takes authorized metadata from an organization's own
systems and turns it into an evidence-backed Record of Processing Activities —
classifying personal data, mapping data subjects, purposes, processing activities and
data flows, detecting privacy gaps, and tracking schema changes over time.

Neither agent lets an AI inference become an authoritative record. Everything material
passes a human review gate, and anything the evidence doesn't support stays explicitly
`Unknown` rather than being guessed at.

## The two flows

```
AGENT 1 — Consent
Website
  -> scan (Playwright, three consent states: pre / accept / reject)
  -> structured evidence
  -> deterministic rules + lookup
  -> RAG retrieval (pgvector over approved DPDP documents)
  -> LLM reasoning (self-hosted primary, NVIDIA fallback)
  -> structured-output validation + citation grounding
  -> findings -> human review gate -> append-only audit

AGENT 2 — Data Discovery / ROPA
Authorized source (Postgres, REST API, or a customer-side adapter)
  -> metadata only: tables, columns, types, foreign keys. Never row values.
  -> deterministic classification (10 personal-data categories, with confidence)
  -> data subject + purpose mapping (never invented; Unknown -> review)
  -> processing activities -> data flows -> risk/gap findings
  -> versioned ROPA records -> human review -> audit
  -> schema change detection against a promoted baseline
```

## Stack

- **Backend** — Python 3.13, FastAPI. Agent 1 uses LangGraph for durable pause/resume
  at its review gate; Agent 2's pipeline is a linear, deterministic chain with no
  graph (see `app/agents/ropa/services/discovery_service.py`).
- **Database** — Postgres 16 + `pgvector`, row-level security, append-only audit log.
  Runs as a local container in production (`docker-compose.prod.yml`); it was on
  Supabase until the shared session pooler's 15-client cap became the bottleneck.
- **Auth** — first-party: `organizations` + `users` tables, bcrypt passwords,
  Consiva-issued JWTs (`app/core/tokens.py`). Legacy Supabase tokens are still
  accepted during the transition and can be switched off by deleting the
  `SUPABASE_*` settings.
- **Scanner** — Playwright, with SSRF protection on every navigation and redirect hop.
- **LLM** — provider-agnostic client: self-hosted llama.cpp (primary), NVIDIA NIM and
  Groq (fallback), all through one OpenAI-compatible abstraction. Embeddings are
  NVIDIA-only.

## Getting started

```bash
cd backend
python -m venv .venv && .venv/Scripts/activate    # Windows
pip install -r requirements.txt
playwright install chromium
cp .env.example .env                               # then fill in real values
python migrate.py
```

Create the first admin — there is no public signup, and the API can't create a user
without an authenticated admin already existing:

```bash
python create_user.py --email you@example.com --org "Your Org" --admin
```

Ingest the RAG knowledge base, or DPDP citations come back empty:

```bash
python -m app.rag.ingest "../knowledge_sources/DPDP Act 2023.pdf" \
  --title "DPDP Act 2023" --source-type dpdp_act --version 2023 --approve
```

Run the three processes:

```bash
python -m app.run_server        # API
python -m app.jobs.worker       # background scan/analyze worker (required)
cd ../frontend && npm install && npm run dev
```

The worker is not optional — scans submitted without one running stay queued.

See [backend/README.md](backend/README.md) for full setup, environment variables,
migrations, API examples, and troubleshooting.

## Agent 2 without a running server

The ROPA pipeline is pure and synchronous, so it can be exercised from the CLI against
any Postgres you're authorized to read:

```bash
python -m app.agents.ropa.run_single_discovery https://customer.example \
  --host db.internal --dbname prod --user consiva_readonly --password '...'
```

The connection is forced read-only at the server (`default_transaction_read_only`), and
a superuser or bypass-RLS credential is rejected before anything is read.

## Integrating an external source

For a customer who won't (and shouldn't) hand over database credentials,
`backend/ropa_integration/` is a self-contained adapter they run inside their own
infrastructure. It reads their schema locally and pushes curated metadata to
`POST /api/v1/ropa/evidence` over authenticated HTTPS.

- It issues no SQL — everything comes from SQLAlchemy's `Inspector`.
- It transmits no row values; `sample_pattern` is never populated.
- Nothing leaves their system unless it's in the allow-list in their copy of
  `adapter.py`.

`ropa_integration/prepmyevent/` is the first implementation, with a README, a data
contract, and a security statement written for the customer's own reviewers.

## Configuration

Secrets live in `backend/.env`, which is gitignored. `backend/.env.example` documents
every variable with empty placeholders. Never commit real keys.

In Docker, note that `/opt/consiva/.env` is read by *compose* for `${...}`
interpolation, while the container inherits `env_file: ./backend/.env` — two different
files. Anything the application must read is passed explicitly in the compose
`environment:` block for that reason.

## Tests

```bash
cd backend
.venv/Scripts/python -m pytest -q
.venv/Scripts/ruff check app/ tests/
```

Some tests need real infrastructure and skip cleanly without it: retrieval quality
needs an ingested knowledge base, and the ROPA integration tests read a live database
catalog.

## Scope

Both agents are implemented and deployed. Deliberately still out of scope: the ML
classifier (`app/ml/` is an unused stub — there is no labelled training data yet, and
the rules layer is the documented Phase 1), per-key rate limiting (needs Redis to work
across worker processes), and queued execution for ROPA connector runs — the worker
branch exists but nothing enqueues it, since the push path completes in-request.
