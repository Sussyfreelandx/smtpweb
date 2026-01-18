import asyncio
import json
from datetime import datetime
from flask import url_for
from app import celery, db, create_app
from app.models import Campaign, Recipient
from core_logic.smtp_handler import SMTPHandler
from core_logic.personalize import personalize_content

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
    
    # Update status to 'Sending' in the database
    recipient.status = 'Sending'
    db.session.commit()

    # Create SMTP configuration from the campaign's stored settings
    smtp_config = {
        'server': campaign.smtp_server,
        'port': campaign.smtp_port,
        'username': campaign.smtp_username,
        'password': campaign.smtp_password, # Remember to handle secrets better in production
        'sender_name': campaign.smtp_sender_name,
        'sender_email': campaign.smtp_sender_email,
        'use_tls': True,
        'use_ssl': False,
    }
    
    smtp_handler = SMTPHandler(smtp_config)
    
    # --- Personalization ---
    # Load recipient data and generate tracking/unsubscribe URLs
    recipient_data = json.loads(recipient.data) if recipient.data else {}
    unsubscribe_url = url_for('main.unsubscribe', recipient_id=recipient.id, _external=True)
    tracking_pixel_url = url_for('main.track_open', recipient_id=recipient.id, _external=True)

    # Use the dedicated personalization module
    p_subject, p_body = personalize_content(
        subject=campaign.subject,
        content=campaign.body_html,
        recipient_data=recipient_data,
        sender_name=campaign.smtp_sender_name,
        unsubscribe_url=unsubscribe_url,
        tracking_pixel_url=tracking_pixel_url,
        # The click tracking URL would be injected here as well
    )

    # Use asyncio.run to execute the async sending function within the synchronous Celery worker
    success, message = asyncio.run(smtp_handler.send_email_async(
        to_email=recipient.email,
        subject=p_subject,
        html_content=p_body,
        unsubscribe_url=unsubscribe_url,
    ))

    # Update recipient status based on the result
    if success:
        recipient.status = 'Sent'
        recipient.sent_at = datetime.utcnow()
    else:
        recipient.status = 'Failed'
        recipient.status_message = message

    db.session.commit()
