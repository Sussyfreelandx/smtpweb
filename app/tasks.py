import time
import json
from datetime import datetime, timedelta
from celery import shared_task, current_task
from flask import url_for, current_app
from app import db, create_app
from app.models import Campaign, Recipient, SMTPServer, Suppression, Sequence, SequenceRecipient
from app.core_logic.smtp_handler import SMTPHandler, SMTPRotationManager, WarmupManager
from app.core_logic.personalization import PersonalizationEngine
from app.utils import log_activity


def get_app():
    """Get or create Flask app for Celery tasks."""
    return create_app()


@shared_task(bind=True, max_retries=3)
def send_campaign_task(self, campaign_id):
    """Main task to send a campaign."""
    app = get_app()
    
    with app.app_context():
        try:
            campaign = Campaign.query.get(campaign_id)
            if not campaign:
                log_activity(f"Campaign {campaign_id} not found", "ERROR")
                return {"status": "error", "message": "Campaign not found"}
            
            if campaign.status != 'Sending':
                log_activity(f"Campaign {campaign.name} is not in Sending status", "WARNING")
                return {"status": "skipped", "message": "Campaign not in sending status"}
            
            # Get SMTP configuration
            if campaign.smtp_rotation_enabled:
                smtp_profiles = get_rotation_smtp_profiles(campaign.user_id)
                if not smtp_profiles:
                    campaign.status = 'Failed'
                    db.session.commit()
                    log_activity("No valid SMTP profiles for rotation", "ERROR")
                    return {"status": "error", "message": "No SMTP profiles available"}
                
                rotation_manager = SMTPRotationManager(smtp_profiles)
            else:
                smtp_profile = campaign.smtp_profile
                if not smtp_profile:
                    campaign.status = 'Failed'
                    db.session.commit()
                    log_activity(f"No SMTP profile for campaign {campaign.name}", "ERROR")
                    return {"status": "error", "message": "No SMTP profile"}
                
                smtp_config = smtp_profile.to_dict()
                if not smtp_config.get('password'):
                    campaign.status = 'Failed'
                    db.session.commit()
                    log_activity("SMTP password not configured", "ERROR")
                    return {"status": "error", "message": "SMTP password missing"}
                
                rotation_manager = None
                smtp_handler = SMTPHandler(smtp_config)
            
            # Get warmup manager if enabled
            warmup_manager = None
            if campaign.warmup_mode and smtp_profile:
                warmup_manager = WarmupManager()
                if smtp_profile.warmup_start_date:
                    daily_limit = warmup_manager.get_daily_limit(smtp_profile.warmup_start_date)
                    log_activity(f"Warmup mode: Daily limit is {daily_limit}", "INFO")
            
            batch_size = campaign.throttle_amount or 20
            delay_seconds = campaign.throttle_delay or 60
            attachments = campaign.get_attachments()
            
            log_activity(f"Starting campaign: {campaign.name}. Batch: {batch_size}, Delay: {delay_seconds}s", "INFO")
            
            total_sent = 0
            total_failed = 0
            current_profile_index = 0
            
            # Main sending loop
            while True:
                db.session.expire_all()
                campaign = Campaign.query.get(campaign_id)
                
                if not campaign or campaign.status != 'Sending':
                    log_activity(f"Campaign {campaign_id} status changed. Stopping.", "WARNING")
                    break
                
                # Get next batch
                recipients = campaign.recipients.filter_by(status='Queued').limit(batch_size).all()
                
                if not recipients:
                    campaign.status = 'Completed'
                    campaign.completed_at = datetime.utcnow()
                    db.session.commit()
                    log_activity(f"Campaign {campaign.name} completed. Sent: {total_sent}, Failed: {total_failed}", "SUCCESS")
                    
                    # Trigger webhook
                    try:
                        from app.webhooks.routes import trigger_campaign_event
                        trigger_campaign_event('campaign.completed', campaign)
                    except Exception: 
                        pass
                    
                    break
                
                log_activity(f"Processing batch of {len(recipients)} recipients...", "INFO")
                
                for recipient in recipients:
                    db.session.expire_all()
                    campaign = Campaign.query.get(campaign_id)
                    
                    if not campaign or campaign.status != 'Sending':
                        break
                    
                    recipient = Recipient.query.get(recipient.id)
                    if not recipient or recipient.status != 'Queued':
                        continue
                    
                    # Get SMTP handler for this recipient
                    if rotation_manager:
                        smtp_handler, error = rotation_manager.get_next_handler()
                        if not smtp_handler:
                            log_activity(f"SMTP rotation exhausted: {error}", "WARNING")
                            campaign.status = 'Paused'
                            db.session.commit()
                            return {"status": "paused", "message": error}
                    
                    try:
                        recipient.status = 'Sending'
                        recipient.attempts += 1
                        db.session.commit()
                        
                        # Personalize content
                        personalizer = PersonalizationEngine(campaign, recipient)
                        p_subject, p_body_html, p_body_plain = personalizer.personalize()
                        
                        # Generate unsubscribe URL
                        unsubscribe_token = recipient.get_tracking_token('unsubscribe')
                        unsubscribe_url = url_for('main.unsubscribe', token=unsubscribe_token, _external=True)
                        
                        # Send email
                        success, message = smtp_handler.send_email_sync(
                            to_email=recipient.email,
                            subject=p_subject,
                            html_content=p_body_html,
                            plain_content=p_body_plain,
                            unsubscribe_url=unsubscribe_url,
                            attachments=attachments
                        )
                        
                        if success:
                            recipient.status = 'Sent'
                            recipient.sent_at = datetime.utcnow()
                            recipient.status_message = "OK"
                            total_sent += 1
                            
                            if smtp_profile: 
                                smtp_profile.sent_today += 1
                            
                            # Trigger webhook
                            try: 
                                from app.webhooks.routes import trigger_email_event
                                trigger_email_event('email.sent', recipient, campaign)
                            except Exception: 
                                pass
                        else:
                            recipient.status = 'Failed'
                            recipient.status_message = message[:250] if message else "Unknown error"
                            total_failed += 1
                            
                            # Classify failure
                            failure_type = smtp_handler.classify_failure(message or "")
                            if failure_type == 'hard_bounce':
                                # Add to suppression
                                if not Suppression.query.filter_by(email=recipient.email).first():
                                    suppression = Suppression(
                                        email=recipient.email,
                                        reason=f"Hard bounce: {message[:100]}",
                                        source='campaign'
                                    )
                                    db.session.add(suppression)
                            
                            log_activity(f"Failed to send to {recipient.email}: {message}", "ERROR")
                        
                        db.session.commit()
                        
                        # Update progress via WebSocket if available
                        try:
                            from app.main.events import broadcast_campaign_progress
                            total = campaign.recipients.count()
                            broadcast_campaign_progress(campaign_id, total_sent, total_failed, total, recipient.email)
                        except Exception:
                            pass
                    
                    except Exception as e: 
                        recipient.status = 'Failed'
                        recipient.status_message = str(e)[:250]
                        total_failed += 1
                        db.session.commit()
                        log_activity(f"Exception sending to {recipient.email}: {e}", "ERROR")
                
                # Throttling delay between batches
                db.session.expire_all()
                campaign = Campaign.query.get(campaign_id)
                
                if campaign and campaign.status == 'Sending':
                    remaining = campaign.recipients.filter_by(status='Queued').count()
                    if remaining > 0:
                        log_activity(f"Throttling: waiting {delay_seconds}s. {remaining} remaining.", "INFO")
                        time.sleep(delay_seconds)
            
            # Cleanup
            if rotation_manager:
                rotation_manager.close_all()
            
            return {
                "status": "completed",
                "sent": total_sent,
                "failed": total_failed
            }
        
        except Exception as e: 
            log_activity(f"Campaign sending error: {str(e)}", "ERROR")
            
            try:
                campaign = Campaign.query.get(campaign_id)
                if campaign: 
                    campaign.status = 'Failed'
                    db.session.commit()
            except Exception:
                pass
            
            raise self.retry(exc=e, countdown=60)


@shared_task(bind=True)
def send_single_email_task(self, recipient_id, campaign_id):
    """Task to send a single email (for retries or individual sends)."""
    app = get_app()
    
    with app.app_context():
        recipient = Recipient.query.get(recipient_id)
        campaign = Campaign.query.get(campaign_id)
        
        if not recipient or not campaign:
            return {"status": "error", "message": "Recipient or campaign not found"}
        
        smtp_profile = campaign.smtp_profile
        if not smtp_profile:
            return {"status": "error", "message": "No SMTP profile"}
        
        smtp_config = smtp_profile.to_dict()
        if not smtp_config.get('password'):
            return {"status": "error", "message": "SMTP password missing"}
        
        smtp_handler = SMTPHandler(smtp_config)
        
        try:
            recipient.status = 'Sending'
            recipient.attempts += 1
            db.session.commit()
            
            personalizer = PersonalizationEngine(campaign, recipient)
            p_subject, p_body_html, p_body_plain = personalizer.personalize()
            
            unsubscribe_token = recipient.get_tracking_token('unsubscribe')
            unsubscribe_url = url_for('main.unsubscribe', token=unsubscribe_token, _external=True)
            
            success, message = smtp_handler.send_email_sync(
                to_email=recipient.email,
                subject=p_subject,
                html_content=p_body_html,
                plain_content=p_body_plain,
                unsubscribe_url=unsubscribe_url,
                attachments=campaign.get_attachments()
            )
            
            if success: 
                recipient.status = 'Sent'
                recipient.sent_at = datetime.utcnow()
                recipient.status_message = "OK"
            else:
                recipient.status = 'Failed'
                recipient.status_message = message[:250] if message else "Unknown error"
            
            db.session.commit()
            
            return {"status": "sent" if success else "failed", "message": message}
        
        except Exception as e: 
            recipient.status = 'Failed'
            recipient.status_message = str(e)[:250]
            db.session.commit()
            return {"status": "error", "message": str(e)}


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
        
        for campaign in scheduled_campaigns: 
            log_activity(f"Starting scheduled campaign: {campaign.name}", "INFO")
            
            campaign.status = 'Sending'
            campaign.started_at = now
            db.session.commit()
            
            send_campaign_task.delay(campaign.id)
        
        return {"processed": len(scheduled_campaigns)}


@shared_task
def process_sequence_automation():
    """Process automated sequence steps."""
    app = get_app()
    
    with app.app_context():
        now = datetime.utcnow()
        
        # Get recipients ready for next step
        due_recipients = SequenceRecipient.query.filter(
            SequenceRecipient.status == 'Active',
            SequenceRecipient.next_action_at <= now
        ).limit(100).all()
        
        processed = 0
        
        for seq_recipient in due_recipients: 
            try:
                sequence = Sequence.query.get(seq_recipient.sequence_id)
                if not sequence:
                    seq_recipient.status = 'Error'
                    continue
                
                steps = sequence.get_steps()
                current_step = seq_recipient.current_step
                
                if current_step >= len(steps):
                    seq_recipient.status = 'Completed'
                    continue
                
                step = steps[current_step]
                
                if step['type'] == 'email':
                    # Send email step
                    # This would create a temporary campaign/recipient or use direct sending
                    pass
                
                elif step['type'] == 'wait':
                    # Calculate next action time
                    hours = float(step.get('duration', 24))
                    seq_recipient.next_action_at = now + timedelta(hours=hours)
                    seq_recipient.current_step += 1
                
                elif step['type'] == 'condition':
                    # Evaluate condition and branch
                    pass
                
                seq_recipient.last_action_at = now
                processed += 1
            
            except Exception as e: 
                log_activity(f"Sequence error for {seq_recipient.email}: {e}", "ERROR")
                seq_recipient.status = 'Error'
        
        db.session.commit()
        
        return {"processed": processed}


@shared_task
def check_imap_replies():
    """Check IMAP for replies to sent emails."""
    app = get_app()
    
    with app.app_context():
        import imaplib
        import email
        from email.header import decode_header
        
        # Get active SMTP profiles with IMAP configured
        profiles = SMTPServer.query.filter(
            SMTPServer.imap_server.isnot(None),
            SMTPServer.imap_server != ''
        ).all()
        
        total_replies = 0
        
        for profile in profiles:
            try: 
                imap_password = profile.get_imap_password()
                if not imap_password: 
                    continue
                
                mail = imaplib.IMAP4_SSL(profile.imap_server, profile.imap_port or 993)
                mail.login(profile.imap_username or profile.username, imap_password)
                mail.select("inbox")
                
                # Search for recent emails
                date_str = (datetime.now() - timedelta(days=1)).strftime("%d-%b-%Y")
                status, messages = mail.search(None, f'(SINCE "{date_str}")')
                
                if status != 'OK':
                    continue
                
                for num in messages[0].split()[-50:]:  # Last 50 messages
                    try:
                        status, msg_data = mail.fetch(num, '(RFC822)')
                        
                        for response_part in msg_data: 
                            if isinstance(response_part, tuple):
                                msg = email.message_from_bytes(response_part[1])
                                from_header = msg["From"]
                                
                                if from_header:
                                    # Extract email
                                    match = re.search(r'<(.+?)>', str(from_header))
                                    sender_email = match.group(1).lower() if match else str(from_header).lower()
                                    
                                    # Check if this is a reply from a recipient
                                    recipient = Recipient.query.filter_by(
                                        email=sender_email,
                                        status='Sent'
                                    ).order_by(Recipient.sent_at.desc()).first()
                                    
                                    if recipient and not recipient.replied_at:
                                        recipient.replied_at = datetime.utcnow()
                                        recipient.status = 'Replied'
                                        total_replies += 1
                                        
                                        log_activity(f"Reply detected from: {sender_email}", "SUCCESS")
                    except Exception: 
                        continue
                
                mail.close()
                mail.logout()
            
            except Exception as e: 
                log_activity(f"IMAP check error for {profile.profile_name}: {e}", "ERROR")
        
        db.session.commit()
        
        return {"replies_found": total_replies}


@shared_task
def cleanup_old_data():
    """Cleanup old logs, temporary data, etc."""
    app = get_app()
    
    with app.app_context():
        cutoff = datetime.utcnow() - timedelta(days=90)
        
        # Delete old webhook deliveries
        from app.models import WebhookDelivery
        deleted = WebhookDelivery.query.filter(
            WebhookDelivery.created_at < cutoff
        ).delete()
        
        db.session.commit()
        
        log_activity(f"Cleanup: Deleted {deleted} old webhook deliveries", "INFO")
        
        return {"deleted_webhook_deliveries": deleted}


@shared_task
def reset_daily_smtp_counts():
    """Reset daily send counts for SMTP profiles."""
    app = get_app()
    
    with app.app_context():
        today = datetime.utcnow().date()
        
        updated = SMTPServer.query.filter(
            SMTPServer.last_reset_date != today
        ).update({
            'sent_today': 0,
            'last_reset_date': today
        }, synchronize_session=False)
        
        db.session.commit()
        
        log_activity(f"Reset daily counts for {updated} SMTP profiles", "INFO")
        
        return {"profiles_reset": updated}


@shared_task
def generate_campaign_report(campaign_id, user_email):
    """Generate and email a campaign report."""
    app = get_app()
    
    with app.app_context():
        campaign = Campaign.query.get(campaign_id)
        if not campaign:
            return {"status": "error", "message": "Campaign not found"}
        
        analytics = campaign.get_analytics()
        
        # Generate report content
        report = f"""
Campaign Report: {campaign.name}
Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC

Summary:
- Total Recipients: {analytics['total']}
- Sent: {analytics['sent']}
- Opened: {analytics['opened']} ({analytics['open_rate']}%)
- Clicked: {analytics['clicked']} ({analytics['click_rate']}%)
- Bounced: {analytics['bounced']}
- Unsubscribed: {analytics['unsubscribed']}

Status: {campaign.status}
"""
        
        log_activity(f"Generated report for campaign {campaign.name}", "INFO")
        
        return {"status": "completed", "report": report}


def get_rotation_smtp_profiles(user_id):
    """Get all active SMTP profiles for rotation."""
    profiles = SMTPServer.query.filter_by(
        user_id=user_id,
        is_active=True
    ).order_by(SMTPServer.priority).all()
    
    valid_profiles = []
    
    for profile in profiles:
        profile.reset_daily_count_if_needed()
        
        password = profile.get_password()
        if not password: 
            continue
        
        if profile.sent_today >= profile.daily_limit:
            continue
        
        config = profile.to_dict()
        config['id'] = profile.id
        config['daily_limit'] = profile.daily_limit
        config['sent_today'] = profile.sent_today
        
        valid_profiles.append(config)
    
    return valid_profiles
