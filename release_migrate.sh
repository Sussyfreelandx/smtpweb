#!/usr/bin/env bash
# release_migrate.sh - run DB migrations during a deployment/release phase
# Usage (Render): set this script as the "Release Command" for your service

set -e
echo "Release migration start: $(date -u +'%Y-%m-%d %H:%M:%S UTC')"

# Ensure FLASK_APP environment variable
export FLASK_APP=wsgi:app

# Only run upgrade. Migrations must be generated locally and committed to the repo.
echo "Upgrading database"
flask db upgrade

echo "Release migration finished: $(date -u +'%Y-%m-%d %H:%M:%S UTC')"
