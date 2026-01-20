#!/usr/bin/env bash
# exit on error
set -o errexit

# 1. Install dependencies
pip install -r requirements.txt

# 2. Check if migrations exist. If not, initialize them.
if [ ! -d "migrations" ]; then
  echo "Migrations folder not found. Initializing..."
  export FLASK_APP=wsgi:app
  flask db init
  flask db migrate -m "Initial migration"
fi

# 3. Upgrade the database
flask db upgrade
