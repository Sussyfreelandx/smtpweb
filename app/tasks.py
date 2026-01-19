import time
from datetime import datetime
from flask import url_for
from celery import group, shared_task
from app import db, create_app
from app.models import Campaign, Recipient
from app.core_logic.smtp_handler import SMTPHandler
from app.core_logic.personalization import PersonalizationEngine
from app.utils import log_activity

# Initialize App Context for Worker
app = create_app()
app.app_context().push()

@shared_task(bind=True)
def send_campaign_task(self, campaign_id):
    """
    Orchestrator for sending campaigns.
    Handles Throttling and Parallel batching.
    """
    with app.app_context():
        campaign = Campaign.query.get(campaign_id)
        if not campaign: return

        if campaign.status not in ['Sending']:
            return

        batch_size = campaign.throttle_amount or 20
        delay_seconds = campaign.throttle_delay or 60
        
        log_activity(f"Starting campaign task: {campaign.name}. Batch: {batch_size}", "INFO")

        recipients = campaign.recipients.filter_by(status='Queued').all()
        total_recipients = len(recipients)
        
        if total_recipients == 0:
            campaign.status = 'Completed'
            db.session.commit()
            return

        # Process in batches
        for i in range(0, total_recipients, batch_size):
            db.session.refresh(campaign)
            if campaign.status != 'Sending':
                log_activity(f"Campaign {campaign.name} stopped/paused.", "WARNING")
                break

            batch = recipients[i : i + batch_size]
            log_activity(f"Sending batch {i//batch_size + 1}: {len(batch)} emails...", "INFO")
            
            # Use Celery group for parallel execution
            job_group = group(send_single_email_task.s(r.id) for r in batch)
            job_group.apply_async()
            
            # Throttling delay
            if i + batch_size < total_recipients:
                time.sleep(delay_seconds)

        # Final check
        db.session.refresh(campaign)
        if campaign.status == 'Sending':
            remaining = campaign.recipients.filter_by(status='Queued').count()
            if remaining == 0:
                campaign.status = 'Completed'
                db.session.commit()
                log_activity(f"Campaign {campaign.name} completed.", "SUCCESS")

@shared_task(bind=True, autoretry_for=(Exception,), retry_kwargs={'max_retries': 2, 'countdown': 30})
def send_single_email_task(self, recipient_id):
    """
    Worker task: Sends a single email.
    """
    with app.app_context():
        recipient = Recipient.query.get(recipient_id)
        if not recipient or recipient.status != 'Queued': return

        campaign = recipient.campaign
        if campaign.status != 'Sending': return

        recipient.status = 'Sending'
        db.session.commit()
        
        try:
            smtp_profile = campaign.smtp_profile
            if not smtp_profile: raise Exception("No SMTP Profile")

            # Decrypt config freshly
            profile_config = smtp_profile.to_dict()
            smtp_handler = SMTPHandler(profile_config)
            personalizer = PersonalizationEngine(campaign, recipient)
            
            p_subject, p_body = personalizer.personalize()
            unsub_token = recipient.get_tracking_token('unsubscribe')
            unsub_url = url_for('main.unsubscribe', token=unsub_token, _external=True)

            # Sync Send (Robust)
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
                log_activity(f"Failed {recipient.email}: {message}", "ERROR")
                
        except Exception as e:
            recipient.status = 'Failed'
            recipient.status_message = str(e)
            log_activity(f"Worker Error {recipient.email}: {e}", "ERROR")
        
        db.session.commit()
