#!/usr/bin/env bash
# Deploy (or update) the Consiva stack. Migrations run automatically, before anything that serves
# traffic starts -- there is no separate migration command.
#
#   scripts/deploy.sh                       # production, docker-compose.prod.yml
#   COMPOSE_FILE=other.yml scripts/deploy.sh
#   WAIT_TIMEOUT=1200 scripts/deploy.sh     # seconds to wait for healthy (default 900)
#
# What it does, in order:
#   1. Pre-flight: Docker present, Compose >= 2.20, compose file valid, required settings present.
#   2. `docker compose up -d --build --remove-orphans --wait`, which builds images, starts the
#      database, runs the one-shot `migrate` service, and only then starts backend, worker and
#      frontend -- waiting until every long-running service reports healthy.
#   3. Verifies the result instead of trusting the exit code alone: migrate exited 0, every service
#      is running, and a read-only `migrate.py --check` reports nothing pending.
#
# On failure it prints the migrate log and the service table, and exits NON-ZERO. It never
# deletes volumes, never resets a database, never edits .env.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.prod.yml}"
WAIT_TIMEOUT="${WAIT_TIMEOUT:-900}"
compose() { docker compose -f "$COMPOSE_FILE" "$@"; }

say()  { printf '\n==> %s\n' "$*"; }
fail() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

# ── 1. Pre-flight ───────────────────────────────────────────────────────────────
say "Pre-flight checks"
command -v docker >/dev/null 2>&1 || fail "docker is not installed or not on PATH."
docker info >/dev/null 2>&1 || fail "cannot talk to the Docker daemon (is it running, and are you in the docker group?)."

COMPOSE_VERSION="$(docker compose version --short 2>/dev/null || true)"
[ -n "$COMPOSE_VERSION" ] || fail "'docker compose' (the v2 plugin) is not available."
# service_completed_successfully needs Compose 2.20+. Refuse older ones rather than start the
# application without the migration gate.
if [ "$(printf '%s\n2.20.0\n' "${COMPOSE_VERSION#v}" | sort -V | head -n1)" != "2.20.0" ]; then
  fail "Docker Compose ${COMPOSE_VERSION} is too old; need >= 2.20 for migration gating."
fi
echo "docker compose ${COMPOSE_VERSION}"

[ -f "$COMPOSE_FILE" ] || fail "compose file not found: $COMPOSE_FILE"
[ -f backend/.env ] || fail "backend/.env is missing (the application's secrets; see backend/.env.example). It is never created or overwritten by this script."
# Validates the file AND the required settings (POSTGRES_PASSWORD, APP_ENV, CONSIVA_JWT_SECRET) --
# compose refuses with the name of the missing one. Output is suppressed on success so no secret
# is ever printed.
if ! compose config -q 2>/tmp/consiva-compose-check.$$; then
  cat /tmp/consiva-compose-check.$$ >&2; rm -f /tmp/consiva-compose-check.$$
  fail "compose configuration is invalid or a required setting is missing (see above). Set it in the .env next to $COMPOSE_FILE."
fi
rm -f /tmp/consiva-compose-check.$$
echo "compose file valid; required settings present"

# ── 2. Build, migrate, start, wait ──────────────────────────────────────────────
say "Building, migrating and starting (this applies any pending migrations automatically)"
if ! compose up -d --build --remove-orphans --wait --wait-timeout "$WAIT_TIMEOUT"; then
  say "DEPLOYMENT FAILED -- service status"
  compose ps -a || true
  say "migrate log (a failed or refused migration explains itself here)"
  compose logs --no-color --tail 80 migrate || true
  cat >&2 <<'EOF'

The stack was NOT brought up on a bad schema: backend, worker and frontend do not start while
`migrate` fails. Nothing was dropped or reset. Read the migrate log above, fix the cause, and run
this script again -- it resumes from the migration that failed.
See docs/DEPLOYMENT.md ("When a migration fails").
EOF
  exit 1
fi

# ── 3. Verify, don't assume ─────────────────────────────────────────────────────
say "Verifying"
MIGRATE_ID="$(compose ps -aq migrate | head -n1)"
[ -n "$MIGRATE_ID" ] || fail "migrate container not found after deploy."
MIGRATE_EXIT="$(docker inspect -f '{{.State.ExitCode}}' "$MIGRATE_ID")"
[ "$MIGRATE_EXIT" = "0" ] || fail "migrate exited with code $MIGRATE_EXIT."
echo "migrate: exit 0"
compose logs --no-color --tail 12 migrate | sed 's/^/    /'

# A read-only second opinion from the same image: nothing may be pending now.
if compose run --rm --no-deps migrate python migrate.py --check >/tmp/consiva-migrate-check.$$ 2>&1; then
  echo "schema check: no pending migrations"
else
  cat /tmp/consiva-migrate-check.$$ >&2; rm -f /tmp/consiva-migrate-check.$$
  fail "migrations are still pending after the deploy -- investigate before using the application."
fi
rm -f /tmp/consiva-migrate-check.$$

compose ps
say "Deployed. All services healthy; schema is current."
