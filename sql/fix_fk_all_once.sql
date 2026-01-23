-- fix_fk_all_once.sql
-- Comprehensive FK replacement script to avoid "cannot drop table ... dependent objects exist"
-- BACKUP YOUR DATABASE BEFORE RUNNING
-- Run: psql "postgresql://<user>:<pass>@<host>:<port>/<db>" -f fix_fk_all_once.sql

DO
$$
DECLARE
    rec record;
    -- Added 'webhook' to this list because it is modified in the body script
    target_tables text[] := ARRAY[
        'recipient_segments',
        'campaign_tags',
        'team_members',
        'webhook_delivery',
        'recipient',
        'daily_stats',
        'campaign',
        'smtp_server',
        'email_template',
        'suppression',
        'notification',
        'api_key',
        'tag',
        'segment',
        'sequence',
        'sequence_recipient',
        'team',
        'webhook'
    ];
BEGIN
    -- 1) Drop ALL foreign key constraints on listed tables (best-effort)
    FOR rec IN
        SELECT con.conname, n.nspname AS schema_name, cls.relname AS table_name
        FROM pg_constraint con
        JOIN pg_class cls ON cls.oid = con.conrelid
        JOIN pg_namespace n ON n.oid = cls.relnamespace
        WHERE con.contype = 'f'
          AND cls.relname::text = ANY(target_tables)
    LOOP
        BEGIN
            EXECUTE format('ALTER TABLE %I.%I DROP CONSTRAINT IF EXISTS %I;', rec.schema_name, rec.table_name, rec.conname);
        EXCEPTION WHEN OTHERS THEN
            -- ignore errors dropping constraints
            RAISE NOTICE 'Could not drop constraint % on %.%', rec.conname, rec.schema_name, rec.table_name;
        END;
    END LOOP;

    -- 2) Recreate safe FK constraints for known relationships.
    -- Note: Each CREATE is wrapped in an exception block so the script is resilient.

    -- recipient_segments (join table) -> cascade both sides
    BEGIN
        EXECUTE '
            ALTER TABLE IF EXISTS recipient_segments
            ADD CONSTRAINT fk_recipient_segments_recipient_id FOREIGN KEY (recipient_id) REFERENCES recipient(id) ON DELETE CASCADE';
    EXCEPTION WHEN OTHERS THEN NULL; END;

    BEGIN
        EXECUTE '
            ALTER TABLE IF EXISTS recipient_segments
            ADD CONSTRAINT fk_recipient_segments_segment_id FOREIGN KEY (segment_id) REFERENCES segment(id) ON DELETE CASCADE';
    EXCEPTION WHEN OTHERS THEN NULL; END;

    -- campaign_tags (join table) -> cascade
    BEGIN
        EXECUTE '
            ALTER TABLE IF EXISTS campaign_tags
            ADD CONSTRAINT fk_campaign_tags_campaign_id FOREIGN KEY (campaign_id) REFERENCES campaign(id) ON DELETE CASCADE';
    EXCEPTION WHEN OTHERS THEN NULL; END;

    BEGIN
        EXECUTE '
            ALTER TABLE IF EXISTS campaign_tags
            ADD CONSTRAINT fk_campaign_tags_tag_id FOREIGN KEY (tag_id) REFERENCES tag(id) ON DELETE CASCADE';
    EXCEPTION WHEN OTHERS THEN NULL; END;

    -- team_members (join table) -> cascade on delete of user or team
    BEGIN
        EXECUTE '
            ALTER TABLE IF EXISTS team_members
            ADD CONSTRAINT fk_team_members_user_id FOREIGN KEY (user_id) REFERENCES "user"(id) ON DELETE CASCADE';
    EXCEPTION WHEN OTHERS THEN NULL; END;

    BEGIN
        EXECUTE '
            ALTER TABLE IF EXISTS team_members
            ADD CONSTRAINT fk_team_members_team_id FOREIGN KEY (team_id) REFERENCES team(id) ON DELETE CASCADE';
    EXCEPTION WHEN OTHERS THEN NULL; END;

    -- webhook_delivery -> cascade when webhook removed
    BEGIN
        EXECUTE '
            ALTER TABLE IF EXISTS webhook_delivery
            ADD CONSTRAINT fk_webhook_delivery_webhook_id FOREIGN KEY (webhook_id) REFERENCES webhook(id) ON DELETE CASCADE';
    EXCEPTION WHEN OTHERS THEN NULL; END;

    -- recipient -> cascade when campaign removed (remove recipient rows)
    BEGIN
        EXECUTE '
            ALTER TABLE IF EXISTS recipient
            ADD CONSTRAINT fk_recipient_campaign_id FOREIGN KEY (campaign_id) REFERENCES campaign(id) ON DELETE CASCADE';
    EXCEPTION WHEN OTHERS THEN NULL; END;

    -- daily_stats -> cascade when campaign removed
    BEGIN
        EXECUTE '
            ALTER TABLE IF EXISTS daily_stats
            ADD CONSTRAINT fk_daily_stats_campaign_id FOREIGN KEY (campaign_id) REFERENCES campaign(id) ON DELETE CASCADE';
    EXCEPTION WHEN OTHERS THEN NULL; END;

    -- sequence_recipient -> cascade when sequence removed
    BEGIN
        EXECUTE '
            ALTER TABLE IF EXISTS sequence_recipient
            ADD CONSTRAINT fk_sequence_recipient_sequence_id FOREIGN KEY (sequence_id) REFERENCES sequence(id) ON DELETE CASCADE';
    EXCEPTION WHEN OTHERS THEN NULL; END;

    -- campaign.template_id -> set NULL when email_template removed (preserve campaign)
    BEGIN
        EXECUTE '
            ALTER TABLE IF EXISTS campaign
            ADD CONSTRAINT fk_campaign_template_id FOREIGN KEY (template_id) REFERENCES email_template(id) ON DELETE SET NULL';
    EXCEPTION WHEN OTHERS THEN NULL; END;

    -- campaign.smtp_profile_id -> set NULL when smtp_server removed
    BEGIN
        EXECUTE '
            ALTER TABLE IF EXISTS campaign
            ADD CONSTRAINT fk_campaign_smtp_profile_id FOREIGN KEY (smtp_profile_id) REFERENCES smtp_server(id) ON DELETE SET NULL';
    EXCEPTION WHEN OTHERS THEN NULL; END;

    -- recipient.smtp_profile_used_id -> set NULL when smtp_server removed
    BEGIN
        EXECUTE '
            ALTER TABLE IF EXISTS recipient
            ADD CONSTRAINT fk_recipient_smtp_profile_used_id FOREIGN KEY (smtp_profile_used_id) REFERENCES smtp_server(id) ON DELETE SET NULL';
    EXCEPTION WHEN OTHERS THEN NULL; END;

    -- smtp_server.user_id -> set NULL when user removed
    BEGIN
        EXECUTE '
            ALTER TABLE IF EXISTS smtp_server
            ADD CONSTRAINT fk_smtp_server_user_id FOREIGN KEY (user_id) REFERENCES "user"(id) ON DELETE SET NULL';
    EXCEPTION WHEN OTHERS THEN NULL; END;

    -- email_template.user_id -> set NULL
    BEGIN
        EXECUTE '
            ALTER TABLE IF EXISTS email_template
            ADD CONSTRAINT fk_email_template_user_id FOREIGN KEY (user_id) REFERENCES "user"(id) ON DELETE SET NULL';
    EXCEPTION WHEN OTHERS THEN NULL; END;

    -- suppression.user_id -> set NULL
    BEGIN
        EXECUTE '
            ALTER TABLE IF EXISTS suppression
            ADD CONSTRAINT fk_suppression_user_id FOREIGN KEY (user_id) REFERENCES "user"(id) ON DELETE SET NULL';
    EXCEPTION WHEN OTHERS THEN NULL; END;

    -- notification.user_id -> cascade (notifications disappear when user deleted)
    BEGIN
        EXECUTE '
            ALTER TABLE IF EXISTS notification
            ADD CONSTRAINT fk_notification_user_id FOREIGN KEY (user_id) REFERENCES "user"(id) ON DELETE CASCADE';
    EXCEPTION WHEN OTHERS THEN NULL; END;

    -- api_key.user_id -> set NULL
    BEGIN
        EXECUTE '
            ALTER TABLE IF EXISTS api_key
            ADD CONSTRAINT fk_api_key_user_id FOREIGN KEY (user_id) REFERENCES "user"(id) ON DELETE SET NULL';
    EXCEPTION WHEN OTHERS THEN NULL; END;

    -- tag.user_id -> set NULL
    BEGIN
        EXECUTE '
            ALTER TABLE IF EXISTS tag
            ADD CONSTRAINT fk_tag_user_id FOREIGN KEY (user_id) REFERENCES "user"(id) ON DELETE SET NULL';
    EXCEPTION WHEN OTHERS THEN NULL; END;

    -- segment.user_id -> set NULL
    BEGIN
        EXECUTE '
            ALTER TABLE IF EXISTS segment
            ADD CONSTRAINT fk_segment_user_id FOREIGN KEY (user_id) REFERENCES "user"(id) ON DELETE SET NULL';
    EXCEPTION WHEN OTHERS THEN NULL; END;

    -- sequence.user_id -> set NULL
    BEGIN
        EXECUTE '
            ALTER TABLE IF EXISTS sequence
            ADD CONSTRAINT fk_sequence_user_id FOREIGN KEY (user_id) REFERENCES "user"(id) ON DELETE SET NULL';
    EXCEPTION WHEN OTHERS THEN NULL; END;

    -- team.owner_id -> set NULL if owner removed
    BEGIN
        EXECUTE '
            ALTER TABLE IF EXISTS team
            ADD CONSTRAINT fk_team_owner_id FOREIGN KEY (owner_id) REFERENCES "user"(id) ON DELETE SET NULL';
    EXCEPTION WHEN OTHERS THEN NULL; END;

    -- Additional defensive constraints (if present)
    BEGIN
        EXECUTE '
            ALTER TABLE IF EXISTS webhook
            ADD CONSTRAINT fk_webhook_user_id FOREIGN KEY (user_id) REFERENCES "user"(id) ON DELETE SET NULL';
    EXCEPTION WHEN OTHERS THEN NULL; END;

    BEGIN
        EXECUTE '
            ALTER TABLE IF EXISTS campaign
            ADD CONSTRAINT fk_campaign_approved_by_id FOREIGN KEY (approved_by_id) REFERENCES "user"(id) ON DELETE SET NULL';
    EXCEPTION WHEN OTHERS THEN NULL; END;

    -- finished
    RAISE NOTICE 'FK replacement script executed (best-effort).';
END;
$$ LANGUAGE plpgsql;
