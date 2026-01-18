import asyncio
from datetime import datetime
from app import db, celery
from app.models import Campaign, Recipient
from app.core_logic.smtp_handler import SMTPHandler
from app.core_logic.personalization import PersonalizationEngine

@celery.task
def send_campaign_task(campaign_id):
    """Celery task to queue sending for a whole campaign."""
    campaign = Campaign.query.get(campaign_id)
    if not campaign:
        return "Campaign not found."

    recipient_ids = [r.id for r in campaign.recipients.filter(Recipient.status.in_(['Queued', 'Failed'])).all()]
    
    for recipient_id in recipient_ids:
        send_single_email_task.delay(recipient_id)
    
    return f"Queued {len(recipient_ids)} emails for campaign '{campaign.name}'."

@celery.task(bind=True, autoretry_for=(Exception,), retry_kwargs={'max_retries': 3, 'countdown': 60})
def send_single_email_task(self, recipient_id):
    """Celery task that sends one email asynchronously."""
    recipient = Recipient.query.get(recipient_id)
    if not recipient:
        return "Recipient not found."
    if recipient.status not in ['Queued', 'Failed']:
        return f"Recipient {recipient.email} status is '{recipient.status}', skipping."

    recipient.status = 'Sending'
    db.session.commit()

    campaign = recipient.campaign
    
    smtp_config = {
        'server': campaign.smtp_server, 'port': campaign.smtp_port,
        'username': campaign.smtp_username, 'password': campaign.smtp_password,
        'sender_name': campaign.smtp_sender_name, 'sender_email': campaign.smtp_sender_email,
        'use_tls': True, 'use_ssl': False, # Can be made configurable in Campaign model
    }
    
    smtp_handler = SMTPHandler(smtp_config)
    personalizer = PersonalizationEngine(campaign, recipient)
    
    final_subject, final_body = personalizer.personalize()
    
    # Use asyncio.run to execute the async function in the synchronous Celery worker
    success, message = asyncio.run(smtp_handler.send_email_async(
        to_email=recipient.email,
        subject=final_subject,
        html_content=final_body,
        unsubscribe_url=url_for('main.unsubscribe', token=recipient.get_tracking_token('unsubscribe'), _external=True)
    ))

    if success:
        recipient.status = 'Sent'
        recipient.sent_at = datetime.utcnow()
    else:
        recipient.status = 'Failed'
        recipient.status_message = message[:250] # Truncate if necessary

    db.session.commit()
    return f"Attempted to send to {recipient.email}: {recipient.status}"
