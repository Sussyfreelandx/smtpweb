"""
Celery tasks for campaign sending and scheduled jobs.

Tasks now import the unified 'celery' instance from the app package,
ensuring they use the correct configuration and app context.
"""

import time
import logging
import traceback
from datetime import datetime, timezone
from email.utils import make_msgid

# Import the celery instance defined in celery_app.py
from celery_app import celery
from app import db
from app.utils import log_activity

# Logger for tasks
logger = logging.getLogger(__name__)
STATUS_MESSAGE_MAX_LENGTH = 255
REPLY_DETECTED_SUFFIX = " | Reply detected"


def _truncate_utf8(text, max_bytes):
    raw = (text or "").encode('utf-8')
    return raw[:max_bytes].decode('utf-8', errors='ignore')


def _apply_seed_spam_pause(campaigns, spam_rate, pause_message):
    paused = 0
    if spam_rate < 50.0:
        return paused
    for campaign in campaigns:
        if campaign.status == 'Sending':
            campaign.status = 'Paused'
            campaign.status_message = pause_message
            paused += 1
    return paused


def _check_profile_send_allowed(profile):
    if not profile:
        return False, "No mailer profile configured"
    if profile.can_send():
        return True, None

    if getattr(profile, 'warmup_enabled', False):
        limit = profile.get_warmup_limit()
        return False, f"Warmup limit reached ({profile.sent_today}/{limit})"
    if getattr(profile, 'daily_limit', 0) and profile.sent_today >= profile.daily_limit:
        return False, f"Daily limit reached ({profile.sent_today}/{profile.daily_limit})"
    if getattr(profile, 'hourly_limit', 0) and profile.sent_this_hour >= profile.hourly_limit:
        return False, f"Hourly limit reached ({profile.sent_this_hour}/{profile.hourly_limit})"
    return False, "Profile sending limits reached"


def _record_profile_send_success(profile):
    if profile:
        profile.increment_sent_count()


def _build_outbound_message_id(profile, recipient_email):
    sender = getattr(profile, 'sender_email', None) or recipient_email or 'local@localhost'
    domain = sender.split('@')[-1] if sender and '@' in sender else 'localhost'
    return make_msgid(domain=domain)


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

            allowed, deny_msg = _check_profile_send_allowed(smtp_profile)
            if not allowed:
                limit_reached_message = deny_msg or "Profile sending limit reached"
                campaign.status = 'Paused'
                campaign.status_message = f"Auto-paused: {limit_reached_message}"
                db.session.commit()
                log_activity(f"⏸️ Campaign paused: {limit_reached_message}", "WARNING")
                break

            for recipient in recipients:
                try:
                    recipient.status = 'Sending'
                    recipient.attempts = (recipient.attempts or 0) + 1
                    outbound_message_id = _build_outbound_message_id(smtp_profile, recipient.email)
                    recipient.message_id = outbound_message_id
                    db.session.commit()

                    personalizer = PersonalizationEngine(campaign, recipient)
                    subject, body_html, body_plain = personalizer.personalize()

                    log_activity(f"Sending to: {recipient.email}")
                    success, error_msg = mailer.send_email(
                        to_email=recipient.email,
                        subject=subject,
                        html_content=body_html,
                        plain_content=body_plain,
                        attachments=attachments,
                        custom_headers={'Message-ID': outbound_message_id},
                    )

                    if success:
                        recipient.status = 'Sent'
                        recipient.sent_at = datetime.utcnow()
                        recipient.status_message = 'OK'
                        _record_profile_send_success(smtp_profile)
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

        allowed, deny_msg = _check_profile_send_allowed(smtp_profile)
        if not allowed:
            return {"status": "error", "message": deny_msg}

        recipient.status = 'Sending'
        recipient.attempts = (recipient.attempts or 0) + 1
        outbound_message_id = _build_outbound_message_id(smtp_profile, recipient.email)
        recipient.message_id = outbound_message_id
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
            plain_content=body_plain,
            custom_headers={'Message-ID': outbound_message_id},
        )

        if success:
            recipient.status = 'Sent'
            recipient.sent_at = datetime.utcnow()
            recipient.status_message = 'OK'
            _record_profile_send_success(smtp_profile)
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
    Campaign, Recipient, SMTPServer = _safe_import_models()
    from app.core_logic.desktop_imap_compat import fetch_recent_imap_reply_signals

    log_activity("Running check_imap_replies")
    checked_profiles = 0
    updated_replies = 0

    try:
        profiles = SMTPServer.query.filter(
            SMTPServer.is_active,
            SMTPServer.imap_server.isnot(None),
            SMTPServer.imap_username.isnot(None),
        ).all()

        for profile in profiles:
            imap_password = profile.get_imap_password()
            if not profile.imap_server or not profile.imap_username or not imap_password:
                continue

            checked_profiles += 1
            try:
                signals = fetch_recent_imap_reply_signals(
                    host=profile.imap_server,
                    port=int(profile.imap_port or 993),
                    username=profile.imap_username,
                    password=imap_password,
                    limit=300,
                )
            except Exception as e:
                log_activity(f"IMAP check failed for profile {profile.id}: {e}", "WARNING")
                continue

            sender_emails = signals.get('sender_emails', set())
            reply_message_ids = signals.get('reply_message_ids', set())
            if not sender_emails and not reply_message_ids:
                continue
            normalized_sender_emails = {addr.strip().lower() for addr in sender_emails if addr}

            recipients = Recipient.query.join(Campaign, Recipient.campaign_id == Campaign.id).filter(
                Campaign.smtp_profile_id == profile.id,
                Recipient.status == 'Sent',
                Recipient.replied_at.is_(None),
            ).all()

            for recipient in recipients:
                recipient_email_norm = (recipient.email or '').strip().lower()
                msg_id = (recipient.message_id or '').strip()
                matched_thread = bool(msg_id and msg_id in reply_message_ids)
                if recipient_email_norm in normalized_sender_emails or matched_thread:
                    recipient.replied_at = datetime.now(timezone.utc)
                    suffix = REPLY_DETECTED_SUFFIX
                    base_msg = recipient.status_message or 'OK'
                    suffix_len_bytes = len(suffix.encode('utf-8'))
                    max_base_len = max(0, STATUS_MESSAGE_MAX_LENGTH - suffix_len_bytes)
                    recipient.status_message = _truncate_utf8(base_msg, max_base_len) + suffix
                    try:
                        recipient.calculate_engagement_score()
                    except Exception as score_err:
                        log_activity(f"Engagement score update failed for recipient {recipient.id}: {score_err}", "WARNING")
                    updated_replies += 1

        db.session.commit()
        return {"status": "checked", "profiles": checked_profiles, "replies_marked": updated_replies}
    except Exception as e:
        db.session.rollback()
        log_activity(f"Error in check_imap_replies: {e}", "ERROR")
        return {"status": "error", "message": str(e)}


@celery.task(name='app.tasks.cleanup_old_data')
def cleanup_old_data():
    return {"status": "cleaned"}


@celery.task(name='app.tasks.verify_seed_inbox_placement')
def verify_seed_inbox_placement(profile_id, seed_list_text):
    """Verify seed inbox placement via IMAP and auto-pause campaigns on high spam rate."""
    Campaign, Recipient, SMTPServer = _safe_import_models()
    from app.core_logic.desktop_seed_inbox_compat import parse_seed_emails, check_seed_inbox_placement_imap

    profile = SMTPServer.query.get(profile_id)
    if not profile:
        return {"status": "error", "message": "Profile not found"}

    seeds = parse_seed_emails(seed_list_text)
    if not seeds:
        return {"status": "error", "message": "No valid seed emails provided"}

    imap_password = profile.get_imap_password()
    if not imap_password:
        return {"status": "error", "message": "IMAP password is not configured for profile"}
    result = check_seed_inbox_placement_imap(
        host=profile.imap_server,
        port=int(profile.imap_port or 993),
        username=profile.imap_username,
        password=imap_password,
        seed_emails=seeds,
    )

    if not result.get("ok"):
        return {"status": "error", "message": result.get("error", "Seed check failed")}

    spam_rate = float(result.get("spam_rate", 0.0))
    campaigns = Campaign.query.filter(Campaign.smtp_profile_id == profile.id).all()
    paused_campaigns = _apply_seed_spam_pause(
        campaigns,
        spam_rate=spam_rate,
        pause_message="Auto-paused: high seed spam rate detected",
    )
    if paused_campaigns:
        db.session.commit()

    return {
        "status": "ok",
        "profile_id": profile.id,
        "spam_rate": spam_rate,
        "paused_campaigns": paused_campaigns,
        "result": result,
    }


@celery.task(name='app.tasks.generate_campaign_report')
def generate_campaign_report(campaign_id, user_email):
    return {"status": "completed"}
