import asyncio
import json
from datetime import datetime
from flask import url_for
from app import celery, db, create_app
from app.models import Campaign, Recipient, SMTPServer
from app.core_logic.smtp_handler import SMTPHandler
from app.core_logic.personalization import PersonalizationEngine

# Create a Flask app context for the celery worker
app = create_app()
app.app_context().push()


@celery.task(bind=True)
def send_campaign_task(self, campaign_id):
    """
    Celery task to send a whole campaign.
    It iterates through recipients and launches a task for each one.
    """
    campaign = Campaign.query.get(campaign_id)
    if not campaign:
        return

    # Gather all recipient IDs for this campaign that are still in 'Queued' state
    recipient_ids = [r.id for r in campaign.recipients.filter_by(status='Queued')]

    for recipient_id in recipient_ids:
        # Launch a separate sub-task for each email to be sent
        send_single_email_task.delay(recipient_id)

@celery.task(bind=True, autoretry_for=(Exception,), retry_kwargs={'max_retries': 3, 'countdown': 60})
def send_single_email_task(self, recipient_id):
    """
    Celery task that sends one email.
    This runs asynchronously and can be retried on failure.
    """
    recipient = Recipient.query.get(recipient_id)
    if not recipient or recipient.status != 'Queued':
        return # Skip if recipient doesn't exist or is not in the queue

    campaign = recipient.campaign
    smtp_profile = campaign.smtp_profile
    
    if not smtp_profile:
        recipient.status = 'Failed'
        recipient.status_message = 'No SMTP Profile associated with campaign.'
        db.session.commit()
        return

    # Update status to 'Sending' in the database
    recipient.status = 'Sending'
    db.session.commit()

    # Create SMTP configuration from the associated profile
    smtp_config = {
        'server': smtp_profile.server,
        'port': smtp_profile.port,
        'username': smtp_profile.username,
        'password': smtp_profile.get_password(), # Decrypts the password
        'sender_name': smtp_profile.sender_name,
        'sender_email': smtp_profile.sender_email,
        'use_tls': smtp_profile.use_tls,
        'use_ssl': smtp_profile.use_ssl,
    }
    
    smtp_handler = SMTPHandler(smtp_config)
    
    # --- Personalization ---
    personalizer = PersonalizationEngine(campaign, recipient)
    p_subject, p_body = personalizer.personalize()

    # --- Sending (Now a direct synchronous call) ---
    success, message = smtp_handler.send_email_sync(
        to_email=recipient.email,
        subject=p_subject,
        html_content=p_body,
        unsubscribe_url=url_for('main.unsubscribe', token=recipient.get_tracking_token('unsubscribe'), _external=True)
    )

    # Update recipient status based on the result
    if success:
        recipient.status = 'Sent'
        recipient.sent_at = datetime.utcnow()
    else:
        recipient.status = 'Failed'
        recipient.status_message = message

    db.session.commit()
