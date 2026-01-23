"""Make campaign.template_id FK use ON DELETE SET NULL to avoid DROP table failures

Revision ID: 20260123_campaign_template_fk_setnull
Revises: <replace-with-previous-revision>
Create Date: 2026-01-23 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '20260123_campaign_template_fk_setnull'
down_revision = '<replace-with-previous-revision>'
branch_labels = None
depends_on = None


def _get_fk_name(conn, table_name, column_name):
    # Use sa.inspect() which handles both Engine and Connection objects correctly
    insp = sa.inspect(conn)
    fkeys = insp.get_foreign_keys(table_name)
    for fk in fkeys:
        cols = fk.get('constrained_columns') or []
        if column_name in cols:
            return fk['name']
    return None


def upgrade():
    conn = op.get_bind()

    # Drop existing FK if it exists
    fk_name = _get_fk_name(conn, 'campaign', 'template_id')
    if fk_name:
        op.drop_constraint(fk_name, 'campaign', type_='foreignkey')

    # Create FK with ON DELETE SET NULL
    op.create_foreign_key(
        'campaign_template_id_fkey',
        source_table='campaign',
        referent_table='email_template',
        local_cols=['template_id'],
        remote_cols=['id'],
        ondelete='SET NULL'
    )


def downgrade():
    conn = op.get_bind()
    fk_name = _get_fk_name(conn, 'campaign', 'template_id')
    if fk_name:
        op.drop_constraint(fk_name, 'campaign', type_='foreignkey')

    # Recreate a conservative FK without ON DELETE clause (default behavior)
    op.create_foreign_key(
        'campaign_template_id_fkey',
        source_table='campaign',
        referent_table='email_template',
        local_cols=['template_id'],
        remote_cols=['id']
    )
