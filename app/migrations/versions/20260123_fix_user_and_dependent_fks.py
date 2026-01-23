"""Make user and campaign/webhook dependent foreign keys use safer ON DELETE policies

Revision ID: 20260123_fix_user_and_dependent_fks
Revises: <replace-with-previous-revision>
Create Date: 2026-01-23 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.engine.reflection import Inspector

# revision identifiers, used by Alembic.
revision = '20260123_fix_user_and_dependent_fks'
down_revision = '<replace-with-previous-revision>'
branch_labels = None
depends_on = None


def _get_fk_name(conn, table_name, column_name):
    insp = Inspector.from_engine(conn)
    for fk in insp.get_foreign_keys(table_name):
        cols = fk.get('constrained_columns') or []
        if column_name in cols:
            return fk['name']
    return None


def upgrade():
    conn = op.get_bind()

    # Helper to drop and create FK using batch operations (SQLite safe)
    def replace_fk(table, column, referent, ondelete):
        existing = _get_fk_name(conn, table, column)
        
        with op.batch_alter_table(table) as batch_op:
            if existing:
                batch_op.drop_constraint(existing, type_='foreignkey')
            
            # Note: referent table is passed as the first arg in batch mode, 
            # source table is implied by the context
            batch_op.create_foreign_key(
                f'fk_{table}_{column}', 
                referent, 
                [column], 
                ['id'], 
                ondelete=ondelete
            )

    # user-related FKs -> SET NULL (safer)
    replace_fk('suppression', 'user_id', 'user', 'SET NULL')
    replace_fk('campaign', 'approved_by_id', 'user', 'SET NULL')
    replace_fk('campaign', 'user_id', 'user', 'SET NULL')
    replace_fk('webhook', 'user_id', 'user', 'SET NULL')
    replace_fk('tag', 'user_id', 'user', 'SET NULL')
    replace_fk('segment', 'user_id', 'user', 'SET NULL')
    replace_fk('email_template', 'user_id', 'user', 'SET NULL')
    replace_fk('api_key', 'user_id', 'user', 'SET NULL')
    replace_fk('team', 'owner_id', 'user', 'SET NULL')
    replace_fk('sequence', 'user_id', 'user', 'SET NULL')
    replace_fk('smtp_server', 'user_id', 'user', 'SET NULL')

    # notification.user_id -> CASCADE (notifications tied to user lifecycle)
    replace_fk('notification', 'user_id', 'user', 'CASCADE')

    # webhook_delivery.webhook_id -> CASCADE (child of webhook)
    replace_fk('webhook_delivery', 'webhook_id', 'webhook', 'CASCADE')

    # recipient and daily_stats -> CASCADE for campaign
    replace_fk('recipient', 'campaign_id', 'campaign', 'CASCADE')
    replace_fk('daily_stats', 'campaign_id', 'campaign', 'CASCADE')


def downgrade():
    conn = op.get_bind()

    # drop the created FKs
    targets = [
        ('suppression','user_id'),
        ('campaign','approved_by_id'),
        ('campaign','user_id'),
        ('webhook','user_id'),
        ('tag','user_id'),
        ('segment','user_id'),
        ('email_template','user_id'),
        ('api_key','user_id'),
        ('team','owner_id'),
        ('sequence','user_id'),
        ('smtp_server','user_id'),
        ('notification','user_id'),
        ('webhook_delivery','webhook_id'),
        ('recipient','campaign_id'),
        ('daily_stats','campaign_id'),
    ]

    for table, col in targets:
        try:
            existing = _get_fk_name(conn, table, col)
            if existing:
                with op.batch_alter_table(table) as batch_op:
                    batch_op.drop_constraint(existing, type_='foreignkey')
        except Exception:
            pass
