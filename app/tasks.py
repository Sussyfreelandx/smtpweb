import time
import json
import logging
import os
import socks
import socket
import smtplib
from datetime import datetime, timedelta

# CRITICAL FIX: Use eventlet for non-blocking sleep
import eventlet
eventlet.monkey_patch()

from celery import shared_task
from flask import url_for, current_app
from app import db, create_app, IS_CELERY
from app.models import Campaign, Recipient, SMTPServer, Suppression, DailyStats, HourlyStats
from app.core_logic.smtp_handler import SMTPHandler, SMTPRotationManager
from app.utils import log_activity
from app.main.events import broadcast_campaign_progress, broadcast_campaign_status_change

# Configure module-level logger
logger = logging.getLogger(__name__)

# ==========================================
#   CRITICAL:  WORKER PROXY CONFIGURATION
# ==========================================
# This block ensures the background worker tunnels traffic through your VPS
# and forces IPv4 to prevent Office365/Gmail connection crashes. 

PROXY_HOST = os.environ.get('SMTP_PROXY_HOST')
PROXY_PORT = int(os.environ.get('SMTP_PROXY_PORT', 1080))
PROXY_USER = os.environ.get('SMTP_PROXY_USER')
PROXY_PASS = os.environ.get('SMTP_PROXY_PASS')

if PROXY_HOST: 
    original_getaddrinfo = socket.getaddrinfo

    def patched_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        if family == 0 or family == socket.AF_INET6:
            try:
                return original_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)
            except socket.gaierror:
                pass
        return original_getaddrinfo(host, port, family, type, proto, flags)

    socket.getaddrinfo = patched_getaddrinfo
    
    if PROXY_USER and PROXY_PASS: 
        socks.set_default_proxy(socks.SOCKS5, PROXY_HOST, PROXY_PORT, username=PROXY_USER, password=PROXY_PASS)
        logger.info(f"🔌 Worker Proxy Active: {PROXY_HOST}:{PROXY_PORT} (Auth: Yes)")
    else:
        socks.set_default_proxy(socks.SOCKS5, PROXY_HOST, PROXY_PORT)
        logger.info(f"🔌 Worker Proxy Active: {PROXY_HOST}:{PROXY_PORT} (Auth: No)")
    
    socks.wrap_module(smtplib)

# ==========================================

_app_instance = None

def get_app():
    """Get or create Flask app for Celery tasks (cached)."""
    global _app_instance
    if _app_instance is None:
        _app_instance = create_app()
    return _app_instance


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def send_campaign_task(self, campaign_id):
    """
    Main task to send a campaign.
    Updated to use robust multi-threaded SMTP sending and non-blocking sleep.
    """
    app = get_app()
    
    with app.app_context():
        try:
            campaign = Campaign.query.get(campaign_id)
            if not campaign:
                logger.error(f"Campaign {campaign_id} not found in task.")
                return {"status": "error", "message": "Campaign not found"}
            
            # Initial state check
            if campaign.status != 'Sending':
                logger.warning(f"Campaign {campaign.name} ({campaign_id}) is not in 'Sending' status. Current status: {campaign.status}. Aborting task.")
                return {"status": "skipped", "message": f"Campaign not in 'Sending' status. Status is '{campaign.status}'."}
            
            # --- 1. Setup SMTP Configuration ---
            rotation_manager = None
            single_smtp_handler = None
            
            if campaign.smtp_rotation_enabled:
                smtp_profiles_data = get_rotation_smtp_profiles(campaign.user_id)
                if not smtp_profiles_data:
                    _fail_campaign(campaign, "No valid SMTP profiles available for rotation")
                    return {"status": "error", "message": "No SMTP profiles available"}
                rotation_manager = SMTPRotationManager(smtp_profiles_data)
                logger.info(f"Campaign {campaign.id}: Using SMTP Rotation with {len(smtp_profiles_data)} profiles.")
            else:
                smtp_profile = campaign.smtp_profile
                if not smtp_profile:
                    _fail_campaign(campaign, "No SMTP profile assigned")
                    return {"status": "error", "message": "No SMTP profile assigned"}
                
                smtp_config = smtp_profile.to_dict()
                if not smtp_config.get('password'):
                    _fail_campaign(campaign, f"SMTP profile '{smtp_profile.profile_name}' is missing a password.")
                    return {"status": "error", "message": "SMTP password missing"}
                
                single_smtp_handler = SMTPHandler(smtp_config)
                logger.info(f"Campaign {campaign.id}: Using single SMTP profile '{smtp_profile.profile_name}'.")

            # --- 2. Sending Configuration ---
            batch_size = campaign.throttle_amount or 20
            delay_seconds = campaign.throttle_delay or 60
            attachments = campaign.get_attachments()
            
            logger.info(f"Starting campaign '{campaign.name}' ({campaign_id}). Batch size: {batch_size}, Delay: {delay_seconds}s.")
            
            total_sent_in_run = 0
            total_failed_in_run = 0
            
            # --- 3. Main Sending Loop ---
            while True:
                # Refresh campaign state from DB to check for pauses/stops
                db.session.expire(campaign)
                db.session.refresh(campaign)
                
                if campaign.status != 'Sending':
                    logger.warning(f"Campaign {campaign_id} status changed to '{campaign.status}'. Stopping worker.")
                    break
                
                recipients = campaign.recipients.filter_by(status='Queued').limit(batch_size).all()
                
                if not recipients:
                    _complete_campaign(campaign)
                    logger.info(f"Campaign {campaign.id} has no more queued recipients. Marking as complete.")
                    break
                
                logger.info(f"Campaign {campaign.id}: Fetched batch of {len(recipients)} recipients.")
                
                # --- Prepare and Send Batch ---
                email_tasks = []
                recipient_map = {}
                
                current_handler = single_smtp_handler
                if rotation_manager:
                    current_handler, err = rotation_manager.get_next_handler()
                    if not current_handler:
                        logger.warning(f"Campaign {campaign.id}: SMTP rotation exhausted: {err}. Pausing campaign.")
                        campaign.status = 'Paused'
                        db.session.commit()
                        broadcast_campaign_status_change(campaign.id, 'Paused', 'All SMTP servers at limit.')
                        break # Exit the loop
                
                for recipient in recipients:
                    recipient.status = 'Sending'
                    recipient.attempts = (recipient.attempts or 0) + 1
                    recipient_map[recipient.email] = recipient
                    
                    # Personalization logic here (assuming it's handled in SMTP handler or needs to be added)
                    task = {
                        'to_email': recipient.email,
                        'subject': campaign.subject, # Placeholder, should be personalized
                        'html_content': campaign.body_html, # Placeholder
                        'recipient_data': recipient.get_data(),
                        'attachments': attachments,
                        'custom_headers': {'X-Campaign-ID': str(campaign.id)}
                    }
                    email_tasks.append(task)
                
                db.session.commit() # Commit 'Sending' status update
                
                # Use the robust threaded method from the handler
                logger.info(f"Campaign {campaign.id}: Sending batch of {len(email_tasks)} emails...")
                results = current_handler.send_bulk_threaded(email_tasks, campaign, max_workers=5)
                
                # --- 5. Process Results ---
                batch_sent = 0
                batch_failed = 0
                
                for res in results:
                    recipient = recipient_map.get(res['email'])
                    if not recipient: continue
                    
                    if res['success']:
                        recipient.status = 'Sent'
                        recipient.sent_at = datetime.utcnow()
                        recipient.status_message = "OK"
                        batch_sent += 1
                    else:
                        recipient.status = 'Failed'
                        recipient.status_message = str(res.get('error', 'Unknown error'))[:255]
                        batch_failed += 1
                        # Optional: Add to suppression list for hard bounces
                        if 'hard_bounce' in str(res.get('error', '')).lower():
                            _add_suppression(recipient.email, f"Hard bounce: {res.get('error','')}", campaign.user_id)
                
                # Update campaign-level counters in a single transaction
                campaign.sent_count = (campaign.sent_count or 0) + batch_sent
                campaign.failed_count = (campaign.failed_count or 0) + batch_failed
                db.session.commit()
                
                total_sent_in_run += batch_sent
                total_failed_in_run += batch_failed

                logger.info(f"Campaign {campaign.id}: Batch finished. Sent: {batch_sent}, Failed: {batch_failed}.")
                
                # Broadcast progress
                broadcast_campaign_progress(campaign_id, campaign.sent_count, campaign.failed_count, campaign.total_recipients, f"Batch sent via {current_handler.smtp_server}")

                # --- 6. Throttling ---
                db.session.expire(campaign)
                db.session.refresh(campaign)
                if campaign.status == 'Sending':
                    remaining = campaign.recipients.filter_by(status='Queued').count()
                    if remaining > 0 and delay_seconds > 0:
                        logger.info(f"Throttling campaign {campaign.id}: waiting {delay_seconds}s. {remaining} recipients remaining.")
                        # CRITICAL FIX: Use non-blocking sleep
                        eventlet.sleep(delay_seconds)

            # Cleanup
            if rotation_manager:
                rotation_manager.close_all()
            elif single_smtp_handler:
                single_smtp_handler.disconnect()
                
            return {
                "status": "finished",
                "sent_in_run": total_sent_in_run,
                "failed_in_run": total_failed_in_run
            }

        except Exception as e:
            logger.critical(f"Campaign sending CRITICAL error for campaign {campaign_id}: {str(e)}", exc_info=True)
            try:
                # Use a new session to be safe
                with app.app_context():
                    campaign = Campaign.query.get(campaign_id)
                    if campaign: 
                        _fail_campaign(campaign, f"Critical error: {str(e)}")
            except Exception as db_err:
                logger.error(f"Failed to update campaign status to 'Failed' after critical error: {db_err}")
            raise self.retry(exc=e)

# --- Helper Functions ---

def _fail_campaign(campaign, message):
    campaign.status = 'Failed'
    campaign.completed_at = datetime.utcnow()
    db.session.commit()
    broadcast_campaign_status_change(campaign.id, 'Failed', message)
    log_activity(f"Campaign {campaign.id} failed: {message}", "ERROR")

def _complete_campaign(campaign):
    campaign.status = 'Completed'
    campaign.completed_at = datetime.utcnow()
    db.session.commit()
    message = f"Sent: {campaign.sent_count}, Failed: {campaign.failed_count}"
    broadcast_campaign_status_change(campaign.id, 'Completed', message)
    log_activity(f"Campaign {campaign.name} completed. {message}", "SUCCESS")
    try:
        from app.webhooks.routes import trigger_campaign_event
        trigger_campaign_event('campaign.completed', campaign)
    except Exception as e:
        logger.warning(f"Failed to trigger campaign.completed webhook for {campaign.id}: {e}")

def _add_suppression(email, reason, user_id):
    if not Suppression.query.filter_by(email=email).first():
        sup = Suppression(email=email, reason=reason, source='campaign', user_id=user_id)
        db.session.add(sup)
        db.session.commit()

# Other tasks remain the same...

@shared_task
def process_scheduled_campaigns():
    """Check and start scheduled campaigns."""
    app = get_app()
    with app.app_context():
        now = datetime.utcnow()
        scheduled_campaigns = Campaign.query.filter(
            Campaign.status == 'Scheduled', 
            Campaign.scheduled_at <= now
        ).all()
        
        for c in scheduled_campaigns: 
            logger.info(f"Starting scheduled campaign: {c.name} ({c.id})")
            c.status = 'Sending'
            c.started_at = now
            db.session.commit()
            broadcast_campaign_status_change(c.id, 'Sending', 'Scheduled start initiated.')
            send_campaign_task.delay(c.id)
            
        return {"processed": len(scheduled_campaigns)}

# ... (The rest of the tasks file can remain as is)
@shared_task(bind=True)
def send_single_email_task(self, recipient_id, campaign_id):
    """Task to send a single email."""
    app = get_app()
    with app.app_context():
        recipient = Recipient.query.get(recipient_id)
        campaign = Campaign.query.get(campaign_id)
        
        if not recipient or not campaign:  return {"status": "error"}
        
        smtp_profile = campaign.smtp_profile
        if not smtp_profile: return {"status": "error", "message": "No SMTP profile"}
        
        config = smtp_profile.to_dict()
        if not config.get('password'): return {"status": "error"}
        
        handler = SMTPHandler(config)
        
        try:
            recipient.status = 'Sending'
            recipient.attempts += 1
            db.session.commit()
            
            from app.core_logic.personalization import PersonalizationEngine
            engine = PersonalizationEngine(campaign, recipient)
            subj, body, plain = engine.personalize()
            
            unsub = url_for('main.unsubscribe', token=recipient.get_tracking_token('unsubscribe'), _external=True)
            
            success, msg = handler.send_email_sync(
                recipient.email, subj, body, plain, unsub, campaign.get_attachments()
            )
            
            if success:
                recipient.status = 'Sent'
                recipient.sent_at = datetime.utcnow()
                recipient.status_message = "OK"
                campaign.sent_count += 1
            else:
                recipient.status = 'Failed'
                recipient.status_message = msg
                campaign.failed_count += 1
            
            db.session.commit()
            return {"status": "sent" if success else "failed", "message": msg}
        except Exception as e:
            recipient.status = 'Failed'
            recipient.status_message = str(e)
            db.session.commit()
            return {"status": "error", "message": str(e)}

@shared_task
def process_sequence_automation():
    """Process automated sequence steps."""
    app = get_app()
    with app.app_context():
        now = datetime.utcnow()
        # In a real app, you would have a SequenceRecipient model
        # This is a placeholder
        return {"processed": 0}

@shared_task
def check_imap_replies():
    """Check IMAP for replies."""
    # Placeholder for IMAP logic
    return {"status":  "checked"}


@shared_task
def cleanup_old_data():
    """Cleanup old logs."""
    # Placeholder for cleanup
    return {"status": "cleaned"}


@shared_task
def reset_daily_smtp_counts():
    """Reset daily send counts."""
    app = get_app()
    with app.app_context():
        today = datetime.utcnow().date()
        updated = SMTPServer.query.filter(SMTPServer.last_reset_date != today).update({'sent_today': 0, 'last_reset_date': today}, synchronize_session=False)
        db.session.commit()
        return {"profiles_reset": updated}

@shared_task
def generate_campaign_report(campaign_id, user_email):
    """Generate and email a campaign report."""
    app = get_app()
    with app.app_context():
        campaign = Campaign.query.get(campaign_id)
        if not campaign: return
        # Placeholder logic
        return {"status": "completed"}
