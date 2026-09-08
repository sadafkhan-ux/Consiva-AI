# Consiva-AI

DPDP (Digital Personal Data Protection Act, 2023) compliance platform — **Consent Agent**.

Scans a website for consent/tracking evidence, reasons over it against a RAG knowledge
base built from the actual DPDP Act/Rules, and produces grounded, citation-validated
findings that a human reviews and approves before they count.

```
Website
  -> scan (Playwright, three consent states: pre / accept / reject)
  -> structured evidence
  -> deterministic rules + lookup
  -> RAG retrieval (pgvector over approved DPDP documents)
  -> LLM reasoning (self-hosted primary, NVIDIA fallback)
  -> structured-output validation + citation grounding
  -> findings
  -> human review gate
  -> append-only audit
```

## Stack

- **Backend** — Python 3.13, FastAPI, LangGraph (durable pause/resume at the human-review gate)
- **Database** — Supabase Postgres + `pgvector`, row-level security, append-only audit log
- **Scanner** — Playwright, with SSRF protection on every navigation and redirect hop
- **LLM** — provider-agnostic client: self-hosted llama.cpp (primary), NVIDIA NIM and Groq
  (fallback), all through one OpenAI-compatible abstraction. Embeddings are NVIDIA-only.

## Getting started

```bash
cd backend
python -m venv .venv && .venv/Scripts/activate    # Windows
pip install -r requirements.txt
playwright install chromium
cp .env.example .env                               # then fill in real values
python migrate.py
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

## Configuration

Secrets live in `backend/.env`, which is gitignored. `backend/.env.example` documents
every variable with empty placeholders. Never commit real keys.

## Tests

```bash
cd backend
.venv/Scripts/python -m pytest -q
.venv/Scripts/ruff check app/ tests/
```

## Scope

This repository currently implements the Consent Agent only. The ML classifier,
continuous monitoring, and the remaining agents are deliberately out of scope for now.
