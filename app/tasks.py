import time
import logging
import traceback
from datetime import datetime
from app import db, celery
from celery import group

logger = logging.getLogger(__name__)

def log_task(message, level="INFO"):
    """Log task activity with timestamp."""
    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] TASK-{level}: {message}")
    if level == "ERROR":
        logger.error(message)
    else:
        logger.info(message)


@celery.task(bind=True, name='app.tasks.send_campaign_task', max_retries=3, default_retry_delay=60)
def send_campaign_task(self, campaign_id):
    """
    High-throughput campaign task dispatcher.
    This task's only job is to get all recipient IDs and create individual
    send tasks for each one, allowing Celery to distribute the load efficiently.
    """
    from app.models import Campaign, Recipient
    
    log_task("=" * 60)
    log_task(f"🚀 TASK LAUNCHER: send_campaign_task")
    log_task(f"   Campaign ID: {campaign_id}")
    log_task(f"   Task ID: {self.request.id}")
    log_task("=" * 60)
    
    try:
        # Load campaign
        campaign = Campaign.query.get(campaign_id)
        if not campaign:
            log_task(f"Campaign {campaign_id} not found", "ERROR")
            return {"status": "error", "message": "Campaign not found"}
        
        log_task(f"Campaign: '{campaign.name}', Status: {campaign.status}")
        
        if campaign.status not in ['Sending', 'Paused', 'Draft', 'Stopped', 'Failed']:
            log_task(f"Campaign in an unsendable state: {campaign.status}", "WARNING")
            return {"status": "skipped", "message": f"Status is {campaign.status}"}

        # If campaign isn't 'Sending', set it to 'Sending'
        if campaign.status != 'Sending':
            campaign.status = 'Sending'
            campaign.started_at = datetime.utcnow()
            db.session.commit()

        # Get all queued recipient IDs for this campaign
        # We use `with_entities(Recipient.id)` for performance, fetching only what's needed.
        recipient_ids = [
            r_id for r_id, in db.session.query(Recipient.id).filter(
                Recipient.campaign_id == campaign_id,
                Recipient.status == 'Queued'
            )
        ]
        
        if not recipient_ids:
            log_task("No queued recipients found. Marking as complete.")
            campaign.status = 'Completed'
            campaign.completed_at = datetime.utcnow()
            db.session.commit()
            return {"status": "completed", "message": "No recipients to send to."}
        
        log_task(f"Found {len(recipient_ids)} queued recipients. Dispatching individual send tasks...")

        # Create a group of individual send tasks
        # This is the "fan-out" part. Celery will now distribute these across all workers.
        job_group = group(
            send_single_email_task.s(recipient_id, campaign.id) for recipient_id in recipient_ids
        )
        
        # Execute the group of tasks
        job_group.apply_async()

        # Update campaign totals
        campaign.total_recipients = campaign.recipients.count()
        db.session.commit()
        
        log_task(f"✅ Successfully dispatched {len(recipient_ids)} email tasks for campaign {campaign_id}.")
        return {"status": "dispatched", "tasks_created": len(recipient_ids)}

    except Exception as e:
        log_task(f"💥 CRITICAL ERROR in dispatcher for campaign {campaign_id}: {e}", "ERROR")
        log_task(traceback.format_exc(), "ERROR")
        try:
            db.session.rollback()
            campaign = Campaign.query.get(campaign_id)
            if campaign:
                campaign.status = 'Failed'
                campaign.status_message = f"Dispatcher Error: {e}"
                db.session.commit()
        except:
            pass
        raise self.retry(exc=e, countdown=60)


@celery.task(bind=True, name='app.tasks.send_single_email_task', max_retries=2, default_retry_delay=120)
def send_single_email_task(self, recipient_id, campaign_id):
    """
    Sends a single email to a recipient. This is the workhorse task.
    """
    from app.models import Campaign, Recipient
    from app.core_logic.smtp_handler import SMTPHandler
    from app.core_logic.personalization import PersonalizationEngine
    
    log_task(f"📨 Processing single email: Recipient ID={recipient_id}, Campaign ID={campaign_id}")
    
    try:
        recipient = Recipient.query.get(recipient_id)
        campaign = Campaign.query.get(campaign_id)
        
        if not recipient or not campaign:
            log_task(f"Recipient {recipient_id} or Campaign {campaign_id} not found.", "ERROR")
            return {"status": "error", "message": "Not found"}
        
        # Prevent re-sending
        if recipient.status not in ['Queued', 'Sending']:
             log_task(f"Recipient {recipient_id} has status '{recipient.status}'. Skipping.", "WARNING")
             return {"status": "skipped", "message": f"Status is {recipient.status}"}

        smtp_profile = campaign.smtp_profile
        if not smtp_profile or not smtp_profile.password_encrypted:
            recipient.status = 'Failed'
            recipient.status_message = "SMTP profile not configured or password missing"
            db.session.commit()
            return {"status": "error", "message": "SMTP profile misconfiguration"}
        
        # Mark as sending
        recipient.status = 'Sending'
        recipient.attempts = (recipient.attempts or 0) + 1
        db.session.commit()
        
        # Personalize content
        engine = PersonalizationEngine(campaign, recipient)
        subject, body_html, body_plain = engine.personalize()
        
        # Send email
        handler = SMTPHandler(smtp_profile.to_dict())
        success, message = handler.send_email(
            to_email=recipient.email,
            subject=subject,
            html_content=body_html,
            plain_content=body_plain
        )
        
        if success:
            recipient.status = 'Sent'
            recipient.sent_at = datetime.utcnow()
            recipient.status_message = 'OK'
            campaign.sent_count = Campaign.sent_count + 1
        else:
            recipient.status = 'Failed'
            recipient.status_message = str(message)[:250]
            campaign.failed_count = Campaign.failed_count + 1
        
        db.session.commit()
        
        # After the last recipient is processed (approximately), check if campaign is done.
        # Note: This is an estimation. A separate periodic task is more robust.
        if (campaign.sent_count + campaign.failed_count) >= campaign.total_recipients:
            if campaign.recipients.filter_by(status='Queued').count() == 0:
                campaign.status = 'Completed'
                campaign.completed_at = datetime.utcnow()
                db.session.commit()
                log_task(f"🏁 Campaign {campaign_id} appears to be complete.")

        return {"status": "sent" if success else "failed", "message": message}
        
    except Exception as e:
        log_task(f"💥 Single email error for recipient {recipient_id}: {e}", "ERROR")
        log_task(traceback.format_exc(), "ERROR")
        try:
            db.session.rollback()
            recipient = Recipient.query.get(recipient_id)
            if recipient:
                recipient.status = 'Failed'
                recipient.status_message = f"Task Error: {e}"[:250]
                db.session.commit()
        except Exception as db_err:
            log_task(f"Could not update recipient status after error: {db_err}", "ERROR")

        # Retry the task with a countdown
        raise self.retry(exc=e, countdown=int(120 * (self.request.retries + 1)))


@celery.task(name='app.tasks.process_scheduled_campaigns')
def process_scheduled_campaigns():
    """Check for and start campaigns that are scheduled to be sent."""
    from app.models import Campaign
    log_task("Running process_scheduled_campaigns")
    
    now = datetime.utcnow()
    scheduled = Campaign.query.filter(
        Campaign.status == 'Scheduled',
        Campaign.scheduled_at <= now
    ).all()
    
    count = 0
    for campaign in scheduled: 
        log_task(f"Starting scheduled campaign: {campaign.name} (ID: {campaign.id})")
        campaign.status = 'Sending'
        campaign.started_at = now
        db.session.commit()
        # Dispatch the campaign launcher task
        send_campaign_task.delay(campaign.id)
        count += 1
    
    return {"processed": count}


@celery.task(name='app.tasks.reset_daily_smtp_counts')
def reset_daily_smtp_counts():
    """Periodically reset the daily send counts for all SMTP servers."""
    from app.models import SMTPServer
    log_task("Running reset_daily_smtp_counts")
    
    today = datetime.utcnow().date()
    updated = SMTPServer.query.filter(
        SMTPServer.last_reset_date != today
    ).update(
        {'sent_today': 0, 'last_reset_date': today},
        synchronize_session=False
    )
    db.session.commit()
    return {"reset_count": updated}

# Placeholder implementations for other tasks to prevent errors
@celery.task(name='app.tasks.process_sequence_automation')
def process_sequence_automation():
    log_task("Sequence automation task placeholder executed.", "INFO")
    return {"status": "ok", "message": "Not implemented yet"}

@celery.task(name='app.tasks.check_imap_replies')
def check_imap_replies():
    log_task("IMAP reply check task placeholder executed.", "INFO")
    return {"status": "checked", "message": "Not implemented yet"}

@celery.task(name='app.tasks.cleanup_old_data')
def cleanup_old_data():
    log_task("Old data cleanup task placeholder executed.", "INFO")
    return {"status": "cleaned", "message": "Not implemented yet"}

@celery.task(name='app.tasks.generate_campaign_report')
def generate_campaign_report(campaign_id, user_email):
    log_task(f"Campaign report generation for {campaign_id} placeholder executed.", "INFO")
    return {"status": "completed", "message": "Not implemented yet"}
