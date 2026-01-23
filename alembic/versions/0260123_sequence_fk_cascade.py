"""Make sequence_recipient.sequence_id FK use ON DELETE CASCADE

Revision ID: 20260123_sequence_fk_cascade
Revises: None
Create Date: 2026-01-23 00:00:00.000000

This migration ensures the foreign key from sequence_recipient.sequence_id -> sequence.id
uses ON DELETE CASCADE. It will attempt to drop any existing FK constraint that
references sequence_id (tries named constraint 'sequence_recipient_sequence_id_fkey'
first, then inspects and drops any FK that constrains sequence_id). Then it creates
a new FK named 'fk_sequence_recipient_sequence_id' with ON DELETE CASCADE.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '20260123_sequence_fk_cascade'
down_revision = None
branch_labels = None
depends_on = None


def _drop_existing_fk_on_sequence_id(bind):
    """
    Helper: drop an existing foreign key on sequence_recipient.sequence_id.
    Attempts to drop a constraint named 'sequence_recipient_sequence_id_fkey' first,
    otherwise inspects foreign keys and drops the first matching one.
    """
    inspector = sa.inspect(bind)
    # Try the common/explicit name first
    try:
        op.drop_constraint('sequence_recipient_sequence_id_fkey', 'sequence_recipient', type_='foreignkey')
        return True
    except Exception:
        # Not found or failed — inspect to find a FK that constrains sequence_id
        fkeys = inspector.get_foreign_keys('sequence_recipient')
        for fk in fkeys:
            constrained = fk.get('constrained_columns') or fk.get('constrained_columns', [])
            if constrained == ['sequence_id']:
                try:
                    op.drop_constraint(fk['name'], 'sequence_recipient', type_='foreignkey')
                    return True
                except Exception:
                    # continue to next if fail
                    continue
    return False


def upgrade():
    bind = op.get_bind()

    # Drop any existing FK referencing sequence_id
    _drop_existing_fk_on_sequence_id(bind)

    # Create new FK with ON DELETE CASCADE
    op.create_foreign_key(
        constraint_name='fk_sequence_recipient_sequence_id',
        source_table='sequence_recipient',
        referent_table='sequence',
        local_cols=['sequence_id'],
        remote_cols=['id'],
        ondelete='CASCADE'
    )


def downgrade():
    bind = op.get_bind()

    # Remove cascading FK if present
    try:
        op.drop_constraint('fk_sequence_recipient_sequence_id', 'sequence_recipient', type_='foreignkey')
    except Exception:
        # Try to drop any FK that constrains sequence_id
        inspector = sa.inspect(bind)
        fkeys = inspector.get_foreign_keys('sequence_recipient')
        for fk in fkeys:
            if fk.get('constrained_columns') == ['sequence_id']:
                try:
                    op.drop_constraint(fk['name'], 'sequence_recipient', type_='foreignkey')
                except Exception:
                    pass
                break

    # Recreate the original non-cascading FK (best-effort)
    op.create_foreign_key(
        constraint_name='sequence_recipient_sequence_id_fkey',
        source_table='sequence_recipient',
        referent_table='sequence',
        local_cols=['sequence_id'],
        remote_cols=['id']
    )