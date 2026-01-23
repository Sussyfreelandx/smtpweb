-- suppression.user_id
ALTER TABLE IF EXISTS suppression DROP CONSTRAINT IF EXISTS suppression_user_id_fkey;
ALTER TABLE IF EXISTS suppression ADD CONSTRAINT suppression_user_id_fkey FOREIGN KEY (user_id) REFERENCES "user"(id) ON DELETE SET NULL;

-- campaign.approved_by_id and campaign.user_id
ALTER TABLE IF EXISTS campaign DROP CONSTRAINT IF EXISTS campaign_approved_by_id_fkey;
ALTER TABLE IF EXISTS campaign ADD CONSTRAINT campaign_approved_by_id_fkey FOREIGN KEY (approved_by_id) REFERENCES "user"(id) ON DELETE SET NULL;

ALTER TABLE IF EXISTS campaign DROP CONSTRAINT IF EXISTS campaign_user_id_fkey;
ALTER TABLE IF EXISTS campaign ADD CONSTRAINT campaign_user_id_fkey FOREIGN KEY (user_id) REFERENCES "user"(id) ON DELETE SET NULL;

-- webhook.user_id
ALTER TABLE IF EXISTS webhook DROP CONSTRAINT IF EXISTS webhook_user_id_fkey;
ALTER TABLE IF EXISTS webhook ADD CONSTRAINT webhook_user_id_fkey FOREIGN KEY (user_id) REFERENCES "user"(id) ON DELETE SET NULL;

-- team_members.user_id
ALTER TABLE IF EXISTS team_members DROP CONSTRAINT IF EXISTS team_members_user_id_fkey;
ALTER TABLE IF EXISTS team_members ADD CONSTRAINT team_members_user_id_fkey FOREIGN KEY (user_id) REFERENCES "user"(id) ON DELETE CASCADE;

-- tag.user_id
ALTER TABLE IF EXISTS tag DROP CONSTRAINT IF EXISTS tag_user_id_fkey;
ALTER TABLE IF EXISTS tag ADD CONSTRAINT tag_user_id_fkey FOREIGN KEY (user_id) REFERENCES "user"(id) ON DELETE SET NULL;

-- segment.user_id
ALTER TABLE IF EXISTS segment DROP CONSTRAINT IF EXISTS segment_user_id_fkey;
ALTER TABLE IF EXISTS segment ADD CONSTRAINT segment_user_id_fkey FOREIGN KEY (user_id) REFERENCES "user"(id) ON DELETE SET NULL;

-- email_template.user_id
ALTER TABLE IF EXISTS email_template DROP CONSTRAINT IF EXISTS email_template_user_id_fkey;
ALTER TABLE IF EXISTS email_template ADD CONSTRAINT email_template_user_id_fkey FOREIGN KEY (user_id) REFERENCES "user"(id) ON DELETE SET NULL;

-- api_key.user_id
ALTER TABLE IF EXISTS api_key DROP CONSTRAINT IF EXISTS api_key_user_id_fkey;
ALTER TABLE IF EXISTS api_key ADD CONSTRAINT api_key_user_id_fkey FOREIGN KEY (user_id) REFERENCES "user"(id) ON DELETE SET NULL;

-- team.owner_id
ALTER TABLE IF EXISTS team DROP CONSTRAINT IF EXISTS team_owner_id_fkey;
ALTER TABLE IF EXISTS team ADD CONSTRAINT team_owner_id_fkey FOREIGN KEY (owner_id) REFERENCES "user"(id) ON DELETE SET NULL;

-- notification.user_id
ALTER TABLE IF EXISTS notification DROP CONSTRAINT IF EXISTS notification_user_id_fkey;
ALTER TABLE IF EXISTS notification ADD CONSTRAINT notification_user_id_fkey FOREIGN KEY (user_id) REFERENCES "user"(id) ON DELETE CASCADE;

-- sequence.user_id
ALTER TABLE IF EXISTS sequence DROP CONSTRAINT IF EXISTS sequence_user_id_fkey;
ALTER TABLE IF EXISTS sequence ADD CONSTRAINT sequence_user_id_fkey FOREIGN KEY (user_id) REFERENCES "user"(id) ON DELETE SET NULL;

-- smtp_server.user_id
ALTER TABLE IF EXISTS smtp_server DROP CONSTRAINT IF EXISTS smtp_server_user_id_fkey;
ALTER TABLE IF EXISTS smtp_server ADD CONSTRAINT smtp_server_user_id_fkey FOREIGN KEY (user_id) REFERENCES "user"(id) ON DELETE SET NULL;

-- webhook_delivery.webhook_id
ALTER TABLE IF EXISTS webhook_delivery DROP CONSTRAINT IF EXISTS webhook_delivery_webhook_id_fkey;
ALTER TABLE IF EXISTS webhook_delivery ADD CONSTRAINT webhook_delivery_webhook_id_fkey FOREIGN KEY (webhook_id) REFERENCES webhook(id) ON DELETE CASCADE;

-- recipient.campaign_id
ALTER TABLE IF EXISTS recipient DROP CONSTRAINT IF EXISTS recipient_campaign_id_fkey;
ALTER TABLE IF EXISTS recipient ADD CONSTRAINT recipient_campaign_id_fkey FOREIGN KEY (campaign_id) REFERENCES campaign(id) ON DELETE CASCADE;

-- daily_stats.campaign_id
ALTER TABLE IF EXISTS daily_stats DROP CONSTRAINT IF EXISTS daily_stats_campaign_id_fkey;
ALTER TABLE IF EXISTS daily_stats ADD CONSTRAINT daily_stats_campaign_id_fkey FOREIGN KEY (campaign_id) REFERENCES campaign(id) ON DELETE CASCADE;