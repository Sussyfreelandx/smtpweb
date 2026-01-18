import json
from datetime import datetime
from flask import url_for
from app import celery, db, create_app
from app.models import Campaign, Recipient
from app.core_logic.smtp_handler import SMTPHandler
from app.core_logic.personalization import PersonalizationEngine

# Create a Flask app context for the celery worker
app = create_app()
app.app_context().push()

@celery.task(bind=True)
def send_campaign_task(self, campaign_id):
    campaign = Campaign.query.get(campaign_id)
    if not campaign:
        return

    recipient_ids = [r.id for r in campaign.recipients.filter_by(status='Queued')]
    for recipient_id in recipient_ids:
        send_single_email_task.delay(recipient_id)

@celery.task(bind=True, autoretry_for=(Exception,), retry_kwargs={'max_retries': 3, 'countdown': 60})
def send_single_email_task(self, recipient_id):
    recipient = Recipient.query.get(recipient_id)
    if not recipient or recipient.status != 'Queued':
        return

    campaign = recipient.campaign
    recipient.status = 'Sending'
    db.session.commit()

    smtp_profile = campaign.smtp_profile
    if not smtp_profile:
        recipient.status = 'Failed'
        recipient.status_message = "No SMTP Profile assigned to campaign."
        db.session.commit()
        return
        
    smtp_config = smtp_profile.to_dict()
    smtp_handler = SMTPHandler(smtp_config)
    
    # --- Personalization ---
    engine = PersonalizationEngine(campaign=campaign, recipient=recipient)
    p_subject, p_body = engine.personalize()
    
    # --- Sending ---
    # Call the synchronous send_email method directly
    success, message = smtp_handler.send_email(
        to_email=recipient.email,
        subject=p_subject,
        html_content=p_body,
        unsubscribe_url=engine._get_context().get('unsubscribe_link') # Get url from context
    )

    if success:
        recipient.status = 'Sent'
        recipient.sent_at = datetime.utcnow()
    else:
        recipient.status = 'Failed'
        recipient.status_message = message

    db.session.commit()
