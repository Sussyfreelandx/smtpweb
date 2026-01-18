import json
import time
from datetime import datetime
from flask import url_for
from celery import group
from app import celery, db, create_app
from app.models import Campaign, Recipient
from app.core_logic.smtp_handler import SMTPHandler
from app.core_logic.personalization import PersonalizationEngine

app = create_app()
app.app_context().push()

@celery.task(bind=True)
def send_campaign_task(self, campaign_id):
    """
    Orchestrator task. Determines how to chunk recipients 
    based on parallel_workers and handles throttling loops.
    """
    campaign = Campaign.query.get(campaign_id)
    if not campaign: return "Campaign not found"

    # Set status to Sending
    campaign.status = 'Sending'
    db.session.commit()

    # Get queued recipients
    recipients = campaign.recipients.filter_by(status='Queued').all()
    total_recipients = len(recipients)
    
    if total_recipients == 0:
        campaign.status = 'Completed'
        db.session.commit()
        return "No queued recipients"

    # Configuration
    batch_size = campaign.throttle_amount or 20
    delay_seconds = campaign.throttle_delay or 60
    workers = campaign.parallel_workers or 1

    processed_count = 0

    # Process in batches (Throttling logic)
    # We slice the list of recipients into batches
    for i in range(0, total_recipients, batch_size):
        # 1. Check if user Paused or Stopped the campaign
        db.session.refresh(campaign) # Get latest status
        if campaign.status == 'Paused':
            return "Campaign Paused"
        if campaign.status == 'Stopped':
            return "Campaign Stopped"

        batch = recipients[i : i + batch_size]
        recipient_ids = [r.id for r in batch]

        # 2. Parallel Processing logic (Celery Group)
        # We create a group of tasks to run simultaneously
        job = group(send_single_email_task.s(rid) for rid in recipient_ids)
        result = job.apply_async()
        
        # Wait for this batch to finish before throttling (Synchronous wait for safety)
        # In high-scale systems we might fire-and-forget, but for throttling accuracy we wait.
        # However, to avoid blocking the worker too long, we usually just fire. 
        # But to respect the "Pause" strictly, checking between batches is best.
        
        processed_count += len(batch)

        # 3. Apply Throttle Delay (if not the last batch)
        if i + batch_size < total_recipients:
            time.sleep(delay_seconds)

    campaign.status = 'Completed'
    db.session.commit()
    return f"Processed {processed_count} recipients"

@celery.task(bind=True, autoretry_for=(Exception,), retry_kwargs={'max_retries': 3, 'countdown': 60})
def send_single_email_task(self, recipient_id):
    """
    Worker task. Sends 1 email.
    """
    # Re-establish context inside worker
    with app.app_context():
        recipient = Recipient.query.get(recipient_id)
        if not recipient: return
        
        # Double check status to prevent duplicates
        if recipient.status not in ['Queued', 'Failed']: 
            return

        recipient.status = 'Sending'
        db.session.commit()
        
        try:
            campaign = recipient.campaign
            smtp_profile = campaign.smtp_profile
            
            if not smtp_profile:
                raise Exception("No SMTP Profile linked")

            # Initialize SMTP Handler
            smtp_handler = SMTPHandler(smtp_profile.to_dict())
            
            # Personalize
            personalizer = PersonalizationEngine(campaign, recipient)
            p_subject, p_body = personalizer.personalize()

            # Send (Standard synchronous SMTP)
            success, message = smtp_handler.send_email_sync(
                to_email=recipient.email,
                subject=p_subject,
                html_content=p_body,
                unsubscribe_url=url_for('core_logic.unsubscribe', campaign_id=campaign.id, recipient_id=recipient.id, _external=True)
            )

            if success:
                recipient.status = 'Sent'
                recipient.sent_at = datetime.utcnow()
                recipient.status_message = "OK"
            else:
                recipient.status = 'Failed'
                recipient.status_message = message

        except Exception as e:
            recipient.status = 'Failed'
            recipient.status_message = str(e)
        
        db.session.commit()
