"""Fix FK constraints that block DROP operations by adding safe ON DELETE policies

Revision ID: 20260123_fix_fk_dependencies
Revises: <replace-with-previous-revision>
Create Date: 2026-01-23 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.engine.reflection import Inspector

# revision identifiers, used by Alembic.
revision = '20260123_fix_fk_dependencies'
down_revision = '<replace-with-previous-revision>'
branch_labels = None
depends_on = None


def _get_existing_fk_name(conn, table_name, column_name):
    """Return the FK constraint name for table.column if found, else None."""
    insp = Inspector.from_engine(conn)
    # Handle case where table might not exist in some environments
    if not insp.has_table(table_name):
        return None
        
    fks = insp.get_foreign_keys(table_name)
    for fk in fks:
        cols = fk.get('constrained_columns') or []
        if column_name in cols:
            return fk['name']
    return None


def _replace_fk(conn, table, column, referent_table, ondelete='SET NULL'):
    """Drop existing FK on table.column (if present) and create a new one with ondelete."""
    existing = _get_existing_fk_name(conn, table, column)
    if existing:
        try:
            op.drop_constraint(existing, table, type_='foreignkey')
        except Exception:
            # best-effort: ignore if can't drop
            pass

    fk_name = f'fk_{table}_{column}'
    try:
        op.create_foreign_key(
            fk_name,
            source_table=table,
            referent_table=referent_table,
            local_cols=[column],
            remote_cols=['id'],
            ondelete=ondelete
        )
    except Exception:
        # Ignore errors if tables don't exist in specific envs
        pass


def upgrade():
    conn = op.get_bind()

    # The list below includes the common parent->child relationships that caused failures.
    # Use ON DELETE CASCADE for true children (e.g. deliveries) and SET NULL for optional references.
    fks_to_fix = [
        # child_table, column, parent_table, ondelete
        ('recipient', 'campaign_id', 'campaign', 'CASCADE'),
        ('daily_stats', 'campaign_id', 'campaign', 'CASCADE'),
        ('campaign_tags', 'campaign_id', 'campaign', 'CASCADE'),
        ('campaign_tags', 'tag_id', 'tag', 'CASCADE'),
        ('webhook_delivery', 'webhook_id', 'webhook', 'CASCADE'),

        ('recipient', 'smtp_profile_used_id', 'smtp_server', 'SET NULL'),
        ('campaign', 'smtp_profile_id', 'smtp_server', 'SET NULL'),

        ('campaign', 'template_id', 'email_template', 'SET NULL'),

        ('suppression', 'user_id', 'user', 'SET NULL'),
        ('campaign', 'user_id', 'user', 'SET NULL'),
        ('campaign', 'approved_by_id', 'user', 'SET NULL'),
        ('webhook', 'user_id', 'user', 'SET NULL'),
        ('team_members', 'user_id', 'user', 'CASCADE'),
        ('tag', 'user_id', 'user', 'SET NULL'),
        ('segment', 'user_id', 'user', 'SET NULL'),
        ('email_template', 'user_id', 'user', 'SET NULL'),
        ('api_key', 'user_id', 'user', 'SET NULL'),
        ('team', 'owner_id', 'user', 'SET NULL'),
        ('notification', 'user_id', 'user', 'CASCADE'),
        ('sequence', 'user_id', 'user', 'SET NULL'),
        ('smtp_server', 'user_id', 'user', 'SET NULL'),

        # recipient -> smtp_server (already above)
        # Additional: sequence_recipient -> sequence
        ('sequence_recipient', 'sequence_id', 'sequence', 'CASCADE'),
    ]

    for table, column, parent, ondelete in fks_to_fix:
        try:
            _replace_fk(conn, table, column, parent, ondelete=ondelete)
        except Exception:
            # ignore missing tables/columns in some environments
            pass


def downgrade():
    # Best-effort: drop created FKs and recreate plain FKs without ON DELETE (may be destructive)
    conn = op.get_bind()

    def _drop_fk_if_exists(table, column):
        existing = _get_existing_fk_name(conn, table, column)
        if existing:
            try:
                op.drop_constraint(existing, table, type_='foreignkey')
            except Exception:
                pass

    # We need the parent table to recreate the FK correctly.
    tables_cols = [
        ('recipient', 'campaign_id', 'campaign'),
        ('daily_stats', 'campaign_id', 'campaign'),
        ('campaign_tags', 'campaign_id', 'campaign'),
        ('campaign_tags', 'tag_id', 'tag'),
        ('webhook_delivery', 'webhook_id', 'webhook'),
        ('recipient', 'smtp_profile_used_id', 'smtp_server'),
        ('campaign', 'smtp_profile_id', 'smtp_server'),
        ('campaign', 'template_id', 'email_template'),
        ('suppression', 'user_id', 'user'),
        ('campaign', 'user_id', 'user'),
        ('campaign', 'approved_by_id', 'user'),
        ('webhook', 'user_id', 'user'),
        ('team_members', 'user_id', 'user'),
        ('tag', 'user_id', 'user'),
        ('segment', 'user_id', 'user'),
        ('email_template', 'user_id', 'user'),
        ('api_key', 'user_id', 'user'),
        ('team', 'owner_id', 'user'),
        ('notification', 'user_id', 'user'),
        ('sequence', 'user_id', 'user'),
        ('smtp_server', 'user_id', 'user'),
        ('sequence_recipient', 'sequence_id', 'sequence'),
    ]

    for table, col, parent in tables_cols:
        try:
            _drop_fk_if_exists(table, col)
            # Recreate a conservative FK without explicit ON DELETE (default behavior)
            op.create_foreign_key(
                f'fk_{table}_{col}', 
                table, 
                parent, 
                [col], 
                ['id']
            )
        except Exception:
            # Don't attempt to reconstruct unknown parent names here - it's a best-effort downgrade
            pass
