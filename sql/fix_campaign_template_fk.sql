-- Backup your DB before running this.
BEGIN;

-- Drop existing FK if present, then recreate with ON DELETE SET NULL
ALTER TABLE campaign DROP CONSTRAINT IF EXISTS campaign_template_id_fkey;

ALTER TABLE campaign ADD CONSTRAINT campaign_template_id_fkey
  FOREIGN KEY (template_id) REFERENCES email_template(id) ON DELETE SET NULL;

COMMIT;
