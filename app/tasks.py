import json
import time
from datetime import datetime
from flask import url_for, current_app
from app import celery, db, create_app
from app.models import Campaign, Recipient
from app.core_logic.smtp_handler import SMTPHandler
from app.core_logic.personalization import PersonalizationEngine

# Initialize context for Celery
app = create_app()
app.app_context().push()

@celery.task(bind=True)
def send_campaign_task(self, campaign_id):
    """
    Orchestrator task.
    This manages the flow, throttling, and dispatching of individual emails.
    """
    campaign = Campaign.query.get(campaign_id)
    if not campaign: return

    # Verify Campaign Status
    if campaign.status != 'Running':
        return # Stop if paused or stopped

    recipients = campaign.recipients.filter_by(status='Queued').all()
    total_sent_in_batch = 0
    throttle_count = campaign.throttle_count or 20
    throttle_delay = campaign.throttle_delay or 1 # Minutes

    for recipient in recipients:
        # Re-check status every iteration to allow immediate Pausing/Stopping
        db.session.refresh(campaign)
        if campaign.status != 'Running':
            break

        # Throttling Logic
        if total_sent_in_batch > 0 and total_sent_in_batch % throttle_count == 0:
            current_app.logger.info(f"Throttling: Pausing for {throttle_delay} minutes...")
            time.sleep(throttle_delay * 60)

        # Dispatch email task
        # We use .apply_async to send to the worker queue
        send_single_email_task.delay(recipient.id)
        total_sent_in_batch += 1

@celery.task(bind=True, autoretry_for=(Exception,), retry_kwargs={'max_retries': 3, 'countdown': 60})
def send_single_email_task(self, recipient_id):
    """
    Worker task.
    Handles the actual SMTP connection and sending for one recipient.
    """
    recipient = Recipient.query.get(recipient_id)
    if not recipient or recipient.status != 'Queued': return

    recipient.status = 'Sending'
    db.session.commit()
    
    try:
        campaign = recipient.campaign
        smtp_profile = campaign.smtp_profile
        if not smtp_profile:
            raise Exception("Campaign is not linked to a valid SMTP Profile.")

        # Initialize SMTP Handler with profile data
        smtp_handler = SMTPHandler(smtp_profile.to_dict())
        
        # Personalize Content (Autograb logic is inside PersonalizationEngine)
        personalizer = PersonalizationEngine(campaign, recipient)
        p_subject, p_body = personalizer.personalize()

        # Send (Using Standard smtplib, NOT aiosmtp)
        success, message = smtp_handler.send_email_sync(
            to_email=recipient.email,
            subject=p_subject,
            html_content=p_body,
            unsubscribe_url=url_for('core_logic.unsubscribe', campaign_id=campaign.id, recipient_id=recipient.id, _external=True)
        )

        if success:
            recipient.status = 'Sent'
            recipient.sent_at = datetime.utcnow()
        else:
            recipient.status = 'Failed'
            recipient.status_message = message
    except Exception as e:
        recipient.status = 'Failed'
        recipient.status_message = str(e)
    
    db.session.commit()
