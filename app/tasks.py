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

from app import celery, db  # celery is the Celery instance configured by create_app
from app.utils import log_activity

# logger = logging.getLogger(__name__) # Removed unused logger


def _safe_import_models():
    """Delay import of models to runtime to avoid circular import issues at module import."""
    from app.models import Campaign, Recipient, SMTPServer
    return Campaign, Recipient, SMTPServer


@celery.task(bind=True, name='app.tasks.send_campaign_task', max_retries=3, default_retry_delay=60)
def send_campaign_task(self, campaign_id):
    """
    Main Celery task to send a campaign.

    This task assumes that the Celery app has been configured with a Task base
    that pushes a Flask application context (see app.make_celery / create_app).
    """
    Campaign, Recipient, SMTPServer = _safe_import_models()
    from app.core_logic.smtp_handler import SMTPHandler
    from app.core_logic.personalization import PersonalizationEngine

    log_activity("=" * 60)
    log_activity(f"📧 TASK RECEIVED: send_campaign_task")
    log_activity(f"   Campaign ID: {campaign_id}")
    log_activity(f"   Task ID: {getattr(self.request, 'id', 'unknown')}")
    log_activity("=" * 60)

    try:
        # Load campaign
        campaign = Campaign.query.get(campaign_id)

        if not campaign:
            log_activity(f"Campaign {campaign_id} not found", "ERROR")
            return {"status": "error", "message": "Campaign not found"}

        log_activity(f"Campaign:  '{campaign.name}', Status: {campaign.status}")

        if campaign.status != 'Sending':
            log_activity(f"Campaign not in 'Sending' status.  Current:  {campaign.status}", "WARNING")
            return {"status": "skipped", "message": f"Status is {campaign.status}"}

        smtp_profile = campaign.smtp_profile
        if not smtp_profile:
            log_activity("No SMTP profile assigned", "ERROR")
            campaign.status = 'Failed'
            db.session.commit()
            return {"status": "error", "message": "No SMTP profile"}

        log_activity(f"SMTP Profile: {smtp_profile.profile_name}")

        smtp_config = smtp_profile.to_dict()
        if not smtp_config.get('password'):
            log_activity("SMTP password not configured", "ERROR")
            campaign.status = 'Failed'
            db.session.commit()
            return {"status": "error", "message": "SMTP password missing"}

        # Initialize SMTP handler
        try:
            smtp_handler = SMTPHandler(smtp_config)
            log_activity("SMTP Handler initialized")
        except Exception as e:
            log_activity(f"SMTP Handler init failed: {e}", "ERROR")
            campaign.status = 'Failed'
            db.session.commit()
            return {"status": "error", "message": str(e)}

        batch_size = campaign.throttle_amount or 20
        delay_seconds = campaign.throttle_delay or 60

        log_activity(f"Config: batch={batch_size}, delay={delay_seconds}s")

        attachments = []
        try:
            attachments = campaign.get_attachments() or []
        except Exception:
            attachments = []

        total_sent = 0
        total_failed = 0
        batch_num = 0

        while True:
            batch_num += 1

            # Refresh campaign from DB to pick up status changes
            db.session.expire_all()
            campaign = Campaign.query.get(campaign_id)

            if not campaign:
                log_activity("Campaign deleted during send", "ERROR")
                break

            if campaign.status != 'Sending':
                log_activity(f"Status changed to '{campaign.status}'.  Stopping.", "WARNING")
                break

            # Fetch queued recipients for this batch
            recipients = campaign.recipients.filter_by(status='Queued').limit(batch_size).all()

            if not recipients:
                log_activity("No more queued recipients.  Completing campaign.")
                campaign.status = 'Completed'
                campaign.completed_at = datetime.utcnow()
                campaign.sent_count = total_sent
                campaign.failed_count = total_failed
                db.session.commit()
                break

            log_activity(f"--- Batch #{batch_num}:  {len(recipients)} recipients ---")

            for recipient in recipients:
                try:
                    # Mark as sending
                    recipient.status = 'Sending'
                    recipient.attempts = (recipient.attempts or 0) + 1
                    db.session.commit()

                    # Personalize content
                    try:
                        personalizer = PersonalizationEngine(campaign, recipient)
                        subject, body_html, body_plain = personalizer.personalize()
                    except Exception as pe:
                        log_activity(f"Personalization error: {pe}", "WARNING")
                        subject = campaign.subject
                        body_html = campaign.body_html
                        body_plain = campaign.body_plain or ""

                    # Send email
                    log_activity(f"Sending to:  {recipient.email}")

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
                        total_sent += 1
                        log_activity(f"✅ Sent:  {recipient.email}")
                    else:
                        recipient.status = 'Failed'
                        recipient.status_message = str(error_msg)[:250] if error_msg else 'Unknown'
                        total_failed += 1
                        log_activity(f"❌ Failed: {recipient.email} - {error_msg}", "ERROR")

                    db.session.commit()

                except Exception as e:
                    log_activity(f"Exception for {getattr(recipient, 'email', 'unknown')}: {e}", "ERROR")
                    try:
                        recipient.status = 'Failed'
                        recipient.status_message = str(e)[:250]
                        db.session.commit()
                    except Exception:
                        db.session.rollback()
                    total_failed += 1

            log_activity(f"Batch #{batch_num} done. Sent: {total_sent}, Failed: {total_failed}")

            # Check remaining
            remaining = campaign.recipients.filter_by(status='Queued').count()

            if remaining > 0 and delay_seconds > 0:
                log_activity(f"Waiting {delay_seconds}s.  {remaining} remaining.")
                time.sleep(delay_seconds)

        # Cleanup
        try:
            smtp_handler.disconnect()
        except Exception:
            pass

        log_activity("=" * 60)
        log_activity(f"🏁 CAMPAIGN COMPLETE: {campaign.name if campaign else campaign_id}")
        log_activity(f"   Sent: {total_sent}, Failed: {total_failed}")
        log_activity("=" * 60)

        return {"status": "completed", "sent": total_sent, "failed": total_failed}

    except Exception as e:
        log_activity(f"💥 CRITICAL ERROR: {e}", "ERROR")
        log_activity(traceback.format_exc(), "ERROR")
        try:
            db.session.rollback()
            campaign = None
            try:
                Campaign, Recipient, SMTPServer = _safe_import_models()
                campaign = Campaign.query.get(campaign_id)
            except Exception:
                pass
            if campaign:
                campaign.status = 'Failed'
                db.session.commit()
        except Exception:
            pass
        # Retry via celery built-in retry
        raise self.retry(exc=e, countdown=60)


@celery.task(bind=True, name='app.tasks.send_single_email_task')
def send_single_email_task(self, recipient_id, campaign_id):
    """Send a single email (used for retrying individual recipients)."""
    Campaign, Recipient, SMTPServer = _safe_import_models()
    from app.core_logic.smtp_handler import SMTPHandler
    from app.core_logic.personalization import PersonalizationEngine

    log_activity(f"Single email task: recipient={recipient_id}, campaign={campaign_id}")

    try:
        recipient = Recipient.query.get(recipient_id)
        campaign = Campaign.query.get(campaign_id)

        if not recipient or not campaign:
            return {"status": "error", "message": "Not found"}

        smtp_profile = campaign.smtp_profile
        if not smtp_profile:
            return {"status": "error", "message": "No SMTP profile"}

        config = smtp_profile.to_dict()
        if not config.get('password'):
            return {"status": "error", "message": "Password missing"}

        handler = SMTPHandler(config)

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
