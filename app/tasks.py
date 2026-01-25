"""
Celery tasks for campaign sending and scheduled jobs.

This version uses the Celery instance exported by app (app.celery) and relies on
the Celery Task base class configured in app.create_app() (make_celery) which
wraps task execution in a Flask app context.

Note: Ensure you start celery workers using the configured app.celery instance
(e.g., `python manage.py celery-worker` or `celery -A app.celery worker ...`)
so the application context is available to tasks.
"""

import time
import logging
import traceback
from datetime import datetime, timedelta

from app import celery, db
from app.utils import log_activity

# Use a standard logger for better integration with production logging systems
logger = logging.getLogger(__name__)


def _safe_import_models():
    """Delay import of models to runtime to avoid circular import issues at module import."""
    from app.models import Campaign, Recipient, SMTPServer
    return Campaign, Recipient, SMTPServer


@celery.task(bind=True, name='app.tasks.send_campaign_task', max_retries=3, default_retry_delay=60)
def send_campaign_task(self, campaign_id):
    """
    Main Celery task to send a campaign.

    This task assumes that the Celery app has been configured with a Task base
    that pushes a Flask application context (see celery_worker.py).
    """
    Campaign, Recipient, SMTPServer = _safe_import_models()
    from app.core_logic.smtp_handler import SMTPHandler
    from app.core_logic.personalization import PersonalizationEngine

    # Use standard logger for better log management
    logger.info("=" * 60)
    logger.info(f"📧 TASK RECEIVED: send_campaign_task for Campaign ID: {campaign_id}")
    logger.info(f"   Task ID: {getattr(self.request, 'id', 'unknown')}")
    logger.info("=" * 60)

    try:
        campaign = Campaign.query.get(campaign_id)

        if not campaign:
            logger.error(f"Campaign {campaign_id} not found.")
            return {"status": "error", "message": "Campaign not found"}

        logger.info(f"Processing Campaign: '{campaign.name}', Current Status: {campaign.status}")

        if campaign.status != 'Sending':
            logger.warning(f"Campaign not in 'Sending' status. Current is '{campaign.status}'. Skipping.")
            return {"status": "skipped", "message": f"Status is {campaign.status}"}

        smtp_profile = campaign.smtp_profile
        if not smtp_profile:
            logger.error(f"Campaign {campaign_id} has no SMTP profile assigned.")
            campaign.status = 'Failed'
            campaign.completed_at = datetime.utcnow()
            db.session.commit()
            return {"status": "error", "message": "No SMTP profile assigned"}

        logger.info(f"Using SMTP Profile: {smtp_profile.profile_name}")

        smtp_config = smtp_profile.to_dict()
        if not smtp_config.get('password'):
            logger.error(f"SMTP profile '{smtp_profile.profile_name}' is missing a password.")
            campaign.status = 'Failed'
            campaign.completed_at = datetime.utcnow()
            db.session.commit()
            return {"status": "error", "message": "SMTP password missing"}

        smtp_handler = SMTPHandler(smtp_config)
        logger.info("SMTP Handler initialized successfully.")

        batch_size = campaign.throttle_amount or 20
        delay_seconds = campaign.throttle_delay or 60
        logger.info(f"Configuration: Batch Size={batch_size}, Delay={delay_seconds}s")

        attachments = campaign.get_attachments() or []

        total_sent_in_run = 0
        total_failed_in_run = 0

        while True:
            # Refresh campaign from DB to get the latest status inside the loop
            db.session.refresh(campaign)
            if campaign.status != 'Sending':
                logger.warning(f"Campaign status changed to '{campaign.status}'. Stopping send loop.")
                break

            recipients = Recipient.query.with_for_update().filter_by(campaign_id=campaign.id, status='Queued').limit(batch_size).all()

            if not recipients:
                logger.info("No more 'Queued' recipients found. Finalizing campaign.")
                campaign.status = 'Completed'
                campaign.completed_at = datetime.utcnow()
                db.session.commit()
                break
            
            logger.info(f"--- Processing batch of {len(recipients)} recipients ---")

            for recipient in recipients:
                try:
                    # Mark as sending immediately to prevent other workers from picking it up
                    recipient.status = 'Sending'
                    recipient.attempts = (recipient.attempts or 0) + 1
                    db.session.commit()

                    logger.info(f"Personalizing for: {recipient.email}")
                    personalizer = PersonalizationEngine(campaign, recipient)
                    subject, body_html, body_plain = personalizer.personalize()

                    logger.info(f"Sending to: {recipient.email}")
                    success, error_msg = smtp_handler.send_email(
                        to_email=recipient.email,
                        subject=subject,
                        html_content=body_html,
                        plain_content=body_plain,
                        attachments=attachments
                    )

                    if success:
                        recipient.status = 'Sent'
                        recipient.sent_at = datetime.utcnow()
                        recipient.status_message = 'OK'
                        total_sent_in_run += 1
                        logger.info(f"✅ Sent: {recipient.email}")
                    else:
                        recipient.status = 'Failed'
                        recipient.status_message = str(error_msg)[:250] if error_msg else 'Unknown send error'
                        total_failed_in_run += 1
                        logger.error(f"❌ Failed: {recipient.email} - {error_msg}")

                    db.session.commit()

                except Exception as e:
                    logger.error(f"Unhandled exception for recipient {getattr(recipient, 'email', 'unknown')}: {e}", exc_info=True)
                    db.session.rollback()
                    try:
                        # Try to mark recipient as failed even after rollback
                        recipient.status = 'Failed'
                        recipient.status_message = str(e)[:250]
                        db.session.commit()
                    except Exception as inner_e:
                        logger.error(f"Could not even mark recipient as failed: {inner_e}")
                        db.session.rollback()
                    total_failed_in_run += 1

            # After batch, check if more recipients are queued
            remaining = Recipient.query.filter_by(campaign_id=campaign.id, status='Queued').count()
            if remaining > 0 and delay_seconds > 0:
                logger.info(f"Batch complete. Waiting for {delay_seconds} seconds. {remaining} recipients remaining.")
                time.sleep(delay_seconds)
            else:
                logger.info("Batch complete. Checking for more recipients immediately.")

        # Final cleanup and logging
        try:
            smtp_handler.disconnect()
        except Exception as e:
            logger.warning(f"Error during SMTP disconnect: {e}")

        logger.info("=" * 60)
        logger.info(f"🏁 CAMPAIGN FINISHED: {campaign.name}")
        logger.info(f"   Sent in this run: {total_sent_in_run}, Failed in this run: {total_failed_in_run}")
        logger.info("=" * 60)

        return {"status": "completed", "sent": total_sent_in_run, "failed": total_failed_in_run}

    except Exception as e:
        logger.critical(f"💥 CRITICAL ERROR in send_campaign_task for campaign {campaign_id}: {e}", exc_info=True)
        db.session.rollback()
        try:
            # Attempt to mark the campaign as failed on critical error
            campaign = Campaign.query.get(campaign_id)
            if campaign:
                campaign.status = 'Failed'
                campaign.completed_at = datetime.utcnow()
                db.session.commit()
        except Exception as final_e:
            logger.error(f"Could not mark campaign as failed after critical error: {final_e}")
        # Retry the task using Celery's built-in mechanism
        raise self.retry(exc=e, countdown=60)


@celery.task(bind=True, name='app.tasks.send_single_email_task')
def send_single_email_task(self, recipient_id, campaign_id):
    """Send a single email (used for retrying individual recipients)."""
    Campaign, Recipient, SMTPServer = _safe_import_models()
    from app.core_logic.smtp_handler import SMTPHandler
    from app.core_logic.personalization import PersonalizationEngine

    logger.info(f"Received single email task: recipient={recipient_id}, campaign={campaign_id}")

    try:
        recipient = Recipient.query.get(recipient_id)
        campaign = Campaign.query.get(campaign_id)

        if not recipient or not campaign:
            logger.error("Recipient or Campaign not found in single email task.")
            return {"status": "error", "message": "Not found"}

        smtp_profile = campaign.smtp_profile
        if not smtp_profile:
            logger.error("No SMTP profile for single send.")
            return {"status": "error", "message": "No SMTP profile"}

        config = smtp_profile.to_dict()
        if not config.get('password'):
            logger.error("SMTP password missing for single send.")
            return {"status": "error", "message": "Password missing"}

        handler = SMTPHandler(config)

        recipient.status = 'Sending'
        recipient.attempts = (recipient.attempts or 0) + 1
        db.session.commit()

        try:
            engine = PersonalizationEngine(campaign, recipient)
            subject, body_html, body_plain = engine.personalize()
        except Exception as e:
            logger.warning(f"Personalization failed in single send, using defaults. Error: {e}")
            subject = campaign.subject
            body_html = campaign.body_html
            body_plain = campaign.body_plain or ""

        success, message = handler.send_email(
            to_email=recipient.email,
            subject=subject,
            html_content=body_html,
            plain_content=body_plain
        )

        if success:
            recipient.status = 'Sent'
            recipient.sent_at = datetime.utcnow()
            recipient.status_message = 'OK (Retry)'
        else:
            recipient.status = 'Failed'
            recipient.status_message = str(message)[:250]

        db.session.commit()
        try:
            handler.disconnect()
        except Exception:
            pass

        return {"status": "sent" if success else "failed", "message": message}

    except Exception as e:
        logger.error(f"Single email task failed critically: {e}", exc_info=True)
        db.session.rollback()
        return {"status": "error", "message": str(e)}


@celery.task(name='app.tasks.process_scheduled_campaigns')
def process_scheduled_campaigns():
    """Check and start scheduled campaigns."""
    Campaign, Recipient, SMTPServer = _safe_import_models()
    logger.info("Running scheduled campaign check...")

    try:
        now = datetime.utcnow()
        scheduled_campaigns = Campaign.query.filter(
            Campaign.status == 'Scheduled',
            Campaign.scheduled_at <= now
        ).all()

        if not scheduled_campaigns:
            logger.info("No scheduled campaigns to start.")
            return {"processed": 0}

        count = 0
        for campaign in scheduled_campaigns:
            logger.info(f"Starting scheduled campaign: {campaign.name} (ID: {campaign.id})")
            campaign.status = 'Sending'
            campaign.started_at = now
            db.session.commit()
            send_campaign_task.delay(campaign.id)
            count += 1

        logger.info(f"Started {count} scheduled campaigns.")
        return {"processed": count}

    except Exception as e:
        logger.error(f"Error in process_scheduled_campaigns: {e}", exc_info=True)
        db.session.rollback()
        return {"status": "error", "message": str(e)}


@celery.task(name='app.tasks.reset_daily_smtp_counts')
def reset_daily_smtp_counts():
    """Reset daily SMTP counts for profiles (used by scheduled job)."""
    Campaign, Recipient, SMTPServer = _safe_import_models()
    logger.info("Running daily SMTP count reset...")

    try:
        today = datetime.utcnow().date()
        updated_count = SMTPServer.query.filter(
            SMTPServer.last_reset_date != today
        ).update(
            {'sent_today': 0, 'last_reset_date': today},
            synchronize_session=False
        )
        db.session.commit()
        logger.info(f"Reset daily counts for {updated_count} SMTP profiles.")
        return {"reset_count": updated_count}

    except Exception as e:
        logger.error(f"Error resetting SMTP counts: {e}", exc_info=True)
        db.session.rollback()
        return {"status": "error", "message": str(e)}


# Lightweight scheduled tasks / placeholders
@celery.task(name='app.tasks.process_sequence_automation')
def process_sequence_automation():
    logger.info("Running sequence automation task (placeholder)...")
    return {"status": "ok"}


@celery.task(name='app.tasks.check_imap_replies')
def check_imap_replies():
    logger.info("Running IMAP reply check task (placeholder)...")
    return {"status": "checked"}


@celery.task(name='app.tasks.cleanup_old_data')
def cleanup_old_data():
    logger.info("Running old data cleanup task (placeholder)...")
    return {"status": "cleaned"}


@celery.task(name='app.tasks.generate_campaign_report')
def generate_campaign_report(campaign_id, user_email):
    logger.info("Running campaign report generation task (placeholder)...")
    return {"status": "completed"}
