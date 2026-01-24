#!/usr/bin/env bash
# exit on error
set -o errexit

echo "🚀 Starting Build Process..."

# 1. Install dependencies
echo "📦 Installing requirements..."
pip install -r requirements.txt

# 2. Database Migration Handling
export FLASK_APP=wsgi:app

if [ ! -d "migrations" ]; then
  echo "⚠️ Migrations folder not found. Initializing..."
  flask db init
  # Attempt to create an initial migration, but don't fail if it's empty
  flask db migrate -m "Initial migration" || true
else
  echo "✅ Migrations folder exists."
  # Try to generate a migration for any new model changes
  echo "🔄 Checking for schema changes..."
  flask db migrate -m "Auto schema update" || true
fi

# 3. Upgrade the database
echo "⬆️ Upgrading database..."
flask db upgrade

echo "🎉 Build Complete!"
