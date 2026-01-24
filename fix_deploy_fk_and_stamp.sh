#!/usr/bin/env bash
# fix_deploy_fk_and_stamp.sh
# One-shot script to:
#  - backup Postgres DB
#  - stamp alembic to HEAD (avoid re-running destructive initial migration)
#  - replace problematic foreign keys with safe ON DELETE behavior
#  - run 'flask db upgrade'
#
# USAGE:
#   1) Set environment variables: DATABASE_URL (full psql url) and FLASK_APP (eg wsgi:app).
#      Example:
#        export DATABASE_URL="postgresql://dbuser:dbpass@dbhost:5432/dbname"
#        export FLASK_APP="wsgi:app"
#   2) Run: ./fix_deploy_fk_and_stamp.sh
#
# IMPORTANT:
#   - BACKUP your database before running this script! The script will attempt an automated backup
#     using pg_dump if available, but you should also take your normal managed DB snapshot.
#   - Run first on a staging copy and verify app behavior (deletes, cascades, set-null) before production.
#   - The script is best-effort; if your schema uses nonstandard FK names it will still replace FKs by column.
#   - You must run this from your project root where flask & migrations are available in the virtualenv.
set -euo pipefail

# --- CONFIGURATION (override via env) ---
DB_URL="${DATABASE_URL:-}"
FLASK_APP="${FLASK_APP:-wsgi:app}"
BACKUP_DIR="${BACKUP_DIR:-./db_backups}"
PG_DUMP_CMD="${PG_DUMP_CMD:-pg_dump}"
PSQL_CMD="${PSQL_CMD:-psql}"
RETRIES="${RETRIES:-3}"
SLEEP_BEFORE_UPGRADE="${SLEEP_BEFORE_UPGRADE:-3}"

if [ -z "$DB_URL" ]; then
  echo "ERROR: DATABASE_URL is not set. Export it and re-run."
  exit 1
fi

echo "Starting one-shot FK fix and Alembic stamp procedure."
echo "DATABASE_URL: ${DB_URL}"
echo "FLASK_APP: ${FLASK_APP}"

mkdir -p "$BACKUP_DIR"

# 1) Backup DB (best-effort)
echo "=== STEP 1: BACKUP DATABASE ==="
BACKUP_FILE="${BACKUP_DIR}/backup_$(date -u +%Y%m%dT%H%M%SZ).sql"
if command -v "$PG_DUMP_CMD" >/dev/null 2>&1; then
  echo "Running pg_dump to ${BACKUP_FILE} ..."
  if $PG_DUMP_CMD --version >/dev/null 2>&1; then
    # Use simple SQL dump; for large DBs you may prefer custom (-Fc) flag
    $PG_DUMP_CMD "$DB_URL" -f "$BACKUP_FILE" && echo "Backup completed: $BACKUP_FILE"
  else
    echo "pg_dump not available or failed; skip automated backup. Please snapshot your DB manually!"
  fi
else
  echo "pg_dump not found on PATH. Please create a DB snapshot manually before proceeding!"
fi

# 2) Stamp the DB to Alembic head (prevents destructive initial migrations)
echo "=== STEP 2: STAMP ALEMBIC TO HEAD ==="
export FLASK_APP="$FLASK_APP"
echo "Running: flask db stamp head"
# allow a couple attempts in case of transient environment issues
attempt=1
while [ $attempt -le $RETRIES ]; do
  if flask db stamp head; then
    echo "flask db stamp head succeeded."
    break
  else
    echo "flask db stamp head attempt $attempt failed; retrying in 2s..."
    attempt=$((attempt+1))
    sleep 2
  fi
done
if [ $attempt -gt $RETRIES ]; then
  echo "ERROR: flask db stamp head repeatedly failed. Resolve stamp issue before proceeding."
  exit 1
fi

# 3) Apply comprehensive FK replacements (safe ON DELETE rules)
echo "=== STEP 3: APPLY DEFENSIVE FK REPLACEMENTS ==="
echo "This will drop and recreate a set of foreign key constraints with ON DELETE SET NULL or CASCADE."
echo "If psql is available the script will execute via the provided DATABASE_URL."

SQL=$(cat <<'EOF'
-- Defensive FK replacement script (best-effort)
BEGIN;

-- recipient_segments (join)
ALTER TABLE IF EXISTS recipient_segments DROP CONSTRAINT IF EXISTS recipient_segments_recipient_id_fkey;
ALTER TABLE IF EXISTS recipient_segments DROP CONSTRAINT IF EXISTS recipient_segments_segment_id_fkey;
ALTER TABLE IF EXISTS recipient_segments ADD CONSTRAINT fk_recipient_segments_recipient_id FOREIGN KEY (recipient_id) REFERENCES recipient(id) ON DELETE CASCADE;
ALTER TABLE IF EXISTS recipient_segments ADD CONSTRAINT fk_recipient_segments_segment_id FOREIGN KEY (segment_id) REFERENCES segment(id) ON DELETE CASCADE;

-- campaign_tags (join)
ALTER TABLE IF EXISTS campaign_tags DROP CONSTRAINT IF EXISTS campaign_tags_campaign_id_fkey;
ALTER TABLE IF EXISTS campaign_tags DROP CONSTRAINT IF EXISTS campaign_tags_tag_id_fkey;
ALTER TABLE IF EXISTS campaign_tags ADD CONSTRAINT fk_campaign_tags_campaign_id FOREIGN KEY (campaign_id) REFERENCES campaign(id) ON DELETE CASCADE;
ALTER TABLE IF EXISTS campaign_tags ADD CONSTRAINT fk_campaign_tags_tag_id FOREIGN KEY (tag_id) REFERENCES tag(id) ON DELETE CASCADE;

-- webhook_delivery -> cascade with webhook
ALTER TABLE IF EXISTS webhook_delivery DROP CONSTRAINT IF EXISTS webhook_delivery_webhook_id_fkey;
ALTER TABLE IF EXISTS webhook_delivery ADD CONSTRAINT fk_webhook_delivery_webhook_id FOREIGN KEY (webhook_id) REFERENCES webhook(id) ON DELETE CASCADE;

-- recipient -> campaign (child)
ALTER TABLE IF EXISTS recipient DROP CONSTRAINT IF EXISTS recipient_campaign_id_fkey;
ALTER TABLE IF EXISTS recipient ADD CONSTRAINT fk_recipient_campaign_id FOREIGN KEY (campaign_id) REFERENCES campaign(id) ON DELETE CASCADE;

-- recipient.smtp_profile_used_id -> set NULL when smtp removed
ALTER TABLE IF EXISTS recipient DROP CONSTRAINT IF EXISTS recipient_smtp_profile_used_id_fkey;
ALTER TABLE IF EXISTS recipient ADD CONSTRAINT fk_recipient_smtp_profile_used_id FOREIGN KEY (smtp_profile_used_id) REFERENCES smtp_server(id) ON DELETE SET NULL;

-- campaign.smtp_profile_id -> set NULL
ALTER TABLE IF EXISTS campaign DROP CONSTRAINT IF EXISTS campaign_smtp_profile_id_fkey;
ALTER TABLE IF EXISTS campaign ADD CONSTRAINT fk_campaign_smtp_profile_id FOREIGN KEY (smtp_profile_id) REFERENCES smtp_server(id) ON DELETE SET NULL;

-- campaign.template_id -> set NULL
ALTER TABLE IF EXISTS campaign DROP CONSTRAINT IF EXISTS campaign_template_id_fkey;
ALTER TABLE IF EXISTS campaign ADD CONSTRAINT fk_campaign_template_id FOREIGN KEY (template_id) REFERENCES email_template(id) ON DELETE SET NULL;

-- daily_stats -> campaign cascade
ALTER TABLE IF EXISTS daily_stats DROP CONSTRAINT IF EXISTS daily_stats_campaign_id_fkey;
ALTER TABLE IF EXISTS daily_stats ADD CONSTRAINT fk_daily_stats_campaign_id FOREIGN KEY (campaign_id) REFERENCES campaign(id) ON DELETE CASCADE;

-- sequence_recipient -> sequence cascade
ALTER TABLE IF EXISTS sequence_recipient DROP CONSTRAINT IF EXISTS sequence_recipient_sequence_id_fkey;
ALTER TABLE IF EXISTS sequence_recipient ADD CONSTRAINT fk_sequence_recipient_sequence_id FOREIGN KEY (sequence_id) REFERENCES sequence(id) ON DELETE CASCADE;

-- smtp_server.user_id -> set NULL
ALTER TABLE IF EXISTS smtp_server DROP CONSTRAINT IF EXISTS smtp_server_user_id_fkey;
ALTER TABLE IF EXISTS smtp_server ADD CONSTRAINT fk_smtp_server_user_id FOREIGN KEY (user_id) REFERENCES "user"(id) ON DELETE SET NULL;

-- email_template.user_id -> set NULL
ALTER TABLE IF EXISTS email_template DROP CONSTRAINT IF EXISTS email_template_user_id_fkey;
ALTER TABLE IF EXISTS email_template ADD CONSTRAINT fk_email_template_user_id FOREIGN KEY (user_id) REFERENCES "user"(id) ON DELETE SET NULL;

-- suppression.user_id -> set NULL
ALTER TABLE IF EXISTS suppression DROP CONSTRAINT IF EXISTS suppression_user_id_fkey;
ALTER TABLE IF EXISTS suppression ADD CONSTRAINT fk_suppression_user_id FOREIGN KEY (user_id) REFERENCES "user"(id) ON DELETE SET NULL;

-- notification.user_id -> cascade
ALTER TABLE IF EXISTS notification DROP CONSTRAINT IF EXISTS notification_user_id_fkey;
ALTER TABLE IF EXISTS notification ADD CONSTRAINT fk_notification_user_id FOREIGN KEY (user_id) REFERENCES "user"(id) ON DELETE CASCADE;

-- team_members -> cascade both sides
ALTER TABLE IF EXISTS team_members DROP CONSTRAINT IF EXISTS team_members_user_id_fkey;
ALTER TABLE IF EXISTS team_members DROP CONSTRAINT IF EXISTS team_members_team_id_fkey;
ALTER TABLE IF EXISTS team_members ADD CONSTRAINT fk_team_members_user_id FOREIGN KEY (user_id) REFERENCES "user"(id) ON DELETE CASCADE;
ALTER TABLE IF EXISTS team_members ADD CONSTRAINT fk_team_members_team_id FOREIGN KEY (team_id) REFERENCES team(id) ON DELETE CASCADE;

-- tag.user_id -> set NULL
ALTER TABLE IF EXISTS tag DROP CONSTRAINT IF EXISTS tag_user_id_fkey;
ALTER TABLE IF EXISTS tag ADD CONSTRAINT fk_tag_user_id FOREIGN KEY (user_id) REFERENCES "user"(id) ON DELETE SET NULL;

-- segment.user_id -> set NULL
ALTER TABLE IF EXISTS segment DROP CONSTRAINT IF EXISTS segment_user_id_fkey;
ALTER TABLE IF EXISTS segment ADD CONSTRAINT fk_segment_user_id FOREIGN KEY (user_id) REFERENCES "user"(id) ON DELETE SET NULL;

-- sequence.user_id -> set NULL
ALTER TABLE IF EXISTS sequence DROP CONSTRAINT IF EXISTS sequence_user_id_fkey;
ALTER TABLE IF EXISTS sequence ADD CONSTRAINT fk_sequence_user_id FOREIGN KEY (user_id) REFERENCES "user"(id) ON DELETE SET NULL;

-- team.owner_id -> set NULL
ALTER TABLE IF EXISTS team DROP CONSTRAINT IF EXISTS team_owner_id_fkey;
ALTER TABLE IF EXISTS team ADD CONSTRAINT fk_team_owner_id FOREIGN KEY (owner_id) REFERENCES "user"(id) ON DELETE SET NULL;

COMMIT;
EOF
)

# run via psql
if command -v "$PSQL_CMD" >/dev/null 2>&1; then
  echo "Executing FK SQL script via psql..."
  echo "$SQL" | $PSQL_CMD "$DB_URL"
  echo "FK replacements executed."
else
  echo "psql not found. Please run the following SQL in your DB admin tool:"
  echo "---- BEGIN SQL ----"
  echo "$SQL"
  echo "---- END SQL ----"
  exit 1
fi

# small pause
sleep "$SLEEP_BEFORE_UPGRADE"

# 4) Run flask db upgrade
echo "=== STEP 4: RUN flask db upgrade ==="
attempt=1
while [ $attempt -le $RETRIES ]; do
  if flask db upgrade; then
    echo "flask db upgrade succeeded."
    break
  else
    echo "flask db upgrade attempt $attempt failed; retrying in 3s..."
    attempt=$((attempt+1))
    sleep 3
  fi
done
if [ $attempt -gt $RETRIES ]; then
  echo "ERROR: flask db upgrade failed after retries. Inspect logs and DB state."
  exit 1
fi

echo "All done — FK setup applied and migrations upgraded. Please verify application behavior."
exit 0
