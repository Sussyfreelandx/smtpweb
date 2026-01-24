#!/usr/bin/env bash
# release_migrate.sh - Smart DB migrations for Render
# Usage (Render): set this script as the "Release Command"

set -e

FLASK_APP=${FLASK_APP:-wsgi:app}
export FLASK_APP

echo "🔄 Release Command Started..."

# 1. Check if migrations directory exists
if [ ! -d "migrations" ]; then
    echo "⚠️  Migrations folder not found. Initializing..."
    flask db init
    
    # 2. CRITICAL FIX: Check if the database is already populated.
    # We check if the 'user' table exists. If it does, we assume the DB is live.
    # We then 'stamp' the DB to say "Current Code matches DB State" to prevent
    # Alembic from trying to DROP all your existing tables.
    if python -c "from app import create_app, db; from sqlalchemy import inspect; app=create_app(); ctx=app.app_context(); ctx.push(); inspector=inspect(db.engine); exit(0 if inspector.has_table('user') else 1)"; then
        echo "✅ Existing database detected. Stamping 'head' to skip initial creation..."
        flask db stamp head
    else
        echo "✨ Fresh database detected. Generating initial migration..."
        flask db migrate -m "Initial migration"
    fi
else
    echo "📂 Migrations folder exists."
fi

# 3. Generate a new migration if there are model changes
echo "🔎 Checking for schema changes..."
flask db migrate -m "Auto migration $(date +%s)" || true

# 4. Apply Upgrades
echo "📈 Applying database upgrades..."
flask db upgrade

# 5. (Optional) Seed standard data if needed
# python manage.py seed_data

echo "✅ Release Command Finished."
