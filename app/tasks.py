import time
import logging
from datetime import datetime, timedelta
from celery import shared_task
from app import db, celery

logger = logging.getLogger(__name__)


def log_task(message, level="INFO"):
    """Helper to log task activity"""
    timestamp = datetime.utcnow().strftime("%H:%M:%S")
    print(f"[{timestamp}] TASK {level}: {message}")
    if level == "ERROR":
        logger.error(message)
    else:
        logger.info(message)


@celery.task(bind=True, max_retries=3)
def send_campaign_task(self, campaign_id):
    """Main task to send a campaign."""
    from app import create_app
    from app.models import Campaign, Recipient, SMTPServer, Suppression, DailyStats, HourlyStats
    from app.core_logic.smtp_handler import SMTPHandler, SMTPRotationManager
    from app.core_logic.personalization import PersonalizationEngine
    from flask import url_for
    
    app = create_app()
    
    with app.app_context():
        log_task(f"📧 Starting send_campaign_task for campaign_id={campaign_id}")
        
        try:
            campaign = Campaign.query.get(campaign_id)
            if not campaign:
                log_task(f"Campaign {campaign_id} not found", "ERROR")
                return {"status": "error", "message": "Campaign not found"}
            
            log_task(f"Campaign found: {campaign.name}, Status: {campaign.status}")
            
            if campaign.status != 'Sending':
                log_task(f"Campaign {campaign.name} is not in Sending status (Current: {campaign.status})", "WARNING")
                return {"status": "skipped", "message": "Campaign not in sending status"}
            
            # Get SMTP configuration
            rotation_manager = None
            single_smtp_handler = None
            
            if campaign.smtp_rotation_enabled:
                smtp_profiles_data = get_rotation_smtp_profiles(campaign.user_id)
                if not smtp_profiles_data:
                    _fail_campaign(campaign, "No valid SMTP profiles available for rotation")
                    return {"status": "error", "message": "No SMTP profiles available"}
                rotation_manager = SMTPRotationManager(smtp_profiles_data)
                log_task(f"Using SMTP rotation with {len(smtp_profiles_data)} profiles")
            else:
                smtp_profile = campaign.smtp_profile
                if not smtp_profile:
                    _fail_campaign(campaign, "No SMTP profile assigned")
                    return {"status": "error", "message": "No SMTP profile"}
                
                smtp_config = smtp_profile.to_dict()
                log_task(f"Using SMTP profile: {smtp_profile.profile_name}")
                
                if not smtp_config.get('password'):
                    _fail_campaign(campaign, "SMTP password not configured")
                    return {"status": "error", "message": "SMTP password missing"}
                
                single_smtp_handler = SMTPHandler(smtp_config)

            # Sending configuration
            batch_size = campaign.throttle_amount or 20
            delay_seconds = campaign.throttle_delay or 60
            attachments = campaign.get_attachments() if hasattr(campaign, 'get_attachments') else []
            
            log_task(f"📤 Starting send loop. Batch: {batch_size}, Delay: {delay_seconds}s")
            
            total_sent = 0
            total_failed = 0
            
            # Main sending loop
            while True:
                db.session.expire_all()
                campaign = Campaign.query.get(campaign_id)
                
                if not campaign or campaign.status != 'Sending':
                    log_task(f"Campaign {campaign_id} status changed or not found. Stopping.")
                    break
                
                recipients = campaign.recipients.filter_by(status='Queued').limit(batch_size).all()
                
                if not recipients:
                    log_task(f"No more queued recipients. Completing campaign.")
                    _complete_campaign(campaign, total_sent, total_failed)
                    break
                
                log_task(f"Processing batch of {len(recipients)} recipients")
                
                email_tasks = []
                recipient_map = {}
                
                current_handler = single_smtp_handler
                if rotation_manager:
                    current_handler, err = rotation_manager.get_next_handler()
                    if not current_handler:
                        log_task(f"SMTP rotation exhausted: {err}", "WARNING")
                        campaign.status = 'Paused'
                        db.session.commit()
                        return {"status": "paused", "message": err}

                for recipient in recipients:
                    recipient.status = 'Sending'
                    recipient.attempts += 1
                    recipient_map[recipient.email] = recipient
                    
                    # Personalize content
                    try:
                        personalizer = PersonalizationEngine(campaign, recipient)
                        p_subject, p_body_html, p_body_plain = personalizer.personalize()
                    except Exception as e:
                        log_task(f"Personalization error for {recipient.email}: {e}", "ERROR")
                        p_subject = campaign.subject
                        p_body_html = campaign.body_html
                        p_body_plain = campaign.body_plain or ""
                    
                    # Build unsubscribe URL
                    try:
                        unsubscribe_token = recipient.get_tracking_token('unsubscribe') if hasattr(recipient, 'get_tracking_token') else ""
                        unsubscribe_url = url_for('tracking.unsubscribe', token=unsubscribe_token, _external=True) if unsubscribe_token else "#"
                    except Exception:
                        unsubscribe_url = "#"

                    task = {
                        'to_email': recipient.email,
                        'subject': p_subject,
                        'html_content': p_body_html,
                        'plain_content': p_body_plain,
                        'unsubscribe_url': unsubscribe_url,
                        'attachments': attachments,
                        'custom_headers': {'X-Campaign-ID': str(campaign.id)}
                    }
                    email_tasks.append(task)
                
                db.session.commit()
                
                # Send batch
                log_task(f"Sending batch of {len(email_tasks)} emails...")
                results = current_handler.send_bulk_threaded(email_tasks, max_workers=5)
                
                # Process results
                for res in results:
                    email = res['email']
                    success = res['success']
                    error_msg = res.get('error')
                    
                    recipient = recipient_map.get(email)
                    if not recipient:
                        continue
                    
                    if success:
                        recipient.status = 'Sent'
                        recipient.sent_at = datetime.utcnow()
                        recipient.status_message = "OK"
                        total_sent += 1
                        log_task(f"✅ Sent to {email}")
                    else:
                        recipient.status = 'Failed'
                        recipient.status_message = error_msg[:250] if error_msg else "Unknown error"
                        total_failed += 1
                        log_task(f"❌ Failed to send to {email}: {error_msg}", "ERROR")
                        
                        # Retry on connection errors
                        failure_type = current_handler.classify_failure(error_msg or "")
                        if failure_type == 'connection_error' and recipient.attempts < 3:
                            recipient.status = 'Queued'
                
                db.session.commit()
                log_task(f"Batch complete. Total sent: {total_sent}, failed: {total_failed}")

                # Throttling delay
                db.session.expire_all()
                campaign = Campaign.query.get(campaign_id)
                if campaign and campaign.status == 'Sending':
                    remaining = campaign.recipients.filter_by(status='Queued').count()
                    if remaining > 0 and delay_seconds > 0:
                        log_task(f"Waiting {delay_seconds}s before next batch. {remaining} remaining.")
                        time.sleep(delay_seconds)

            # Cleanup
            if rotation_manager:
                rotation_manager.close_all()
            elif single_smtp_handler:
                single_smtp_handler.disconnect()
            
            log_task(f"🏁 Campaign complete. Sent: {total_sent}, Failed: {total_failed}")
            return {"status": "completed", "sent": total_sent, "failed": total_failed}

        except Exception as e:
            log_task(f"❌ Campaign sending CRITICAL error: {str(e)}", "ERROR")
            logger.exception(f"Campaign {campaign_id} failed with exception")
            try:
                db.session.rollback()
                campaign = Campaign.query.get(campaign_id)
                if campaign:
                    campaign.status = 'Failed'
                    db.session.commit()
            except:
                pass
            raise self.retry(exc=e, countdown=60)


def _fail_campaign(campaign, message):
    """Mark campaign as failed."""
    campaign.status = 'Failed'
    db.session.commit()
    log_task(f"Campaign {campaign.id} failed: {message}", "ERROR")


def _complete_campaign(campaign, sent, failed):
    """Mark campaign as completed."""
    campaign.status = 'Completed'
    campaign.completed_at = datetime.utcnow()
    campaign.sent_count = sent
    campaign.failed_count = failed
    db.session.commit()
    log_task(f"Campaign {campaign.name} completed. Sent: {sent}, Failed: {failed}")


def get_rotation_smtp_profiles(user_id):
    """Get active SMTP profiles for rotation."""
    from app.models import SMTPServer
    
    profiles = SMTPServer.query.filter_by(user_id=user_id, is_active=True).order_by(SMTPServer.priority).all()
    valid_profiles = []
    
    for profile in profiles:
        if profile.last_reset_date != datetime.utcnow().date():
            profile.sent_today = 0
            profile.last_reset_date = datetime.utcnow().date()
            db.session.commit()

        if profile.sent_today >= profile.daily_limit:
            continue
            
        password = profile.get_password()
        if not password:
            continue
        
        config = profile.to_dict()
        config['id'] = profile.id
        config['password'] = password
        valid_profiles.append(config)
        
    return valid_profiles


@celery.task(bind=True)
def send_single_email_task(self, recipient_id, campaign_id):
    """Send a single email."""
    from app import create_app
    from app.models import Campaign, Recipient
    from app.core_logic.smtp_handler import SMTPHandler
    from app.core_logic.personalization import PersonalizationEngine
    from flask import url_for
    
    app = create_app()
    
    with app.app_context():
        try:
            recipient = Recipient.query.get(recipient_id)
            campaign = Campaign.query.get(campaign_id)
            
            if not recipient or not campaign:
                return {"status": "error"}
            
            smtp_profile = campaign.smtp_profile
            if not smtp_profile:
                return {"status": "error", "message": "No SMTP profile"}
            
            config = smtp_profile.to_dict()
            if not config.get('password'):
                return {"status": "error"}
            
            handler = SMTPHandler(config)
            
            recipient.status = 'Sending'
            recipient.attempts += 1
            db.session.commit()
            
            engine = PersonalizationEngine(campaign, recipient)
            subj, body, plain = engine.personalize()
            
            success, msg = handler.send_email_sync(
                recipient.email, subj, body, plain
            )
            
            if success:
                recipient.status = 'Sent'
                recipient.sent_at = datetime.utcnow()
                recipient.status_message = "OK"
            else:
                recipient.status = 'Failed'
                recipient.status_message = msg
            
            db.session.commit()
            return {"status": "sent" if success else "failed", "message": msg}
        except Exception as e:
            if recipient:
                recipient.status = 'Failed'
                recipient.status_message = str(e)
                db.session.commit()
            return {"status": "error", "message": str(e)}


@celery.task
def process_scheduled_campaigns():
    """Process scheduled campaigns."""
    from app import create_app
    from app.models import Campaign
    
    app = create_app()
    
    with app.app_context():
        now = datetime.utcnow()
        scheduled = Campaign.query.filter(
            Campaign.status == 'Scheduled',
            Campaign.scheduled_at <= now
        ).all()
        
        for c in scheduled:
            log_task(f"Starting scheduled campaign: {c.name}")
            c.status = 'Sending'
            c.started_at = now
            db.session.commit()
            send_campaign_task.delay(c.id)
        
        return {"processed": len(scheduled)}


@celery.task
def process_sequence_automation():
    """Process sequence automation."""
    return {"status": "ok"}


@celery.task
def check_imap_replies():
    """Check IMAP replies."""
    return {"status": "checked"}


@celery.task
def cleanup_old_data():
    """Cleanup old data."""
    return {"status": "cleaned"}


@celery.task
def reset_daily_smtp_counts():
    """Reset daily SMTP counts."""
    from app import create_app
    from app.models import SMTPServer
    
    app = create_app()
    
    with app.app_context():
        today = datetime.utcnow().date()
        updated = SMTPServer.query.filter(
            SMTPServer.last_reset_date != today
        ).update(
            {'sent_today': 0, 'last_reset_date': today},
            synchronize_session=False
        )
        db.session.commit()
        return {"profiles_reset": updated}


@celery.task
def generate_campaign_report(campaign_id, user_email):
    """Generate campaign report."""
    return {"status": "completed"}
