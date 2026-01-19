import json
from datetime import datetime
from flask import url_for
from app import celery, db, create_app
from app.models import Campaign, Recipient
from app.core_logic.smtp_handler import SMTPHandler
from app.core_logic.personalization import PersonalizationEngine

app = create_app()
app.app_context().push()

@celery.task(bind=True)
def send_campaign_task(self, campaign_id):
    """
    Main background task to iterate through recipients.
    Does NOT use aiosmtp. Uses standard synchronous execution via Celery workers.
    """
    campaign = Campaign.query.get(campaign_id)
    if not campaign: return
    
    # Process queued recipients
    recipients = campaign.recipients.filter_by(status='Queued').all()
    for r in recipients:
        send_single_email_task.delay(r.id)

@celery.task(bind=True, autoretry_for=(Exception,), retry_kwargs={'max_retries': 3, 'countdown': 60})
def send_single_email_task(self, recipient_id):
    """
    Sends a single email.
    Handles:
    - Personalization (Autograb, Jinja, Spintax)
    - CSS Inlining
    - A/B Testing selection
    - SMTP Sending (Synchronous)
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

        # Initialize Handlers
        smtp_handler = SMTPHandler(smtp_profile.to_dict())
        personalizer = PersonalizationEngine(campaign, recipient)
        
        # Generate final content (A/B, Autograb, CSS Inline handled here)
        p_subject, p_body = personalizer.personalize()

        # Generate Unsubscribe Link
        unsub_token = recipient.get_tracking_token('unsubscribe')
        unsub_url = url_for('main.unsubscribe', token=unsub_token, _external=True)

        # Send via synchronous SMTP
        success, message = smtp_handler.send_email_sync(
            to_email=recipient.email,
            subject=p_subject,
            html_content=p_body,
            unsubscribe_url=unsub_url
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
