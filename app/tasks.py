import json
import time
from datetime import datetime
from flask import url_for
from celery import group
from app import celery, db, create_app
from app.models import Campaign, Recipient
from app.core_logic.smtp_handler import SMTPHandler
from app.core_logic.personalization import PersonalizationEngineimport time
from datetime import datetime
from flask import url_for
from celery import group, shared_task
from app import db
from app.models import Campaign, Recipient
from app.core_logic.smtp_handler import SMTPHandler
from app.core_logic.personalization import PersonalizationEngine
from app.utils import log_activity

# REMOVED: Global app context push that caused circular import 500 error
# The Celery worker will load the app context automatically via the 'make_celery' factory pattern.

@shared_task(bind=True)
def send_campaign_task(self, campaign_id):
    """
    Orchestrator for sending campaigns.
    Handles Throttling and Parallel batching.
    """
    # We must import Campaign inside the task to ensure db is bound to the worker's app context
    campaign = Campaign.query.get(campaign_id)
    if not campaign: return

    # Check status
    if campaign.status not in ['Sending']:
        return

    # Fetch configuration
    batch_size = campaign.throttle_amount or 20
    delay_seconds = campaign.throttle_delay or 60
    
    # We use log_activity but check if we are in a request context first, 
    # though utils.py uses a global deque so it's safe.
    log_activity(f"Starting campaign task: {campaign.name}. Batch: {batch_size}, Delay: {delay_seconds}s", "INFO")

    # Get Queued Recipients
    recipients = campaign.recipients.filter_by(status='Queued').all()
    total_recipients = len(recipients)
    
    # Process in batches (Throttling)
    for i in range(0, total_recipients, batch_size):
        # Re-check status before every batch (Allows Pausing/Stopping)
        db.session.refresh(campaign)
        if campaign.status != 'Sending':
            log_activity(f"Campaign {campaign.name} status changed to {campaign.status}. Stopping task.", "WARNING")
            break

        batch = recipients[i : i + batch_size]
        
        log_activity(f"Sending batch {i//batch_size + 1}: {len(batch)} recipients...", "INFO")
        
        # Create a group of tasks to run in parallel workers
        job_group = group(send_single_email_task.s(r.id) for r in batch)
        job_group.apply_async()
        
        # Apply Throttling delay if there are more emails to come
        if i + batch_size < total_recipients:
            log_activity(f"Throttling active. Waiting {delay_seconds} seconds...", "INFO")
            time.sleep(delay_seconds)

    # Final check
    db.session.refresh(campaign)
    if campaign.status == 'Sending':
        remaining = campaign.recipients.filter_by(status='Queued').count()
        if remaining == 0:
            campaign.status = 'Completed'
            db.session.commit()
            log_activity(f"Campaign {campaign.name} completed successfully.", "SUCCESS")

@shared_task(bind=True, autoretry_for=(Exception,), retry_kwargs={'max_retries': 2, 'countdown': 30})
def send_single_email_task(self, recipient_id):
    """
    Worker task. Sends a single email using the configuration.
    """
    recipient = Recipient.query.get(recipient_id)
    if not recipient or recipient.status != 'Queued': return

    campaign = recipient.campaign
    
    # Double check pause state inside worker
    if campaign.status != 'Sending':
        return # Skip if paused/stopped

    recipient.status = 'Sending'
    db.session.commit()
    
    try:
        smtp_profile = campaign.smtp_profile
        if not smtp_profile:
            raise Exception("No SMTP Profile linked.")

        # Initialize Handlers
        smtp_handler = SMTPHandler(smtp_profile.to_dict())
        personalizer = PersonalizationEngine(campaign, recipient)
        
        # Generate content
        p_subject, p_body = personalizer.personalize()

        # Unsubscribe Link
        unsub_token = recipient.get_tracking_token('unsubscribe')
        unsub_url = url_for('main.unsubscribe', token=unsub_token, _external=True)

        # Send Sync
        success, message = smtp_handler.send_email_sync(
            to_email=recipient.email,
            subject=p_subject,
            html_content=p_body,
            unsubscribe_url=unsub_url
        )

        if success:
            recipient.status = 'Sent'
            recipient.sent_at = datetime.utcnow()
            recipient.status_message = "OK"
        else:
            recipient.status = 'Failed'
            recipient.status_message = message
            log_activity(f"Failed to send to {recipient.email}: {message}", "ERROR")
            
    except Exception as e:
        recipient.status = 'Failed'
        recipient.status_message = str(e)
        log_activity(f"Worker Exception for {recipient.email}: {e}", "ERROR")
    
    db.session.commit()
from app.utils import log_activity

app = create_app()
app.app_context().push()

@celery.task(bind=True)
def send_campaign_task(self, campaign_id):
    """
    Orchestrator for sending campaigns.
    Handles Throttling and Parallel batching.
    """
    campaign = Campaign.query.get(campaign_id)
    if not campaign: return

    # Check status
    if campaign.status not in ['Sending']:
        return

    # Fetch configuration
    batch_size = campaign.throttle_amount or 20
    delay_seconds = campaign.throttle_delay or 60
    workers = campaign.parallel_workers or 10

    log_activity(f"Starting campaign task: {campaign.name}. Batch: {batch_size}, Delay: {delay_seconds}s", "INFO")

    # Get Queued Recipients
    recipients = campaign.recipients.filter_by(status='Queued').all()
    total_recipients = len(recipients)
    
    # Process in batches (Throttling)
    for i in range(0, total_recipients, batch_size):
        # Re-check status before every batch (Allows Pausing/Stopping)
        db.session.refresh(campaign)
        if campaign.status != 'Sending':
            log_activity(f"Campaign {campaign.name} status changed to {campaign.status}. Stopping task.", "WARNING")
            break

        batch = recipients[i : i + batch_size]
        
        log_activity(f"Sending batch {i//batch_size + 1}: {len(batch)} recipients...", "INFO")
        
        # Create a group of tasks to run in parallel workers
        job_group = group(send_single_email_task.s(r.id) for r in batch)
        result_group = job_group.apply_async()
        
        # Apply Throttling delay if there are more emails to come
        if i + batch_size < total_recipients:
            log_activity(f"Throttling active. Waiting {delay_seconds} seconds...", "INFO")
            time.sleep(delay_seconds)

    # Final check
    db.session.refresh(campaign)
    if campaign.status == 'Sending':
        remaining = campaign.recipients.filter_by(status='Queued').count()
        if remaining == 0:
            campaign.status = 'Completed'
            db.session.commit()
            log_activity(f"Campaign {campaign.name} completed successfully.", "SUCCESS")

@celery.task(bind=True, autoretry_for=(Exception,), retry_kwargs={'max_retries': 2, 'countdown': 30})
def send_single_email_task(self, recipient_id):
    """
    Worker task.
    Sends a single email using the configuration.
    """
    # Create new app context for thread safety in workers
    with app.app_context():
        recipient = Recipient.query.get(recipient_id)
        if not recipient or recipient.status != 'Queued': return

        campaign = recipient.campaign
        
        # Double check pause state inside worker
        if campaign.status != 'Sending':
            return # Skip if paused/stopped

        recipient.status = 'Sending'
        db.session.commit()
        
        try:
            smtp_profile = campaign.smtp_profile
            if not smtp_profile:
                raise Exception("No SMTP Profile linked.")

            # Initialize Handlers
            smtp_handler = SMTPHandler(smtp_profile.to_dict())
            personalizer = PersonalizationEngine(campaign, recipient)
            
            # Generate content
            p_subject, p_body = personalizer.personalize()

            # Unsubscribe Link
            unsub_token = recipient.get_tracking_token('unsubscribe')
            unsub_url = url_for('main.unsubscribe', token=unsub_token, _external=True)

            # Send Sync
            success, message = smtp_handler.send_email_sync(
                to_email=recipient.email,
                subject=p_subject,
                html_content=p_body,
                unsubscribe_url=unsub_url
            )

            if success:
                recipient.status = 'Sent'
                recipient.sent_at = datetime.utcnow()
                recipient.status_message = "OK"
                # log_activity(f"Sent to {recipient.email}", "SUCCESS") # Optional: too noisy for large lists
            else:
                recipient.status = 'Failed'
                recipient.status_message = message
                log_activity(f"Failed to send to {recipient.email}: {message}", "ERROR")
                
        except Exception as e:
            recipient.status = 'Failed'
            recipient.status_message = str(e)
            log_activity(f"Worker Exception for {recipient.email}: {e}", "ERROR")
        
        db.session.commit()

