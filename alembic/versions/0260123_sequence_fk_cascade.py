"""Make sequence_recipient.sequence_id FK use ON DELETE CASCADE

Revision ID: 20260123_sequence_fk_cascade
Revises: 
Create Date: 2026-01-23 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


# revision identifiers, used by Alembic.
revision = '20260123_sequence_fk_cascade'
down_revision = None  # Replace with actual previous revision ID if exists
branch_labels = None
depends_on = None


def _get_fk_name_on_column(conn, table_name, column_name):
    """Return foreign key constraint name for a given column, or None."""
    insp = inspect(conn)
    fkeys = insp.get_foreign_keys(table_name)
    for fk in fkeys:
        if fk.get('constrained_columns') and column_name in fk['constrained_columns']:
            return fk['name']
    return None


def upgrade():
    conn = op.get_bind()

    # Determine existing FK name (if any) for sequence_recipient.sequence_id
    fk_name = _get_fk_name_on_column(conn, 'sequence_recipient', 'sequence_id')

    # If found, drop it
    if fk_name:
        op.drop_constraint(fk_name, 'sequence_recipient', type_='foreignkey')
    else:
        # attempt common name fallback
        try:
            op.drop_constraint('sequence_recipient_sequence_id_fkey', 'sequence_recipient', type_='foreignkey')
        except Exception:
            # nothing to drop
            pass

    # Create foreign key with ON DELETE CASCADE
    op.create_foreign_key(
        'fk_sequence_recipient_sequence_id',
        'sequence_recipient',
        'sequence',
        ['sequence_id'],
        ['id'],
        ondelete='CASCADE'
    )


def downgrade():
    # Roll back to a standard FK without cascade
    try:
        op.drop_constraint('fk_sequence_recipient_sequence_id', 'sequence_recipient', type_='foreignkey')
    except Exception:
        # try any existing FK
        conn = op.get_bind()
        fk_name = _get_fk_name_on_column(conn, 'sequence_recipient', 'sequence_id')
        if fk_name:
            op.drop_constraint(fk_name, 'sequence_recipient', type_='foreignkey')

    op.create_foreign_key(
        'sequence_recipient_sequence_id_fkey',
        'sequence_recipient',
        'sequence',
        ['sequence_id'],
        ['id']
    )
