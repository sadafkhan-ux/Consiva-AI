# Deploying Consiva with Docker (migrations included)

One command builds the images, applies any pending database migrations, starts everything, and
waits until it is healthy. **There is no separate migration step.**

```bash
cd /opt/consiva
git pull
bash scripts/deploy.sh
```

`scripts/deploy.sh` is a thin wrapper with pre-flight checks and failure reporting around exactly:

```bash
docker compose -f docker-compose.prod.yml up -d --build --remove-orphans --wait
```

Either form is safe to run on every deployment, on a fresh server or a long-lived one.

## What happens, in order

```
db ──healthy──▶ migrate (one-shot) ──exit 0──▶ backend ──healthy──▶ worker
                     │                                          └──▶ frontend
                     └─ exit ≠ 0 ──▶ backend / worker / frontend are NOT started; `up` fails
```

| Step | Service | What it does |
|---|---|---|
| 1 | `db` | Postgres + pgvector. Others wait for its health check (`pg_isready`). |
| 2 | `migrate` | Runs `python migrate.py` from the **same image** as the API, then exits. Retries the connection for up to 120 s if Postgres is up but not yet accepting connections. |
| 3 | `backend` | Starts only after `migrate` exited **0**. |
| 4 | `worker` | Starts only after `migrate` exited 0 **and** the backend is healthy. It also runs every periodic sweep — there is no separate scheduler. |
| 5 | `frontend` | Starts after the backend is healthy, so it can never be reached before the schema is current. |

`migrate` is a separate service, not a step in the API's or worker's command, so that exactly one
process ever applies migrations: two containers racing on startup (or every restart re-running
them) is what this design rules out.

Requires **Docker Compose ≥ 2.20** (`depends_on: condition: service_completed_successfully`).
`scripts/deploy.sh` checks this and stops with a clear message on an older one.

## How migrations are made safe

`backend/migrate.py` is the existing runner (it still tracks history in `schema_migrations`); it was
extended, not replaced.

- **One runner at a time.** A Postgres advisory lock is taken first and the applied set is read
  *after* it is held, so two simultaneous deployments serialise instead of both applying the same
  file. The lock is session-level: a crashed runner releases it automatically.
- **Each file in its own transaction, recorded in that same transaction.** A failed migration is
  rolled back whole and is **not** recorded; earlier ones stay applied; the next run resumes at the
  one that failed.
- **Checked before anything runs.** The runner refuses (exit 3) if it finds duplicate/malformed
  migration numbers; a pending migration numbered *below* one already applied; an existing
  application schema with an **empty** history table; or a statement Postgres cannot run inside a
  transaction (e.g. `CREATE INDEX CONCURRENTLY`).
- **DDL lock timeout** (60 s per migration): a migration queued behind a long-running query fails
  clearly instead of hanging the deployment.
- **Never destructive.** The runner never drops, truncates or recreates anything itself. A redeploy
  or `docker compose down` (without `-v`) never touches the database volume.

### Exit codes (`migrate` container, and `python migrate.py`)

| Code | Meaning | What to do |
|---|---|---|
| 0 | Applied, or nothing to do | — |
| 1 | A migration **failed**, was rolled back, and is not recorded | Read the `migrate` log, fix the migration (or the data it tripped on), deploy again. |
| 2 | `--check` only: migrations are pending | Informational. |
| 3 | **Refused** to run an unsafe state | The message says why and what to do (see below). |
| 4 | Database unreachable / connection rejected | Check the database is running and `POSTGRES_PASSWORD` / database name. A wrong password or database name fails immediately; only "not up yet" is retried. |
| 5 | Timed out waiting for another migration run | Another deployment is mid-migration; wait and retry. If none is running, look for a stale session holding advisory lock `7204190558313421` in `pg_locks`. |

## When a migration fails

`docker compose up` exits non-zero (`service "migrate" didn't complete successfully: exit 1`);
`scripts/deploy.sh` additionally prints the service table and the `migrate` log. The new
`backend`, `worker` and `frontend` containers are created but **never started**. Containers that were
already running from the previous deployment keep running on the previous schema (migrations are
additive).

```bash
docker compose -f docker-compose.prod.yml logs migrate     # the error, with the migration's name
```

Fix the cause and run `bash scripts/deploy.sh` again. Nothing needs cleaning up: the failed file
rolled back and was not recorded.

## Existing databases

- **Already managed by `migrate.py`** (the normal case): only pending migrations run; `Nothing to do`
  when current.
- **Built some other way** (hand-run SQL, a dump) so `schema_migrations` is empty but tables exist:
  `migrate` **refuses** (exit 3) rather than re-run every migration against live tables. After you
  have verified the schema really matches, record it explicitly, then deploy normally:
  ```bash
  docker compose -f docker-compose.prod.yml run --rm migrate \
      python migrate.py --baseline-through 0033_ropa_evidence_analysis_job_type
  ```
  This inserts history rows **without running** those migrations. Nothing is ever baselined
  automatically.
- **Out-of-order migration** (a pending file numbered below one already applied, e.g. a branch
  merged later): refused. Review it; if it is safe to apply late, run once with
  `python migrate.py --allow-out-of-order`, otherwise renumber it.
- **Database ahead of the image** (rolling back to an older image): not an error. `migrate` notes
  `NNNN is recorded as applied but is not in this checkout` and continues.

## Verify locally (no server, no real secrets)

Use a throwaway project name so nothing clashes with a real stack, and dummy secrets:

```bash
cat > /tmp/consiva-test.env <<'EOF'
POSTGRES_PASSWORD=local-test-only
APP_ENV=production
CONSIVA_JWT_SECRET=local-test-only-jwt-secret-0123456789abcdef
FRONTEND_PORT=127.0.0.1:18080
EOF
# backend/.env must exist (compose loads it into backend/worker); a stub with dummy values is enough
docker compose -p consivatest --env-file /tmp/consiva-test.env -f docker-compose.prod.yml \
    up -d --build --wait

# what migrate did
docker compose -p consivatest -f docker-compose.prod.yml logs migrate

# read-only: anything pending?  (exit 0 = no, 2 = yes)
docker compose -p consivatest --env-file /tmp/consiva-test.env -f docker-compose.prod.yml \
    run --rm --no-deps migrate python migrate.py --check

# clean up the throwaway stack AND its volume (only safe for a TEST project name)
docker compose -p consivatest --env-file /tmp/consiva-test.env -f docker-compose.prod.yml down -v
```

Runner tests (pure ones always run; the database ones create and drop their own temporary
databases on the server you name, and are skipped without it):

```bash
cd backend
python -m pytest tests/test_migrate_runner.py -q
TEST_DATABASE_URL=postgresql+asyncpg://USER:PASS@HOST:5432/postgres \
    python -m pytest tests/test_migrate_runner.py -q          # use a throwaway server/database
```

## Settings

Compose reads `/opt/consiva/.env` (see the root `.env.example`); the application reads
`backend/.env` (see `backend/.env.example`). They are different files. `migrate` receives **only**
`DATABASE_URL` and the `MIGRATE_*` settings — not `backend/.env` — and the same `DATABASE_URL`
(one YAML anchor) is used by `migrate`, `backend` and `worker`, so a migration can never run against
a different database than the one the application reads.

## Manual prerequisites that remain

- `backend/.env` and `/opt/consiva/.env` must exist with real values. They are never created,
  printed or overwritten by any of this.
- The first administrator, once per environment:
  `docker compose -f docker-compose.prod.yml run --rm backend python create_user.py --email you@example.com --org "Your Org" --admin`
- Regulatory sources, once per organisation: `python seed_regwatch_sources.py --org-id <uuid>`.
- Row-level-security cutover to the `consiva_app` role is a separate, existing, manual step (see the
  comments in `docker-compose.prod.yml`); the owner role used by `migrate` must remain the owner.
- Take a database backup before deploying migrations you have not reviewed.

## Not covered

- Rolling back a *schema* change. Migrations are forward-only and additive by convention; to undo
  one, write a new migration.
- Zero-downtime migrations: containers from the previous deployment keep serving while `migrate`
  runs, but a migration that takes a long exclusive lock will fail on the lock timeout rather than
  queue indefinitely. Review such migrations before production.
