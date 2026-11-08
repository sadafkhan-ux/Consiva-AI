# Consiva-AI

DPDP (Digital Personal Data Protection Act, 2023) compliance platform built around one
agent, the **Consent Agent**.

It scans a website for consent/tracking evidence, reads its privacy policy, reasons over
the evidence against a RAG knowledge base built from the actual DPDP Act and Rules, and
produces grounded, citation-validated findings a human reviews before they count.

No AI inference becomes an authoritative record. Everything material passes a human
review gate, and anything the evidence doesn't support stays explicitly `Unknown`
rather than being guessed at.

## The flow

```
Website
  -> scan (Playwright, three consent states: pre / accept / reject)
  -> structured evidence (+ privacy-policy content analysis)
  -> deterministic rules + lookup
  -> RAG retrieval (pgvector over approved DPDP documents)
  -> LLM reasoning (Claude primary when ANTHROPIC_API_KEY is set; else self-hosted; NVIDIA/Groq fallback)
  -> structured-output validation + citation grounding
  -> findings -> human review gate -> append-only audit
```

## Stack

- **Backend** — Python 3.13, FastAPI. The Consent Agent uses LangGraph for durable
  pause/resume at its review gate (`app/agents/consent_agent/graph.py`).
- **Database** — Postgres 16 + `pgvector`, row-level security, append-only audit log.
  Runs as a local container in production (`docker-compose.prod.yml`); it was on
  Supabase until the shared session pooler's 15-client cap became the bottleneck.
- **Auth** — first-party: `organizations` + `users` tables, bcrypt passwords,
  Consiva-issued JWTs (`app/core/tokens.py`). Legacy Supabase tokens are still
  accepted during the transition and can be switched off by deleting the
  `SUPABASE_*` settings.
- **Scanner** — Playwright, with SSRF protection on every navigation and redirect hop.
- **LLM** — provider-agnostic client: Claude (primary when `ANTHROPIC_API_KEY` is set), an optional
  self-hosted llama.cpp server, and NVIDIA NIM or Groq as the fallback. Embeddings are
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

### Deploying with Docker

On a server, deploy (and update) with one command; pending database migrations are applied
automatically and the application only starts once they have succeeded:

```bash
bash scripts/deploy.sh      # = docker compose -f docker-compose.prod.yml up -d --build --remove-orphans --wait
```

See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) for the flow, failure handling and local verification.

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

## Integrating a website or backend

External callers use the Consent Agent API (`/api/v1/consent-agent/*`). For now it is
**open**: with `APP_ENV=development` or `OPEN_ACCESS=true`, a request with no token acts as
the oldest user's account (the same one the console auto-logs in as). A request with a
login token acts as that user. With neither setting, a token is still required. A
replacement auth system is planned. See
[docs/CONSENT_AGENT_API_FOR_WEBSITE_TEAM.md](docs/CONSENT_AGENT_API_FOR_WEBSITE_TEAM.md).

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
needs an ingested knowledge base.

## Scope

The Consent Agent is implemented and deployed. Deliberately still out of scope: the ML
classifier (`app/ml/` is an unused stub — there is no labelled training data yet, and
the rules layer is the documented Phase 1), and per-key rate limiting (needs Redis to
work across worker processes).

The database migrations for the agents that were removed (ROPA, DSR, Breach, Regulatory
Watch, Purpose Classifier) are kept unchanged, so existing databases still match the
migration ledger; their tables are simply no longer used.
