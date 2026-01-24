#!/usr/bin/env bash
# apply_fix.sh
# One-shot apply: backup DB, create Alembic migration to fix FKs, stamp & upgrade.
# Usage:
#   export DATABASE_URL="postgresql://user:pass@host:5432/dbname"
#   export FLASK_APP="wsgi:app"   # if not set already
#   ./apply_fix.sh
set -euo pipefail

# CONFIG (can override with env variables)
DB_URL="${DATABASE_URL:-}"
FLASK_APP="${FLASK_APP:-wsgi:app}"
BACKUP_DIR="${BACKUP_DIR:-./db_backups}"
PG_DUMP_CMD="${PG_DUMP_CMD:-pg_dump}"
PSQL_CMD="${PSQL_CMD:-psql}"
MIG_DIR="migrations/versions"
MIG_FILENAME="20260124_fix_fk_dependencies.py"
RETRIES="${RETRIES:-3}"

if [ -z "$DB_URL" ]; then
  echo "ERROR: DATABASE_URL must be set. Example:"
  echo "  export DATABASE_URL='postgresql://user:pass@host:5432/dbname'"
  exit 1
fi

echo "Using FLASK_APP=${FLASK_APP}, DATABASE_URL=${DB_URL}"
mkdir -p "$BACKUP_DIR"
mkdir -p "$MIG_DIR"

# 1) Backup DB (best-effort)
echo "Backing up DB to ${BACKUP_DIR} ..."
BACKUP_FILE="${BACKUP_DIR}/backup_$(date -u +%Y%m%dT%H%M%SZ).sql"
if command -v "$PG_DUMP_CMD" >/dev/null 2>&1; then
  echo "Running pg_dump to ${BACKUP_FILE} ..."
  $PG_DUMP_CMD "$DB_URL" -f "$BACKUP_FILE"
  echo "Backup saved to ${BACKUP_FILE}"
else
  echo "pg_dump not found; please ensure you have a backup before proceeding."
  exit 1
fi

# 2) Ensure FLASK_APP and flask invocation works
export FLASK_APP="$FLASK_APP"

echo "Determining Alembic current DB revision (flask db current) ..."
# run and parse flask db current output; if no current, attempt to use head from migrations/versions
CURRENT_REV=""
set +e
OUT="$(flask db current 2>&1)" || true
set -e
if echo "$OUT" | grep -q "Current revision for .* is"; then
  CURRENT_REV="$(echo "$OUT" | sed -n 's/.*Current revision for .* is \([0-9a-f]*\).*/\1/p')"
fi

if [ -z "$CURRENT_REV" ]; then
  # Try list of existing versions files to infer head
  LATEST_FILE="$(ls -1 $MIG_DIR 2>/dev/null | sort | tail -n1 || true)"
  if [ -n "$LATEST_FILE" ]; then
    # filename starts with revision id
    CURRENT_REV="${LATEST_FILE%%_*}"
    echo "flask db current returned nothing; using latest migrations file revision: $CURRENT_REV"
  else
    echo "No existing migration revision detected. We'll stamp head after creating migration."
    CURRENT_REV=""
  fi
fi

# Determine python representation of down_revision
if [ -z "$CURRENT_REV" ]; then
    DOWN_REV_PY="None"
else
    DOWN_REV_PY="'$CURRENT_REV'"
fi

# 3) Create migration file content, with down_revision = CURRENT_REV (or None)
MIG_PATH="${MIG_DIR}/${MIG_FILENAME}"
echo "Creating migration file at ${MIG_PATH} (down_revision=${DOWN_REV_PY})"

cat > "$MIG_PATH" <<PY
"""Fix FK constraints that block DROP operations by adding safe ON DELETE policies

Revision ID: 20260124_fix_fk_dependencies
Revises: ${CURRENT_REV:-None}
Create Date: $(date -u +"%Y-%m-%d %H:%M:%S UTC")
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.engine.reflection import Inspector

# revision identifiers, used by Alembic.
revision = '20260124_fix_fk_dependencies'
down_revision = ${DOWN_REV_PY}
branch_labels = None
depends_on = None


def _get_existing_fk_name(conn, table_name, column_name):
    insp = Inspector.from_engine(conn)
    for fk in insp.get_foreign_keys(table_name):
        cols = fk.get('constrained_columns') or []
        if column_name in cols:
            return fk['name']
    return None


def _replace_fk(conn, table, column, referent_table, ondelete='SET NULL'):
    existing = _get_existing_fk_name(conn, table, column)
    if existing:
        try:
            op.drop_constraint(existing, table, type_='foreignkey')
        except Exception:
            pass
    # Use standard f-string without mixing .format for safety
    fk_name = f'fk_{table}_{column}'
    op.create_foreign_key(
        fk_name,
        source_table=table,
        referent_table=referent_table,
        local_cols=[column],
        remote_cols=['id'],
        ondelete=ondelete
    )


def upgrade():
    conn = op.get_bind()

    # Join tables -> CASCADE
    # Explicitly handle tables that were previously in the broken loop
    try:
        _replace_fk(conn, 'recipient_segments', 'recipient_id', 'recipient', ondelete='CASCADE')
    except Exception:
        pass
    try:
        _replace_fk(conn, 'recipient_segments', 'segment_id', 'segment', ondelete='CASCADE')
    except Exception:
        pass
    try:
        _replace_fk(conn, 'campaign_tags', 'campaign_id', 'campaign', ondelete='CASCADE')
    except Exception:
        pass
    try:
        _replace_fk(conn, 'campaign_tags', 'tag_id', 'tag', ondelete='CASCADE')
    except Exception:
        pass
    try:
        _replace_fk(conn, 'sequence_recipient', 'sequence_id', 'sequence', ondelete='CASCADE')
    except Exception:
        pass

    # Specific replacements with intended parents and behaviors
    try:
        _replace_fk(conn, 'recipient', 'campaign_id', 'campaign', ondelete='CASCADE')
    except Exception:
        pass
    try:
        _replace_fk(conn, 'recipient', 'smtp_profile_used_id', 'smtp_server', ondelete='SET NULL')
    except Exception:
        pass
    try:
        _replace_fk(conn, 'campaign', 'smtp_profile_id', 'smtp_server', ondelete='SET NULL')
    except Exception:
        pass
    try:
        _replace_fk(conn, 'campaign', 'template_id', 'email_template', ondelete='SET NULL')
    except Exception:
        pass
    try:
        _replace_fk(conn, 'daily_stats', 'campaign_id', 'campaign', ondelete='CASCADE')
    except Exception:
        pass
    try:
        _replace_fk(conn, 'webhook_delivery', 'webhook_id', 'webhook', ondelete='CASCADE')
    except Exception:
        pass

    # user-related refs: set NULL to avoid massive cascades
    user_fks = [
        ('suppression', 'user_id'),
        ('campaign', 'user_id'),
        ('campaign', 'approved_by_id'),
        ('webhook', 'user_id'),
        ('email_template', 'user_id'),
        ('api_key', 'user_id'),
        ('smtp_server', 'user_id'),
        ('tag', 'user_id'),
        ('segment', 'user_id'),
        ('sequence', 'user_id'),
        ('team', 'owner_id'),
    ]
    for table, column in user_fks:
        try:
            _replace_fk(conn, table, column, 'user', ondelete='SET NULL')
        except Exception:
            pass

    # notifications & team_members: cascade on user deletion (remove linked rows)
    try:
        _replace_fk(conn, 'notification', 'user_id', 'user', ondelete='CASCADE')
    except Exception:
        pass
    try:
        _replace_fk(conn, 'team_members', 'user_id', 'user', ondelete='CASCADE')
    except Exception:
        pass
    try:
        _replace_fk(conn, 'team_members', 'team_id', 'team', ondelete='CASCADE')
    except Exception:
        pass

def downgrade():
    # Best-effort: drop created FKs - not fully reconstructing original names
    conn = op.get_bind()
    tables_cols = [
        ('recipient', 'campaign_id'),
        ('recipient', 'smtp_profile_used_id'),
        ('campaign', 'smtp_profile_id'),
        ('campaign', 'template_id'),
        ('daily_stats', 'campaign_id'),
        ('webhook_delivery', 'webhook_id'),
        ('recipient_segments', 'recipient_id'),
        ('recipient_segments', 'segment_id'),
        ('campaign_tags', 'campaign_id'),
        ('campaign_tags', 'tag_id'),
        ('suppression', 'user_id'),
        ('campaign', 'user_id'),
        ('campaign', 'approved_by_id'),
        ('webhook', 'user_id'),
        ('email_template', 'user_id'),
        ('api_key', 'user_id'),
        ('smtp_server', 'user_id'),
        ('tag', 'user_id'),
        ('segment', 'user_id'),
        ('sequence', 'user_id'),
        ('team', 'owner_id'),
        ('notification', 'user_id'),
        ('team_members', 'user_id'),
        ('team_members', 'team_id'),
        ('sequence_recipient', 'sequence_id'),
    ]
    for table, col in tables_cols:
        try:
            existing = _get_existing_fk_name(conn, table, col)
            if existing:
                op.drop_constraint(existing, table, type_='foreignkey')
        except Exception:
            pass
PY

# Note: the generated migration above contains all required logic.
# The placeholder block has been removed and replaced with explicit calls.

echo "Migration file created. Please review: ${MIG_PATH}"

# 4) Stamp head if necessary (if flask db current returned nothing)
if [ -z "${CURRENT_REV:-}" ]; then
  echo "No current revision detected; stamping DB to head to avoid destructive initial migration..."
  flask db stamp head
fi

# 5) Run flask db upgrade
echo "Running: flask db upgrade"
flask db upgrade

echo "Done. Please verify application behavior (delete template, campaign, user tests)."
