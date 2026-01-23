BEGIN;

-- Allow dropping campaign by cascading child daily_stats/recipient where appropriate
ALTER TABLE IF EXISTS recipient DROP CONSTRAINT IF EXISTS recipient_campaign_id_fkey;
ALTER TABLE IF EXISTS recipient ADD CONSTRAINT recipient_campaign_id_fkey FOREIGN KEY (campaign_id) REFERENCES campaign(id) ON DELETE CASCADE;

ALTER TABLE IF EXISTS daily_stats DROP CONSTRAINT IF EXISTS daily_stats_campaign_id_fkey;
ALTER TABLE IF EXISTS daily_stats ADD CONSTRAINT daily_stats_campaign_id_fkey FOREIGN KEY (campaign_id) REFERENCES campaign(id) ON DELETE CASCADE;

-- Campaign -> template (set template_id to NULL if template removed)
ALTER TABLE IF EXISTS campaign DROP CONSTRAINT IF EXISTS campaign_template_id_fkey;
ALTER TABLE IF EXISTS campaign ADD CONSTRAINT campaign_template_id_fkey FOREIGN KEY (template_id) REFERENCES email_template(id) ON DELETE SET NULL;

-- Campaign -> smtp profile (set NULL)
ALTER TABLE IF EXISTS campaign DROP CONSTRAINT IF EXISTS campaign_smtp_profile_id_fkey;
ALTER TABLE IF EXISTS campaign ADD CONSTRAINT campaign_smtp_profile_id_fkey FOREIGN KEY (smtp_profile_id) REFERENCES smtp_server(id) ON DELETE SET NULL;

-- Recipient -> smtp_profile_used (set NULL)
ALTER TABLE IF EXISTS recipient DROP CONSTRAINT IF EXISTS recipient_smtp_profile_used_id_fkey;
ALTER TABLE IF EXISTS recipient ADD CONSTRAINT recipient_smtp_profile_used_id_fkey FOREIGN KEY (smtp_profile_used_id) REFERENCES smtp_server(id) ON DELETE SET NULL;

-- Webhook deliveries cascade
ALTER TABLE IF EXISTS webhook_delivery DROP CONSTRAINT IF EXISTS webhook_delivery_webhook_id_fkey;
ALTER TABLE IF EXISTS webhook_delivery ADD CONSTRAINT webhook_delivery_webhook_id_fkey FOREIGN KEY (webhook_id) REFERENCES webhook(id) ON DELETE CASCADE;

-- User-related references: safer SET NULL (prevents huge cascade on user delete)
ALTER TABLE IF EXISTS suppression DROP CONSTRAINT IF EXISTS suppression_user_id_fkey;
ALTER TABLE IF EXISTS suppression ADD CONSTRAINT suppression_user_id_fkey FOREIGN KEY (user_id) REFERENCES "user"(id) ON DELETE SET NULL;

ALTER TABLE IF EXISTS smtp_server DROP CONSTRAINT IF EXISTS smtp_server_user_id_fkey;
ALTER TABLE IF EXISTS smtp_server ADD CONSTRAINT smtp_server_user_id_fkey FOREIGN KEY (user_id) REFERENCES "user"(id) ON DELETE SET NULL;

ALTER TABLE IF EXISTS campaign DROP CONSTRAINT IF EXISTS campaign_user_id_fkey;
ALTER TABLE IF EXISTS campaign ADD CONSTRAINT campaign_user_id_fkey FOREIGN KEY (user_id) REFERENCES "user"(id) ON DELETE SET NULL;

ALTER TABLE IF EXISTS campaign DROP CONSTRAINT IF EXISTS campaign_approved_by_id_fkey;
ALTER TABLE IF EXISTS campaign ADD CONSTRAINT campaign_approved_by_id_fkey FOREIGN KEY (approved_by_id) REFERENCES "user"(id) ON DELETE SET NULL;

-- Notifications cascade with their user
ALTER TABLE IF EXISTS notification DROP CONSTRAINT IF EXISTS notification_user_id_fkey;
ALTER TABLE IF EXISTS notification ADD CONSTRAINT notification_user_id_fkey FOREIGN KEY (user_id) REFERENCES "user"(id) ON DELETE CASCADE;

-- Team members should remove entries if a user is deleted
ALTER TABLE IF EXISTS team_members DROP CONSTRAINT IF EXISTS team_members_user_id_fkey;
ALTER TABLE IF EXISTS team_members ADD CONSTRAINT team_members_user_id_fkey FOREIGN KEY (user_id) REFERENCES "user"(id) ON DELETE CASCADE;

-- Other user refs set null
ALTER TABLE IF EXISTS tag DROP CONSTRAINT IF EXISTS tag_user_id_fkey;
ALTER TABLE IF EXISTS tag ADD CONSTRAINT tag_user_id_fkey FOREIGN KEY (user_id) REFERENCES "user"(id) ON DELETE SET NULL;

ALTER TABLE IF EXISTS email_template DROP CONSTRAINT IF EXISTS email_template_user_id_fkey;
ALTER TABLE IF EXISTS email_template ADD CONSTRAINT email_template_user_id_fkey FOREIGN KEY (user_id) REFERENCES "user"(id) ON DELETE SET NULL;

COMMIT;
