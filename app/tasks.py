import time
import logging
import os
import smtplib
import socket
import socks
import ipaddress
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
#   WORKER PROXY PATCH (SAFE MODE)
# ==========================================
PROXY_HOST = os.environ.get('SMTP_PROXY_HOST')

# We apply this patch ONLY if we haven't already patched it.
# This check prevents "RecursionError".
if PROXY_HOST and socket.socket is not socks.socksocket:
    PROXY_PORT = int(os.environ.get('SMTP_PROXY_PORT', 1080))
    PROXY_USER = os.environ.get('SMTP_PROXY_USER')
    PROXY_PASS = os.environ.get('SMTP_PROXY_PASS')
    
    logger.info(f"🔌 Worker: Patching socket with SOCKS5 Proxy ({PROXY_HOST})...")
    
    class SmartSocket(socks.socksocket):
        def connect(self, dest_pair):
            host, port = dest_pair
            is_internal = False
            
            # Simple check for internal services (Redis, DB, Localhost)
            if isinstance(host, str):
                if host.startswith("red-") or "render.internal" in host or host == "localhost" or host == "127.0.0.1":
                    is_internal = True
            
            if is_internal:
                self.set_proxy(None) # Disable proxy for internal traffic
            else:
                # Enable proxy for external traffic (SMTP)
                if PROXY_USER and PROXY_PASS:
                    self.set_proxy(socks.SOCKS5, PROXY_HOST, PROXY_PORT, True, PROXY_USER, PROXY_PASS)
                else:
                    self.set_proxy(socks.SOCKS5, PROXY_HOST, PROXY_PORT)
            
            return super(SmartSocket, self).connect(dest_pair)

    # Apply the patch
    socket.socket = SmartSocket
    socks.wrap_module(smtplib)
    
    # IPv4 Force Patch (Fixes PySocks crash with IPv6)
    original_getaddrinfo = socket.getaddrinfo
    def patched_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        if family == 0 or family == socket.AF_INET6:
            try:
                return original_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)
            except socket.gaierror:
                pass
        return original_getaddrinfo(host, port, family, type, proto, flags)
    socket.getaddrinfo = patched_getaddrinfo

# ==========================================

def get_app():
    from app import create_app
    return create_app()

@shared_task(bind=True, max_retries=3)
def send_campaign_task(self, campaign_id):
    """
    Main background task for sending campaigns.
    """
    # Create a fresh app context for this thread
    app = get_app()
    with app.app_context():
        try:
            campaign = Campaign.query.get(campaign_id)
            if not campaign:
                log_activity(f"Campaign {campaign_id} not found", "ERROR")
                return {"status": "error", "message": "Campaign not found"}
            
            # Double check status to prevent zombies
            if campaign.status != 'Sending':
                # Force status update if it looks stuck
                if campaign.status == 'Scheduled':
                    campaign.status = 'Sending'
                    db.session.commit()
                else:
                    log_activity(f"Campaign {campaign.name} is {campaign.status}, stopping task.", "WARNING")
                    return {"status": "skipped", "message": f"Campaign is {campaign.status}"}
            
            # --- 1. Setup SMTP ---
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
            
            log_activity(f"Starting sending loop: {campaign.name}", "INFO")
            
            total_sent = 0
            total_failed = 0
            
            # --- 3. Main Loop ---
            while True:
                # Refresh campaign state (important for pause detection)
                db.session.expire_all()
                campaign = Campaign.query.get(campaign_id)
                
                if not campaign or campaign.status != 'Sending':
                    log_activity(f"Campaign stopped/paused by user.", "WARNING")
                    break
                
                # Fetch batch
                recipients = campaign.recipients.filter_by(status='Queued').limit(batch_size).all()
                
                if not recipients:
                    _complete_campaign(campaign, total_sent, total_failed)
                    break
                
                # Get Handler
                current_handler = single_smtp_handler
                if rotation_manager:
                    current_handler, err = rotation_manager.get_next_handler()
                    if not current_handler:
                        campaign.status = 'Paused'
                        db.session.commit()
                        log_activity(f"SMTP exhausted: {err}", "WARNING")
                        return {"status": "paused", "message": err}

                # Prepare Tasks
                email_tasks = []
                recipient_map = {}
                
                for recipient in recipients:
                    recipient.status = 'Sending'
                    recipient.attempts += 1
                    recipient_map[recipient.email] = recipient
                    
                    personalizer = PersonalizationEngine(campaign, recipient)
                    p_subject, p_body_html, p_body_plain = personalizer.personalize()
                    
                    unsubscribe_token = recipient.get_tracking_token('unsubscribe')
                    unsubscribe_url = url_for('main.unsubscribe', token=unsubscribe_token, _external=True)
                    
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
                
                # Commit 'Sending' status before network IO
                db.session.commit()
                
                # Send Batch (Threaded)
                # Since we are not using Eventlet in worker, standard threads work perfectly
                results = current_handler.send_bulk_threaded(email_tasks, max_workers=5)
                
                # Process Results
                batch_sent = 0
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
                        batch_sent += 1
                        total_sent += 1
                        _update_stats(campaign.user_id)
                    else:
                        recipient.status = 'Failed'
                        recipient.status_message = error_msg[:250] if error_msg else "Unknown error"
                        total_failed += 1
                        
                        # Handle bounces
                        if 'hard_bounce' in current_handler.classify_failure(error_msg or ""):
                            _add_suppression(recipient.email, "Hard Bounce", campaign.user_id)
                
                db.session.commit()
                
                # Update progress in background (fire and forget)
                try:
                    from app.events import broadcast_campaign_progress
                    broadcast_campaign_progress(campaign_id, total_sent, total_failed, campaign.total_recipients)
                except: pass

                # Throttling
                if delay_seconds > 0:
                    time.sleep(delay_seconds)

            # Cleanup
            if rotation_manager: rotation_manager.close_all()
            elif single_smtp_handler: single_smtp_handler.disconnect()
            
            return {"status": "completed", "sent": total_sent}

        except Exception as e:
            log_activity(f"Campaign Worker Exception: {str(e)}", "ERROR")
            try:
                campaign = Campaign.query.get(campaign_id)
                if campaign: 
                    campaign.status = 'Failed'
                    db.session.commit()
            except: pass
            raise self.retry(exc=e, countdown=60)

# ... (Helper functions remain unchanged) ...
def _fail_campaign(campaign, message):
    campaign.status = 'Failed'
    db.session.commit()
    log_activity(f"Campaign {campaign.id} failed: {message}", "ERROR")

def _complete_campaign(campaign, sent, failed):
    campaign.status = 'Completed'
    campaign.completed_at = datetime.utcnow()
    db.session.commit()
    log_activity(f"Campaign {campaign.name} completed. Sent: {sent}", "SUCCESS")

def _add_suppression(email, reason, user_id):
    if not Suppression.query.filter_by(email=email).first():
        sup = Suppression(email=email, reason=reason, source='campaign', user_id=user_id)
        db.session.add(sup)

def _update_stats(user_id):
    try:
        today = datetime.utcnow().date()
        daily = DailyStats.query.filter_by(user_id=user_id, date=today).first()
        if not daily: 
            daily = DailyStats(user_id=user_id, date=today)
            db.session.add(daily)
        daily.emails_sent += 1
        db.session.commit()
    except: 
        db.session.rollback()

def get_rotation_smtp_profiles(user_id):
    profiles = SMTPServer.query.filter_by(user_id=user_id, is_active=True).all()
    valid = []
    for p in profiles:
        if p.sent_today < p.daily_limit and p.get_password():
            config = p.to_dict()
            config['id'] = p.id
            config['password'] = p.get_password()
            valid.append(config)
    return valid

# Include other shared tasks (send_single_email_task, etc.) exactly as they were, 
# just ensure they import 'app' via 'get_app()' inside the task if they need context.
