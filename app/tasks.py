import time
import logging
import smtplib
from datetime import datetime, timedelta
from celery import shared_task
from flask import url_for, current_app
from app import db
from app.models import Campaign, Recipient, SMTPServer, Suppression, Sequence, SequenceRecipient, DailyStats, HourlyStats
from app.core_logic.smtp_handler import SMTPHandler, SMTPRotationManager
from app.core_logic.personalization import PersonalizationEngine
from app.utils import log_activity

logger = logging.getLogger(__name__)

# NOTE: Proxy patching is handled in app/__init__.py to prevent recursion errors.

def get_app():
    from app import create_app
    return create_app()

@shared_task(bind=True, max_retries=3)
def send_campaign_task(self, campaign_id):
    """
    Main task to send a campaign.
    """
    # Ensure we have an app context
    if not current_app:
        app = get_app()
        ctx = app.app_context()
        ctx.push()
    else:
        ctx = None

    try:
        # Re-query strictly to ensure fresh data
        campaign = Campaign.query.get(campaign_id)
        if not campaign:
            log_activity(f"Campaign {campaign_id} not found", "ERROR")
            return {"status": "error", "message": "Campaign not found"}
        
        if campaign.status != 'Sending':
            log_activity(f"Campaign {campaign.name} is not in Sending status (Current: {campaign.status})", "WARNING")
            return {"status": "skipped", "message": "Campaign not in sending status"}
        
        # --- 1. Setup SMTP Configuration ---
        rotation_manager = None
        single_smtp_handler = None
        
        if campaign.smtp_rotation_enabled:
            smtp_profiles_data = get_rotation_smtp_profiles(campaign.user_id)
            if not smtp_profiles_data:
                _fail_campaign(campaign, "No valid SMTP profiles available for rotation")
                return {"status": "error", "message": "No SMTP profiles available"}
            rotation_manager = SMTPRotationManager(smtp_profiles_data)
        else:
            smtp_profile = campaign.smtp_profile
            if not smtp_profile:
                _fail_campaign(campaign, "No SMTP profile assigned")
                return {"status":  "error", "message": "No SMTP profile"}
            
            smtp_config = smtp_profile.to_dict()
            if not smtp_config.get('password'):
                _fail_campaign(campaign, "SMTP password not configured")
                return {"status":  "error", "message": "SMTP password missing"}
            
            single_smtp_handler = SMTPHandler(smtp_config)

        # --- 2. Sending Configuration ---
        batch_size = campaign.throttle_amount or 20
        delay_seconds = campaign.throttle_delay or 60
        attachments = campaign.get_attachments()
        
        log_activity(f"Starting campaign: {campaign.name}. Batch: {batch_size}, Delay: {delay_seconds}s", "INFO")
        
        total_sent = 0
        total_failed = 0
        
        # --- 3. Main Sending Loop ---
        while True:
            # Refresh session to prevent stale data
            db.session.expire_all()
            campaign = Campaign.query.get(campaign_id)
            
            if not campaign or campaign.status != 'Sending':
                log_activity(f"Campaign {campaign_id} status changed to {campaign.status}. Stopping.", "WARNING")
                break
            
            # Fetch next batch
            recipients = campaign.recipients.filter_by(status='Queued').limit(batch_size).all()
            
            if not recipients:
                _complete_campaign(campaign, total_sent, total_failed)
                break
            
            email_tasks = []
            recipient_map = {}
            
            # Get Handler
            current_handler = single_smtp_handler
            if rotation_manager:
                current_handler, err = rotation_manager.get_next_handler()
                if not current_handler:
                    log_activity(f"SMTP rotation exhausted: {err}", "WARNING")
                    campaign.status = 'Paused'
                    db.session.commit()
                    return {"status": "paused", "message": err}

            # Prepare Tasks
            for recipient in recipients:
                recipient.status = 'Sending'
                recipient.attempts += 1
                recipient_map[recipient.email] = recipient
                
                personalizer = PersonalizationEngine(campaign, recipient)
                p_subject, p_body_html, p_body_plain = personalizer.personalize()
                
                # Use the new token-based unsubscribe route
                try:
                    unsubscribe_token = recipient.get_tracking_token('unsubscribe')
                    unsubscribe_url = url_for('tracking.unsubscribe', token=unsubscribe_token, _external=True)
                except Exception as e:
                    logger.error(f"Error building unsub link: {e}")
                    unsubscribe_url = "#"

                task = {
                    'to_email': recipient.email,
                    'subject': p_subject,
                    'html_content': p_body_html,
                    'plain_content': p_body_plain,
                    'unsubscribe_url':  unsubscribe_url,
                    'attachments': attachments,
                    'custom_headers': {'X-Campaign-ID': str(campaign.id)}
                }
                email_tasks.append(task)
            
            db.session.commit() # Commit 'Sending' status before network call
            
            # --- 4. Send Batch ---
            results = current_handler.send_bulk_threaded(email_tasks, max_workers=5)
            
            # --- 5. Process Results ---
            for res in results:
                email = res['email']
                success = res['success']
                error_msg = res.get('error')
                
                recipient = recipient_map.get(email)
                if not recipient: continue
                
                if success:
                    recipient.status = 'Sent'
                    recipient.sent_at = datetime.utcnow()
                    recipient.status_message = "OK"
                    total_sent += 1
                    
                    # Update stats inside the loop
                    _update_stats(campaign.user_id)
                    
                    try:
                        from app.webhooks.routes import trigger_email_event
                        trigger_email_event('email.sent', recipient, campaign)
                    except: pass
                else:
                    recipient.status = 'Failed'
                    recipient.status_message = error_msg[:250] if error_msg else "Unknown error"
                    total_failed += 1
                    
                    failure_type = current_handler.classify_failure(error_msg or "")
                    if failure_type == 'hard_bounce':
                        _add_suppression(recipient.email, f"Hard bounce: {error_msg[:50]}", campaign.user_id)
                    
                    # Re-queue on connection errors
                    if failure_type == 'connection_error' and recipient.attempts < 3:
                        recipient.status = 'Queued'
            
            db.session.commit()
            
            # Update Dashboard Progress
            try:
                from app.events import broadcast_campaign_progress
                total = campaign.total_recipients
                broadcast_campaign_progress(campaign_id, total_sent, total_failed, total, f"Batch sent via {current_handler.smtp_server}")
            except: pass

            # Throttling Delay
            db.session.expire_all()
            campaign = Campaign.query.get(campaign_id)
            if campaign and campaign.status == 'Sending':
                remaining = campaign.recipients.filter_by(status='Queued').count()
                if remaining > 0 and delay_seconds > 0:
                    time.sleep(delay_seconds)

        # Cleanup
        if rotation_manager:
            rotation_manager.close_all()
        elif single_smtp_handler:
            single_smtp_handler.disconnect()
            
        return {"status": "completed", "sent": total_sent, "failed": total_failed}

    except Exception as e:
        log_activity(f"Campaign sending CRITICAL error: {str(e)}", "ERROR")
        try:
            db.session.rollback()
            campaign = Campaign.query.get(campaign_id)
            if campaign: 
                campaign.status = 'Failed'
                db.session.commit()
        except: pass
        raise self.retry(exc=e, countdown=60)
        
    finally:
        if ctx:
            ctx.pop()

# --- Helper Functions ---

def _fail_campaign(campaign, message):
    campaign.status = 'Failed'
    db.session.commit()
    log_activity(f"Campaign {campaign.id} failed: {message}", "ERROR")

def _complete_campaign(campaign, sent, failed):
    campaign.status = 'Completed'
    campaign.completed_at = datetime.utcnow()
    db.session.commit()
    log_activity(f"Campaign {campaign.name} completed. Sent: {sent}, Failed: {failed}", "SUCCESS")
    try:
        from app.webhooks.routes import trigger_campaign_event
        trigger_campaign_event('campaign.completed', campaign)
    except: pass

def _add_suppression(email, reason, user_id):
    if not Suppression.query.filter_by(email=email).first():
        sup = Suppression(email=email, reason=reason, source='campaign', user_id=user_id)
        db.session.add(sup)

def _update_stats(user_id):
    try:
        today = datetime.utcnow().date()
        hour = datetime.utcnow().hour
        
        daily = DailyStats.query.filter_by(user_id=user_id, date=today).first()
        if not daily: 
            daily = DailyStats(user_id=user_id, date=today)
            db.session.add(daily)
        daily.emails_sent += 1
        
        hourly = HourlyStats.query.filter_by(user_id=user_id, hour_of_day=hour).first()
        if not hourly: 
            hourly = HourlyStats(user_id=user_id, hour_of_day=hour)
            db.session.add(hourly)
        hourly.total_sent += 1
        
        db.session.commit()
    except Exception: 
        db.session.rollback()

def get_rotation_smtp_profiles(user_id):
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
        if not password: continue
        
        config = profile.to_dict()
        config['id'] = profile.id
        config['password'] = password
        valid_profiles.append(config)
        
    return valid_profiles

@shared_task(bind=True)
def send_single_email_task(self, recipient_id, campaign_id):
    if not current_app:
        app = get_app()
        ctx = app.app_context()
        ctx.push()
    else:
        ctx = None
        
    try:
        recipient = Recipient.query.get(recipient_id)
        campaign = Campaign.query.get(campaign_id)
        
        if not recipient or not campaign: return {"status": "error"}
        
        smtp_profile = campaign.smtp_profile
        if not smtp_profile: return {"status": "error", "message": "No SMTP profile"}
        
        config = smtp_profile.to_dict()
        if not config.get('password'): return {"status": "error"}
        
        handler = SMTPHandler(config)
        
        recipient.status = 'Sending'
        recipient.attempts += 1
        db.session.commit()
        
        engine = PersonalizationEngine(campaign, recipient)
        subj, body, plain = engine.personalize()
        
        unsub = url_for('tracking.unsubscribe', token=recipient.get_tracking_token('unsubscribe'), _external=True)
        
        success, msg = handler.send_email_sync(
            recipient.email, subj, body, plain, unsub, campaign.get_attachments()
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
    finally:
        if ctx: ctx.pop()

@shared_task
def process_scheduled_campaigns():
    if not current_app:
        app = get_app()
        ctx = app.app_context()
        ctx.push()
    else:
        ctx = None
        
    try:
        now = datetime.utcnow()
        scheduled = Campaign.query.filter(Campaign.status == 'Scheduled', Campaign.scheduled_at <= now).all()
        for c in scheduled: 
            log_activity(f"Starting scheduled campaign: {c.name}", "INFO")
            c.status = 'Sending'
            c.started_at = now
            db.session.commit()
            send_campaign_task.delay(c.id)
        return {"processed": len(scheduled)}
    finally:
        if ctx: ctx.pop()

@shared_task
def process_sequence_automation():
    if not current_app:
        app = get_app()
        ctx = app.app_context()
        ctx.push()
    else:
        ctx = None
    try:
        now = datetime.utcnow()
        due = SequenceRecipient.query.filter(SequenceRecipient.status == 'Active', SequenceRecipient.next_action_at <= now).limit(100).all()
        count = 0
        for sr in due:
            sequence = Sequence.query.get(sr.sequence_id)
            if not sequence: continue
            sr.current_step += 1
            next_step = sequence.steps.filter_by(step_number=sr.current_step).first()
            if next_step: 
                sr.next_action_at = now + timedelta(days=next_step.delay_days, hours=next_step.delay_hours)
            else:
                sr.status = 'Completed'
                sr.next_action_at = None
            count += 1
        db.session.commit()
        return {"processed": count}
    finally:
        if ctx: ctx.pop()

@shared_task
def check_imap_replies():
    return {"status": "checked"}

@shared_task
def cleanup_old_data():
    return {"status": "cleaned"}

@shared_task
def reset_daily_smtp_counts():
    if not current_app:
        app = get_app()
        ctx = app.app_context()
        ctx.push()
    else:
        ctx = None
    try:
        today = datetime.utcnow().date()
        updated = SMTPServer.query.filter(SMTPServer.last_reset_date != today).update({'sent_today': 0, 'last_reset_date': today}, synchronize_session=False)
        db.session.commit()
        return {"profiles_reset": updated}
    finally:
        if ctx: ctx.pop()

@shared_task
def generate_campaign_report(campaign_id, user_email):
    if not current_app:
        app = get_app()
        ctx = app.app_context()
        ctx.push()
    else:
        ctx = None
    try:
        campaign = Campaign.query.get(campaign_id)
        if not campaign: return
        return {"status": "completed"}
    finally:
        if ctx: ctx.pop()
