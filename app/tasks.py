import time
from datetime import datetime
from flask import url_for
from celery import shared_task
from app import db, create_app
from app.models import Campaign, Recipient
from app.core_logic.smtp_handler import SMTPHandler
from app.core_logic.personalization import PersonalizationEngine
from app.utils import log_activity

# No need to push app context globally here. Celery handles it.

@shared_task(bind=True)
def send_campaign_task(self, campaign_id):
    app = create_app()
    with app.app_context():
        campaign = Campaign.query.get(campaign_id)
        if not campaign or campaign.status != 'Sending': return

        batch_size = campaign.throttle_amount or 20
        delay_seconds = campaign.throttle_delay or 60
        
        log_activity(f"Starting campaign '{campaign.name}'. Batch: {batch_size}", "INFO")

        while True:
            db.session.refresh(campaign)
            if campaign.status != 'Sending':
                log_activity(f"Campaign '{campaign.name}' paused/stopped.", "WARNING")
                break
                
            recipients = campaign.recipients.filter_by(status='Queued').limit(batch_size).all()
            if not recipients:
                break # No more queued recipients
                
            batch_ids = [r.id for r in recipients]
            
            log_activity(f"Processing batch of {len(batch_ids)} emails...", "INFO")
            
            # Send batch to a worker
            send_batch_task.delay(campaign_id, batch_ids)
            
            # Throttle if there might be more emails
            time.sleep(delay_seconds)

        # Final Status Check
        db.session.refresh(campaign)
        remaining = campaign.recipients.filter_by(status='Queued').count()
        if remaining == 0 and campaign.status == 'Sending':
            campaign.status = 'Completed'
            db.session.commit()
            log_activity(f"Campaign '{campaign.name}' completed.", "SUCCESS")

@shared_task(bind=True)
def send_batch_task(self, campaign_id, recipient_ids):
    app = create_app()
    with app.app_context():
        campaign = Campaign.query.get(campaign_id)
        if not campaign: return

        smtp_profile = campaign.smtp_profile
        if not smtp_profile:
            log_activity("Worker failed: No SMTP profile.", "ERROR")
            return

        config = smtp_profile.to_dict()
        if not config.get('password'):
            log_activity(f"Worker failed for profile {smtp_profile.profile_name}: Password missing or decryption failed.", "ERROR")
            return
            
        smtp_handler = SMTPHandler(config)
        
        connected, error_msg = smtp_handler.connect()
        if not connected:
            log_activity(f"SMTP Connect Error: {error_msg}", "ERROR")
            Recipient.query.filter(Recipient.id.in_(recipient_ids)).update(
                {'status': 'Failed', 'status_message': f'SMTP Connect: {error_msg}'},
                synchronize_session=False
            )
            db.session.commit()
            return

        for rid in recipient_ids:
            recipient = Recipient.query.get(rid)
            if not recipient: continue
            
            try:
                personalizer = PersonalizationEngine(campaign, recipient)
                p_subject, p_body = personalizer.personalize()
                unsub_token = recipient.get_tracking_token('unsubscribe')
                unsub_url = url_for('main.unsubscribe', token=unsub_token, _external=True)

                success, msg = smtp_handler.send_message_existing_conn(
                    recipient.email, p_subject, p_body, unsub_url
                )

                if success:
                    recipient.status = 'Sent'
                    recipient.sent_at = datetime.now()
                else:
                    recipient.status = 'Failed'
                    recipient.status_message = msg
            except Exception as e:
                recipient.status = 'Failed'
                recipient.status_message = str(e)
            
            db.session.commit()

        smtp_handler.quit()
