#!/usr/bin/env bash
# One-time (and repeatable) cleanup of scan-generated data for BOTH scanning agents, from Docker.
#
#   scripts/cleanup_scan_data.sh                 # dry run: verifies the database, reports what WOULD be deleted, changes nothing
#   scripts/cleanup_scan_data.sh --execute       # backs up the database, clears finished scans of both agents, then verifies
#   scripts/cleanup_scan_data.sh --execute --grace-minutes 0     # also clear scans that finished a moment ago
#   scripts/cleanup_scan_data.sh --execute --agent enterprise    # only one agent (all | five_page | enterprise)
#   scripts/cleanup_scan_data.sh --build ...     # rebuild the image first (needed once, after pulling the cleanup code)
#   scripts/cleanup_scan_data.sh --execute --vacuum   # afterwards give the freed disk space back (VACUUM FULL of the cleared tables: they are locked for a moment)
#
# What it does, in order:
#   1. checks the database container is up and prints which database it is (name, address, version);
#   2. (with --execute) takes a full backup, backups/pre_cleanup_<time>.dump, and checks that the backup can be read back;
#   3. runs `python -m app.cleanup` in a throw-away container of the `cleanup` service: it STOPS, changing nothing, if the database is not the one named in CLEANUP_CONFIRM_DATABASE;
#   4. writes the scan history to backups/scan_history_<time>.csv before deleting anything;
#   5. deletes the rows of scan-generated tables, never a table or a column; keeps each scan's id, website, status, times and failure reason;
#   6. verifies: structure identical, no other table lost a row, history identical, every constraint valid;
#   7. (with --execute) runs a second, dry pass: it must find nothing left to delete (the cleanup is safe to repeat).
#
# Restore, if ever needed:  docker compose -f docker-compose.prod.yml exec -T db pg_restore -U consiva -d consiva --clean --if-exists < backups/pre_cleanup_<time>.dump
set -euo pipefail
export MSYS_NO_PATHCONV=1                       # Git Bash on Windows must not rewrite /backups into a Windows path

cd "$(dirname "$0")/.."
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.prod.yml}"
DC=(docker compose -f "$COMPOSE_FILE")
EXECUTE=0; BUILD=0; VACUUM=0; ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --execute) EXECUTE=1 ;;
    --build) BUILD=1 ;;
    --vacuum) VACUUM=1 ;;
    *) ARGS+=("$1") ;;
  esac
  shift
done
STAMP="$(date +%Y%m%d_%H%M%S)"
mkdir -p backups

echo "== 1. the database =="
"${DC[@]}" ps --status running db | grep -q db || { echo "STOPPED: the db container is not running." >&2; exit 2; }
"${DC[@]}" exec -T db psql -U consiva -d consiva -At -F ' | ' -c "select 'database '||current_database()||' | address '||coalesce(inet_server_addr()::text,'local socket')||' | standby: '||pg_is_in_recovery()||' | '||(select count(*) from information_schema.tables where table_schema='public' and table_type='BASE TABLE')||' tables'"

if [ "$BUILD" = 1 ]; then
  echo "== building the image =="
  "${DC[@]}" build backend
fi

if [ "$EXECUTE" = 1 ]; then
  echo "== 2. backup =="
  DUMP="backups/pre_cleanup_${STAMP}.dump"
  "${DC[@]}" exec -T db pg_dump -U consiva -d consiva -Fc > "$DUMP"
  [ -s "$DUMP" ] || { echo "STOPPED: the backup is empty." >&2; rm -f "$DUMP"; exit 1; }
  "${DC[@]}" exec -T db pg_restore --list < "$DUMP" > /dev/null || { echo "STOPPED: the backup cannot be read back." >&2; exit 1; }
  echo "backup written and readable: $DUMP ($(du -h "$DUMP" | cut -f1))"
fi

echo "== 3. cleanup =="
RUN=("${DC[@]}" run --rm --no-deps cleanup python -m app.cleanup)
if [ "$EXECUTE" = 1 ]; then
  "${RUN[@]}" --execute --history-dir /backups "${ARGS[@]}"
  STATUS=$?
  echo "== 4. second pass (must find nothing left) =="
  "${RUN[@]}" "${ARGS[@]}"
else
  "${RUN[@]}" "${ARGS[@]}"
fi
if [ "$VACUUM" = 1 ] && [ "$EXECUTE" = 1 ]; then
  echo "== 5. returning the freed disk space (VACUUM FULL of the cleared tables) =="
  TABLES=$("${DC[@]}" run --rm --no-deps cleanup python -c "from app.cleanup import registry as r; print(' '.join(sorted(r.all_cleared_tables())))" | tail -1)
  for t in $TABLES; do
    "${DC[@]}" exec -T db psql -U consiva -d consiva -qAt -c "vacuum full \"$t\""
  done
  "${DC[@]}" exec -T db psql -U consiva -d consiva -At -c "select 'database size now: '||pg_size_pretty(pg_database_size('consiva'))"
fi
echo "done."
