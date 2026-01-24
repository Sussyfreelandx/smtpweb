"""Fix FK constraints to use explicit ON DELETE rules to avoid DROP failures

Revision ID: 20260124_fix_fk_dependencies
Revises: <HEAD_REV>
Create Date: 2026-01-24 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '20260124_fix_fk_dependencies'
down_revision = '<HEAD_REV>'
branch_labels = None
depends_on = None


def _safe_drop_constraint(name, table):
    try:
        op.drop_constraint(name, table, type_='foreignkey')
    except Exception:
        # best-effort: ignore if the constraint does not exist or can't be dropped
        pass


def upgrade():
    # Replace many FK constraints with explicit ON DELETE behavior (best-effort)
    # child/join tables -> ON DELETE CASCADE
    _safe_drop_constraint('recipient_segments_recipient_id_fkey', 'recipient_segments')
    _safe_drop_constraint('recipient_segments_segment_id_fkey', 'recipient_segments')
    op.create_foreign_key('fk_recipient_segments_recipient_id', 'recipient_segments', 'recipient', ['recipient_id'], ['id'], ondelete='CASCADE')
    op.create_foreign_key('fk_recipient_segments_segment_id', 'recipient_segments', 'segment', ['segment_id'], ['id'], ondelete='CASCADE')

    _safe_drop_constraint('campaign_tags_campaign_id_fkey', 'campaign_tags')
    _safe_drop_constraint('campaign_tags_tag_id_fkey', 'campaign_tags')
    op.create_foreign_key('fk_campaign_tags_campaign_id', 'campaign_tags', 'campaign', ['campaign_id'], ['id'], ondelete='CASCADE')
    op.create_foreign_key('fk_campaign_tags_tag_id', 'campaign_tags', 'tag', ['tag_id'], ['id'], ondelete='CASCADE')

    _safe_drop_constraint('webhook_delivery_webhook_id_fkey', 'webhook_delivery')
    op.create_foreign_key('fk_webhook_delivery_webhook_id', 'webhook_delivery', 'webhook', ['webhook_id'], ['id'], ondelete='CASCADE')

    _safe_drop_constraint('sequence_recipient_sequence_id_fkey', 'sequence_recipient')
    op.create_foreign_key('fk_sequence_recipient_sequence_id', 'sequence_recipient', 'sequence', ['sequence_id'], ['id'], ondelete='CASCADE')

    _safe_drop_constraint('recipient_campaign_id_fkey', 'recipient')
    op.create_foreign_key('fk_recipient_campaign_id', 'recipient', 'campaign', ['campaign_id'], ['id'], ondelete='CASCADE')

    _safe_drop_constraint('daily_stats_campaign_id_fkey', 'daily_stats')
    op.create_foreign_key('fk_daily_stats_campaign_id', 'daily_stats', 'campaign', ['campaign_id'], ['id'], ondelete='CASCADE')

    # Optional refs -> ON DELETE SET NULL (safer)
    _safe_drop_constraint('recipient_smtp_profile_used_id_fkey', 'recipient')
    op.create_foreign_key('fk_recipient_smtp_profile_used_id', 'recipient', 'smtp_server', ['smtp_profile_used_id'], ['id'], ondelete='SET NULL')

    _safe_drop_constraint('campaign_smtp_profile_id_fkey', 'campaign')
    op.create_foreign_key('fk_campaign_smtp_profile_id', 'campaign', 'smtp_server', ['smtp_profile_id'], ['id'], ondelete='SET NULL')

    _safe_drop_constraint('campaign_template_id_fkey', 'campaign')
    op.create_foreign_key('fk_campaign_template_id', 'campaign', 'email_template', ['template_id'], ['id'], ondelete='SET NULL')

    _safe_drop_constraint('smtp_server_user_id_fkey', 'smtp_server')
    op.create_foreign_key('fk_smtp_server_user_id', 'smtp_server', 'user', ['user_id'], ['id'], ondelete='SET NULL')

    _safe_drop_constraint('email_template_user_id_fkey', 'email_template')
    op.create_foreign_key('fk_email_template_user_id', 'email_template', 'user', ['user_id'], ['id'], ondelete='SET NULL')

    _safe_drop_constraint('suppression_user_id_fkey', 'suppression')
    op.create_foreign_key('fk_suppression_user_id', 'suppression', 'user', ['user_id'], ['id'], ondelete='SET NULL')

    _safe_drop_constraint('api_key_user_id_fkey', 'api_key')
    op.create_foreign_key('fk_api_key_user_id', 'api_key', 'user', ['user_id'], ['id'], ondelete='SET NULL')

    _safe_drop_constraint('tag_user_id_fkey', 'tag')
    op.create_foreign_key('fk_tag_user_id', 'tag', 'user', ['user_id'], ['id'], ondelete='SET NULL')

    _safe_drop_constraint('segment_user_id_fkey', 'segment')
    op.create_foreign_key('fk_segment_user_id', 'segment', 'user', ['user_id'], ['id'], ondelete='SET NULL')

    _safe_drop_constraint('sequence_user_id_fkey', 'sequence')
    op.create_foreign_key('fk_sequence_user_id', 'sequence', 'user', ['user_id'], ['id'], ondelete='SET NULL')

    _safe_drop_constraint('team_owner_id_fkey', 'team')
    op.create_foreign_key('fk_team_owner_id', 'team', 'user', ['owner_id'], ['id'], ondelete='SET NULL')

    # Notifications and membership: cascade (remove related rows when user/team removed)
    _safe_drop_constraint('notification_user_id_fkey', 'notification')
    op.create_foreign_key('fk_notification_user_id', 'notification', 'user', ['user_id'], ['id'], ondelete='CASCADE')

    _safe_drop_constraint('team_members_user_id_fkey', 'team_members')
    _safe_drop_constraint('team_members_team_id_fkey', 'team_members')
    op.create_foreign_key('fk_team_members_user_id', 'team_members', 'user', ['user_id'], ['id'], ondelete='CASCADE')
    op.create_foreign_key('fk_team_members_team_id', 'team_members', 'team', ['team_id'], ['id'], ondelete='CASCADE')

    # Webhook -> user (optional)
    _safe_drop_constraint('webhook_user_id_fkey', 'webhook')
    op.create_foreign_key('fk_webhook_user_id', 'webhook', 'user', ['user_id'], ['id'], ondelete='SET NULL')

    # Other defensive replacements (if present)
    _safe_drop_constraint('activity_log_team_id_fkey', 'activity_log')
    op.create_foreign_key('fk_activity_log_team_id', 'activity_log', 'team', ['team_id'], ['id'], ondelete='SET NULL')

    # If other FK constraints exist not addressed here, add them similarly.

def downgrade():
    # Best-effort: drop the created constraints so migration can be reversed.
    for name, table in [
        ('fk_recipient_segments_recipient_id', 'recipient_segments'),
        ('fk_recipient_segments_segment_id', 'recipient_segments'),
        ('fk_campaign_tags_campaign_id', 'campaign_tags'),
        ('fk_campaign_tags_tag_id', 'campaign_tags'),
        ('fk_webhook_delivery_webhook_id', 'webhook_delivery'),
        ('fk_sequence_recipient_sequence_id', 'sequence_recipient'),
        ('fk_recipient_campaign_id', 'recipient'),
        ('fk_daily_stats_campaign_id', 'daily_stats'),
        ('fk_recipient_smtp_profile_used_id', 'recipient'),
        ('fk_campaign_smtp_profile_id', 'campaign'),
        ('fk_campaign_template_id', 'campaign'),
        ('fk_smtp_server_user_id', 'smtp_server'),
        ('fk_email_template_user_id', 'email_template'),
        ('fk_suppression_user_id', 'suppression'),
        ('fk_api_key_user_id', 'api_key'),
        ('fk_tag_user_id', 'tag'),
        ('fk_segment_user_id', 'segment'),
        ('fk_sequence_user_id', 'sequence'),
        ('fk_team_owner_id', 'team'),
        ('fk_notification_user_id', 'notification'),
        ('fk_team_members_user_id', 'team_members'),
        ('fk_team_members_team_id', 'team_members'),
        ('fk_webhook_user_id', 'webhook'),
        ('fk_activity_log_team_id', 'activity_log'),
    ]:
        try:
            op.drop_constraint(name, table, type_='foreignkey')
        except Exception:
            pass
