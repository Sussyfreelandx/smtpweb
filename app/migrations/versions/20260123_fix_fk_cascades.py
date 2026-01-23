"""Make dependent foreign keys use ON DELETE CASCADE to avoid DROP table failures

Revision ID: 20260123_fix_fk_cascades
Revises: <replace-with-previous-revision>
Create Date: 2026-01-23 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision = '20260123_fix_fk_cascades'
down_revision = '<replace-with-previous-revision>'
branch_labels = None
depends_on = None


def _get_fk_name_on_column(conn, table_name, column_name):
    """Return foreign key constraint name for a given column, or None."""
    insp = inspect(conn)
    fkeys = insp.get_foreign_keys(table_name)
    for fk in fkeys:
        cols = fk.get('constrained_columns') or []
        if column_name in cols:
            return fk['name']
    return None


def _drop_and_create_fk(conn, table, column, ref_table, ref_column='id', fk_name=None):
    """Drop existing FK on table.column and create new FK with ON DELETE CASCADE."""
    # determine existing fk name
    existing = _get_fk_name_on_column(conn, table, column)
    if existing:
        op.drop_constraint(existing, table, type_='foreignkey')
    else:
        # attempt provided common default fallback names
        if fk_name:
            try:
                op.drop_constraint(fk_name, table, type_='foreignkey')
            except Exception:
                pass

    new_name = f'fk_{table}_{column}'
    op.create_foreign_key(
        new_name,
        source_table=table,
        referent_table=ref_table,
        local_cols=[column],
        remote_cols=[ref_column],
        ondelete='CASCADE'
    )


def upgrade():
    conn = op.get_bind()

    # List of (table, column, parent_table, fallback_fk_name)
    fks = [
        ('recipient', 'campaign_id', 'campaign', 'recipient_campaign_id_fkey'),
        ('daily_stats', 'campaign_id', 'campaign', 'daily_stats_campaign_id_fkey'),
        ('campaign_tags', 'campaign_id', 'campaign', 'campaign_tags_campaign_id_fkey'),
        ('campaign_tags', 'tag_id', 'tag', 'campaign_tags_tag_id_fkey'),
        ('sequence_recipient', 'sequence_id', 'sequence', 'sequence_recipient_sequence_id_fkey'),
        ('webhook_delivery', 'webhook_id', 'webhook', 'webhook_delivery_webhook_id_fkey'),
        # include other likely dependents if present
        ('api_key', 'user_id', 'user', None),
        ('notification', 'user_id', 'user', None),
        ('activity_log', 'user_id', 'user', None),
        ('smtp_server', 'user_id', 'user', None),
        ('email_template', 'user_id', 'user', None),
        ('daily_stats', 'smtp_profile_id', 'smtp_server', None),
        ('recipient', 'smtp_profile_used_id', 'smtp_server', None),
    ]

    for table, column, parent, fallback in fks:
        try:
            _drop_and_create_fk(conn, table, column, parent, 'id', fk_name=fallback)
        except Exception:
            # ignore if table/column doesn't exist in this schema
            pass


def downgrade():
    # In downgrade we drop created FKs (best-effort) and recreate basic FKs without cascade
    conn = op.get_bind()

    # Reverse list (same columns)
    fks = [
        ('recipient', 'campaign_id', 'campaign', 'recipient_campaign_id_fkey'),
        ('daily_stats', 'campaign_id', 'campaign', 'daily_stats_campaign_id_fkey'),
        ('campaign_tags', 'campaign_id', 'campaign', 'campaign_tags_campaign_id_fkey'),
        ('campaign_tags', 'tag_id', 'tag', 'campaign_tags_tag_id_fkey'),
        ('sequence_recipient', 'sequence_id', 'sequence', 'sequence_recipient_sequence_id_fkey'),
        ('webhook_delivery', 'webhook_id', 'webhook', 'webhook_delivery_webhook_id_fkey'),
        ('api_key', 'user_id', 'user', None),
        ('notification', 'user_id', 'user', None),
        ('activity_log', 'user_id', 'user', None),
        ('smtp_server', 'user_id', 'user', None),
        ('email_template', 'user_id', 'user', None),
        ('daily_stats', 'smtp_profile_id', 'smtp_server', None),
        ('recipient', 'smtp_profile_used_id', 'smtp_server', None),
    ]

    for table, column, parent, fallback in fks:
        try:
            # drop our fk (if exists)
            created_name = f'fk_{table}_{column}'
            try:
                op.drop_constraint(created_name, table, type_='foreignkey')
            except Exception:
                # determine any FK on the column and drop it
                existing = _get_fk_name_on_column(conn, table, column)
                if existing:
                    op.drop_constraint(existing, table, type_='foreignkey')
            # recreate a conservative FK without cascade (name fallback)
            fallback_name = fallback or f'{table}_{column}_fkey'
            op.create_foreign_key(fallback_name, table, parent, [column], ['id'])
        except Exception:
            pass