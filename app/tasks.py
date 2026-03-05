"""
Celery tasks for campaign sending and scheduled jobs.

Tasks now import the unified 'celery' instance from the app package,
ensuring they use the correct configuration and app context.
"""

import time
import logging
import traceback
from datetime import datetime

# Import the celery instance defined in celery_app.py
from celery_app import celery
from app import db
from app.utils import log_activity

# Logger for tasks
logger = logging.getLogger(__name__)


def _safe_import_models():
    """Delay import of models to runtime to avoid circular import issues at module import."""
    from app.models import Campaign, Recipient, SMTPServer
    return Campaign, Recipient, SMTPServer


@celery.task(bind=True, name='app.tasks.send_campaign_task', max_retries=3, default_retry_delay=60)
def send_campaign_task(self, campaign_id):
    """
    Main Celery task to send a campaign.
    This task now correctly runs within the Flask application context provided by
    the custom ContextTask in celery_app.py.
    """
    Campaign, Recipient, SMTPServer = _safe_import_models()
    from app.core_logic.mailer_transport import create_mailer_transport
    from app.core_logic.personalization import PersonalizationEngine

    log_activity("=" * 60)
    log_activity(f"📧 TASK RECEIVED: send_campaign_task")
    log_activity(f"   Campaign ID: {campaign_id}")
    log_activity(f"   Task ID: {getattr(self.request, 'id', 'unknown')}")
    log_activity("=" * 60)

    try:
        campaign = Campaign.query.get(campaign_id)

        if not campaign:
            log_activity(f"Campaign {campaign_id} not found", "ERROR")
            return {"status": "error", "message": "Campaign not found"}

        log_activity(f"Campaign:  '{campaign.name}', Status: {campaign.status}")

        if campaign.status != 'Sending':
            log_activity(f"Campaign not in 'Sending' status. Current: {campaign.status}", "WARNING")
            return {"status": "skipped", "message": f"Status is {campaign.status}"}

        smtp_profile = campaign.smtp_profile
        if not smtp_profile:
            log_activity("No mailer profile assigned", "ERROR")
            campaign.status = 'Failed'
            db.session.commit()
            return {"status": "error", "message": "No mailer profile"}

        log_activity(f"Mailer Profile: {smtp_profile.profile_name}")

        smtp_config = smtp_profile.to_dict()
        mailer = create_mailer_transport(smtp_config)
        valid, validation_msg = mailer.validate_configuration()
        if not valid:
            log_activity(f"Mailer profile invalid: {validation_msg}", "ERROR")
            campaign.status = 'Failed'
            campaign.status_message = validation_msg
            db.session.commit()
            return {"status": "error", "message": validation_msg}

        log_activity("Mailer transport initialized")

        batch_size = campaign.throttle_amount or 20
        delay_seconds = campaign.throttle_delay or 60
        log_activity(f"Config: batch={batch_size}, delay={delay_seconds}s")

        attachments = campaign.get_attachments() or []
        total_sent_in_task = 0
        total_failed_in_task = 0
        batch_num = 0

        while True:
            batch_num += 1
            db.session.expire_all()
            campaign = Campaign.query.get(campaign_id)

            if not campaign or campaign.status != 'Sending':
                log_activity(f"Stopping task. Campaign deleted or status changed to '{campaign.status if campaign else 'DELETED'}'", "WARNING")
                break

            recipients = campaign.recipients.filter_by(status='Queued').limit(batch_size).all()

            if not recipients:
                log_activity("No more queued recipients. Completing campaign.")
                campaign.status = 'Completed'
                campaign.completed_at = datetime.utcnow()
                db.session.commit()
                break

            log_activity(f"--- Batch #{batch_num}: Processing {len(recipients)} recipients ---")

            for recipient in recipients:
                try:
                    recipient.status = 'Sending'
                    recipient.attempts = (recipient.attempts or 0) + 1
                    db.session.commit()

                    personalizer = PersonalizationEngine(campaign, recipient)
                    subject, body_html, body_plain = personalizer.personalize()

                    log_activity(f"Sending to: {recipient.email}")
                    success, error_msg = mailer.send_email(
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
                        campaign.sent_count = (campaign.sent_count or 0) + 1
                        total_sent_in_task += 1
                        log_activity(f"✅ Sent: {recipient.email}")
                    else:
                        recipient.status = 'Failed'
                        recipient.status_message = str(error_msg)[:250] if error_msg else 'Unknown'
                        campaign.failed_count = (campaign.failed_count or 0) + 1
                        total_failed_in_task += 1
                        log_activity(f"❌ Failed: {recipient.email} - {error_msg}", "ERROR")
                    
                    db.session.commit()

                except Exception as e:
                    log_activity(f"Critical error for recipient {getattr(recipient, 'email', 'unknown')}: {e}", "ERROR")
                    db.session.rollback()
                    # Mark recipient as failed if possible
                    try:
                        recipient.status = 'Failed'
                        recipient.status_message = str(e)[:250]
                        campaign.failed_count = (campaign.failed_count or 0) + 1
                        db.session.commit()
                    except Exception:
                        db.session.rollback()
                    total_failed_in_task += 1

            log_activity(f"Batch #{batch_num} done. Sent: {total_sent_in_task}, Failed: {total_failed_in_task}")

            remaining = campaign.recipients.filter_by(status='Queued').count()
            if remaining > 0 and delay_seconds > 0:
                log_activity(f"Waiting {delay_seconds}s. {remaining} recipients remaining.")
                time.sleep(delay_seconds)

        try:
            mailer.disconnect()
        except Exception:
            pass

        log_activity("=" * 60)
        log_activity(f"🏁 CAMPAIGN TASK FINISHED: {campaign.name if campaign else campaign_id}")
        log_activity(f"   Total Sent in this task: {total_sent_in_task}, Total Failed in this task: {total_failed_in_task}")
        log_activity("=" * 60)

        return {"status": "completed", "sent": total_sent_in_task, "failed": total_failed_in_task}

    except Exception as e:
        log_activity(f"💥 CRITICAL TASK ERROR: {e}", "ERROR")
        log_activity(traceback.format_exc(), "ERROR")
        db.session.rollback()
        try:
            campaign = Campaign.query.get(campaign_id)
            if campaign:
                campaign.status = 'Failed'
                campaign.status_message = "A critical error occurred in the sending task."
                db.session.commit()
        except Exception as db_err:
            log_activity(f"Could not even mark campaign as failed: {db_err}", "ERROR")
        
        raise self.retry(exc=e, countdown=60)


@celery.task(bind=True, name='app.tasks.send_single_email_task')
def send_single_email_task(self, recipient_id, campaign_id):
    """Send a single email (used for retrying individual recipients)."""
    Campaign, Recipient, SMTPServer = _safe_import_models()
    from app.core_logic.mailer_transport import create_mailer_transport
    from app.core_logic.personalization import PersonalizationEngine

    log_activity(f"Single email task: recipient={recipient_id}, campaign={campaign_id}")

    try:
        recipient = Recipient.query.get(recipient_id)
        campaign = Campaign.query.get(campaign_id)

        if not recipient or not campaign:
            return {"status": "error", "message": "Not found"}

        smtp_profile = campaign.smtp_profile
        if not smtp_profile:
            return {"status": "error", "message": "No mailer profile"}

        config = smtp_profile.to_dict()
        handler = create_mailer_transport(config)
        valid, validation_msg = handler.validate_configuration()
        if not valid:
            return {"status": "error", "message": validation_msg}

        recipient.status = 'Sending'
        recipient.attempts = (recipient.attempts or 0) + 1
        db.session.commit()

        try:
            engine = PersonalizationEngine(campaign, recipient)
            subject, body_html, body_plain = engine.personalize()
        except Exception:
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
            recipient.status_message = 'OK'
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
        log_activity(f"Single email error: {e}", "ERROR")
        db.session.rollback()
        return {"status": "error", "message": str(e)}


@celery.task(name='app.tasks.process_scheduled_campaigns')
def process_scheduled_campaigns():
    """Check and start scheduled campaigns."""
    Campaign, Recipient, SMTPServer = _safe_import_models()
    log_activity("Running process_scheduled_campaigns")

    try:
        now = datetime.utcnow()
        scheduled = Campaign.query.filter(
            Campaign.status == 'Scheduled',
            Campaign.scheduled_at <= now
        ).all()

        count = 0
        for campaign in scheduled:
            log_activity(f"Starting scheduled:  {campaign.name}")
            campaign.status = 'Sending'
            campaign.started_at = now
            db.session.commit()
            # Enqueue the send_campaign_task
            send_campaign_task.delay(campaign.id)
            count += 1

        return {"processed": count}
    except Exception as e:
        log_activity(f"Error in process_scheduled_campaigns: {e}", "ERROR")
        db.session.rollback()
        return {"status": "error", "message": str(e)}


@celery.task(name='app.tasks.reset_daily_smtp_counts')
def reset_daily_smtp_counts():
    """Reset daily SMTP counts for profiles (used by scheduled job)."""
    Campaign, Recipient, SMTPServer = _safe_import_models()
    log_activity("Running reset_daily_smtp_counts")

    try:
        today = datetime.utcnow().date()
        updated = SMTPServer.query.filter(
            SMTPServer.last_reset_date != today
        ).update(
            {'sent_today': 0, 'last_reset_date': today},
            synchronize_session=False
        )
        db.session.commit()
        return {"reset": updated}
    except Exception as e:
        log_activity(f"Error resetting smtp counts: {e}", "ERROR")
        db.session.rollback()
        return {"status": "error", "message": str(e)}


# Lightweight scheduled tasks / placeholders
@celery.task(name='app.tasks.process_sequence_automation')
def process_sequence_automation():
    return {"status": "ok"}


@celery.task(name='app.tasks.check_imap_replies')
def check_imap_replies():
    return {"status": "checked"}


@celery.task(name='app.tasks.cleanup_old_data')
def cleanup_old_data():
    return {"status": "cleaned"}


@celery.task(name='app.tasks.generate_campaign_report')
def generate_campaign_report(campaign_id, user_email):
    return {"status": "completed"}
