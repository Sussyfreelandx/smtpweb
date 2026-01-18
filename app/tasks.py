import asyncio
from app import celery, db
from app.models import Campaign, Recipient
from core_logic.smtp_handler import SMTPHandler
from datetime import datetime
import json

@celery.task(bind=True)
def send_campaign_task(self, campaign_id):
    """
    Celery task to queue up sending for a whole campaign.
    """
    campaign = db.session.get(Campaign, campaign_id)
    if not campaign:
        return 'Campaign not found'

    # Gather all recipient IDs for this campaign that are still 'Queued'
    recipient_ids = [r.id for r in campaign.recipients.filter_by(status='Queued')]

    for recipient_id in recipient_ids:
        # Launch a separate task for each email to be sent
        send_single_email_task.delay(recipient_id)
    
    return f'Queued {len(recipient_ids)} emails for campaign {campaign_id}.'

@celery.task(bind=True, autoretry_for=(Exception,), retry_kwargs={'max_retries': 3, 'countdown': 60})
def send_single_email_task(self, recipient_id):
    """
    Celery task that sends one email.
    This runs asynchronously in the Celery worker.
    """
    recipient = db.session.get(Recipient, recipient_id)
    if not recipient or recipient.status != 'Queued':
        return f'Recipient {recipient_id} not found or not in Queued state.'

    campaign = recipient.campaign
    
    # Update status to 'Sending' immediately
    recipient.status = 'Sending'
    db.session.commit()

    try:
        # Create SMTP config from the campaign model
        smtp_config = {
            'server': campaign.smtp_server,
            'port': campaign.smtp_port,
            'username': campaign.smtp_username,
            'password': campaign.smtp_password, # Assumes password is saved; handle secrets better in prod
            'sender_name': campaign.smtp_sender_name,
            'sender_email': campaign.smtp_sender_email,
            'use_tls': True, # Can be made configurable in the model
            'use_ssl': False,
        }
        
        smtp_handler = SMTPHandler(smtp_config)
        
        # --- Personalization Logic ---
        # Adapted from the original script's _personalize_content function
        recipient_data = json.loads(recipient.data) if recipient.data else {}
        context = {k.lower(): v for k, v in recipient_data.items()}
        
        # Add dynamic placeholders
        if 'firstname' not in context:
            local_part = recipient.email.split('@')[0]
            context['firstname'] = local_part.split('.')[0].capitalize()

        context['greetings'] = f"Hello {context.get('firstname', '')}"
        # TODO: Add more advanced personalization from the original script as needed
        
        # Render subject and body
        from jinja2 import Template
        subject_template = Template(campaign.subject)
        body_template = Template(campaign.body_html)
        
        personalized_subject = subject_template.render(context)
        personalized_body = body_template.render(context)
        
        # Execute the async send function within the synchronous Celery task
        # This is a key pattern for mixing sync (Celery) and async (aiosmtp)
        loop = asyncio.get_event_loop()
        success, message = loop.run_until_complete(smtp_handler.send_email_async(
            to_email=recipient.email,
            subject=personalized_subject,
            html_content=personalized_body,
            # TODO: Add unsubscribe and tracking URLs from context
        ))

        # Update recipient status based on the result
        if success:
            recipient.status = 'Sent'
            recipient.sent_at = datetime.utcnow()
        else:
            recipient.status = 'Failed'
            recipient.status_message = message

        db.session.commit()
        return f"Processed recipient {recipient_id}: {recipient.status}"

    except Exception as e:
        recipient.status = 'Failed'
        recipient.status_message = str(e)
        db.session.commit()
        # The task will be retried automatically due to the decorator config
        raise self.retry(exc=e)