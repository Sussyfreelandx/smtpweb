import time
import logging
import os
import smtplib
import socket
import socks
from datetime import datetime, timedelta
from celery import shared_task
from flask import url_for
from app import db
from app.models import Campaign, Recipient, SMTPServer, Suppression, Sequence, SequenceRecipient, DailyStats, HourlyStats
from app.core_logic.smtp_handler import SMTPHandler, SMTPRotationManager
from app.core_logic.personalization import PersonalizationEngine
from app.utils import log_activity

logger = logging.getLogger(__name__)

# ==========================================
#   SMART SOCKET FACTORY
# ==========================================
def create_connection_with_proxy(host, port, timeout=None, source_address=None):
    """
    Creates a socket connection, automatically using SOCKS5 proxy if configured.
    Use this instead of global monkey patching to avoid Eventlet conflicts.
    """
    proxy_host = os.environ.get('SMTP_PROXY_HOST')
    proxy_port = int(os.environ.get('SMTP_PROXY_PORT', 1080))
    proxy_user = os.environ.get('SMTP_PROXY_USER')
    proxy_pass = os.environ.get('SMTP_PROXY_PASS')

    if proxy_host:
        # Check if internal host (Redises, Postgres, Localhost)
        is_internal = False
        if host.startswith("red-") or "render.internal" in host or host in ["localhost", "127.0.0.1"]:
            is_internal = True
            
        if not is_internal:
            try:
                # Setup SOCKS5 socket
                sock = socks.socksocket()
                if proxy_user and proxy_pass:
                    sock.set_proxy(socks.SOCKS5, proxy_host, proxy_port, True, proxy_user, proxy_pass)
                else:
                    sock.set_proxy(socks.SOCKS5, proxy_host, proxy_port)
                
                sock.settimeout(timeout)
                sock.connect((host, port))
                return sock
            except Exception as e:
                logger.error(f"Proxy Connection Failed to {host}:{port} - {e}")
                raise e

    # Fallback to standard socket
    return socket.create_connection((host, port), timeout, source_address)

# Patch smtplib locally for this module only
smtplib.socket.create_connection = create_connection_with_proxy

# ==========================================

def get_app():
    from app import create_app
    return create_app()

@shared_task(bind=True, max_retries=3)
def send_campaign_task(self, campaign_id):
    from flask import current_app
    
    # Ensure app context
    if not current_app:
        app = get_app()
        ctx = app.app_context()
        ctx.push()
    else:
        ctx = None

    try:
        # 1. Fetch Campaign
        # Use a fresh session query to ensure latest data
        db.session.expire_all()
        campaign = Campaign.query.get(campaign_id)
        
        if not campaign:
            log_activity(f"Campaign {campaign_id} not found", "ERROR")
            return {"status": "error", "message": "Campaign not found"}
        
        if campaign.status != 'Sending':
            log_activity(f"Campaign {campaign.name} is not in Sending status (Current: {campaign.status})", "WARNING")
            return {"status": "skipped", "message": "Campaign not in sending status"}
        
        # 2. Setup SMTP
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

        batch_size = campaign.throttle_amount or 20
        delay_seconds = campaign.throttle_delay or 60
        attachments = campaign.get_attachments()
        
        log_activity(f"Starting batch for: {campaign.name}", "INFO")
        
        total_sent = 0
        total_failed = 0
        
        # 3. Processing Loop
        while True:
            # Refresh DB state
            db.session.commit() # Commit previous changes first
            campaign = Campaign.query.get(campaign_id)
            
            if not campaign or campaign.status != 'Sending':
                break
            
            # Fetch Queued recipients
            recipients = campaign.recipients.filter_by(status='Queued').limit(batch_size).all()
            
            if not recipients:
                _complete_campaign(campaign, campaign.sent_count, campaign.failed_count)
                break
            
            # Prepare Batch
            current_handler = single_smtp_handler
            if rotation_manager:
                current_handler, err = rotation_manager.get_next_handler()
                if not current_handler:
                    campaign.status = 'Paused'
                    db.session.commit()
                    return {"status": "paused", "message": err}

            # Process recipients serially or in small threads to avoid overloading
            # Since we have concurrency issues, let's do this batch strictly serially for reliability first
            for recipient in recipients:
                recipient.status = 'Sending'
                recipient.attempts += 1
                db.session.commit() # Commit status change immediately
                
                try:
                    personalizer = PersonalizationEngine(campaign, recipient)
                    p_subject, p_body_html, p_body_plain = personalizer.personalize()
                    
                    unsubscribe_token = recipient.get_tracking_token('unsubscribe')
                    unsubscribe_url = url_for('tracking.unsubscribe', token=unsubscribe_token, _external=True)
                    
                    # SEND
                    success, msg = current_handler.send_email_sync(
                        recipient.email,
                        p_subject,
                        p_body_html,
                        p_body_plain,
                        unsubscribe_url,
                        attachments,
                        {'X-Campaign-ID': str(campaign.id)}
                    )
                    
                    if success:
                        recipient.status = 'Sent'
                        recipient.sent_at = datetime.utcnow()
                        recipient.status_message = "OK"
                        total_sent += 1
                        campaign.sent_count = (campaign.sent_count or 0) + 1
                        _update_stats(campaign.user_id)
                    else:
                        recipient.status = 'Failed'
                        recipient.status_message = msg[:250] if msg else "Unknown error"
                        total_failed += 1
                        campaign.failed_count = (campaign.failed_count or 0) + 1
                        
                        # Handle bounces
                        if 'bounce' in str(msg).lower():
                            _add_suppression(recipient.email, f"Bounce: {msg[:50]}", campaign.user_id)
                
                except Exception as e:
                    recipient.status = 'Failed'
                    recipient.status_message = f"Error: {str(e)}"
                    total_failed += 1
                    logger.error(f"Recipient processing error: {e}")
                
                db.session.commit()
            
            # Throttle
            if delay_seconds > 0:
                time.sleep(delay_seconds)

        if rotation_manager:
            rotation_manager.close_all()
        elif single_smtp_handler:
            single_smtp_handler.disconnect()
            
        return {"status": "completed", "sent": total_sent, "failed": total_failed}

    except Exception as e:
        log_activity(f"Campaign crashed: {str(e)}", "ERROR")
        try:
            campaign = Campaign.query.get(campaign_id)
            if campaign: 
                campaign.status = 'Failed'
                db.session.commit()
        except: pass
        raise self.retry(exc=e, countdown=60)
    finally:
        if ctx: ctx.pop()

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
        if not password:  continue
        config = profile.to_dict()
        config['id'] = profile.id
        config['password'] = password
        valid_profiles.append(config)
    return valid_profiles

# --- Placeholders for other tasks ---
@shared_task
def process_scheduled_campaigns():
    return {"status": "checked"}

@shared_task
def send_single_email_task(recipient_id, campaign_id):
    return {"status": "deprecated"} # Use campaign task

@shared_task
def reset_daily_smtp_counts():
    return {"status": "done"}

@shared_task
def process_sequence_automation():
    return {"status": "done"}

@shared_task
def generate_campaign_report(cid, email):
    return {"status": "done"}

@shared_task
def cleanup_old_data():
    return {"status": "done"}

@shared_task
def check_imap_replies():
    return {"status": "done"}
