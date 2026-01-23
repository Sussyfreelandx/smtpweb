#!/usr/bin/env bash
# release_migrate.sh - run DB migrations during a deployment/release phase
# Usage (Render): set this script as the "Release Command" for your service
# This script will attempt to run 'flask db upgrade' and will retry on transient failures.

set -euo pipefail

FLASK_APP=${FLASK_APP:-wsgi:app}
RETRIES=${RETRIES:-8}
SLEEP_SECONDS=${SLEEP_SECONDS:-5}

export FLASK_APP

echo "Release migration start: $(date -u +"%Y-%m-%d %H:%M:%S UTC")"
echo "Using FLASK_APP=${FLASK_APP}"
echo "Retries: ${RETRIES}, initial sleep: ${SLEEP_SECONDS}s"

# Ensure alembic migrations folder exists; if not, initialize and create initial migration.
if [ ! -d "migrations" ]; then
  echo "Migrations folder not found — initializing migrations directory"
  flask db init || true
  echo "Creating initial migration..."
  # try to autogenerate (may be empty)
  flask db migrate -m "Initial migration" || true
fi

attempt=1
sleep_time=${SLEEP_SECONDS}

while [ "$attempt" -le "$RETRIES" ]; do
  echo "Attempt ${attempt}/${RETRIES}: running 'flask db upgrade'..."
  if flask db upgrade; then
    echo "Database upgrade applied successfully."
    break
  else
    echo "Database upgrade failed on attempt ${attempt}."
    if [ "$attempt" -lt "$RETRIES" ]; then
    
      echo "Sleeping ${sleep_time}s before retry..."
      sleep "$sleep_time"
      # exponential backoff with cap
      sleep_time=$((sleep_time * 2))
      if [ "$sleep_time" -gt 60 ]; then
        sleep_time=60
      fi
    else
      echo "Reached max retries (${RETRIES}). Exiting with error."
      exit 1
    fi
  fi
  attempt=$((attempt + 1))
done

echo "Release migration finished: $(date -u +"%Y-%m-%d %H:%M:%S UTC")"
