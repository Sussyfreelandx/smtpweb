import time
import logging
import traceback
from datetime import datetime, timedelta
from app import db, celery

logger = logging.getLogger(__name__)


def log_task(message, level="INFO"):
    """Log task activity with timestamp."""
    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] TASK-{level}: {message}")
    if level == "ERROR":
        logger.error(message)
    else:
        logger.info(message)


@celery.task(bind=True, name='app.tasks.send_campaign_task', max_retries=3, default_retry_delay=60)
def send_campaign_task(self, campaign_id):
    """
    Main Celery task to send a campaign.
    """
    from app import create_app
    from app.models import Campaign, Recipient, SMTPServer
    from app.core_logic.smtp_handler import SMTPHandler
    from app.core_logic.personalization import PersonalizationEngine
    
    log_task("=" * 60)
    log_task(f"📧 TASK RECEIVED: send_campaign_task")
    log_task(f"   Campaign ID: {campaign_id}")
    log_task(f"   Task ID: {self.request.id}")
    log_task("=" * 60)
    
    app = create_app()
    
    with app.app_context():
        try:
            # Load campaign
            campaign = Campaign.query.get(campaign_id)
            
            if not campaign:
                log_task(f"Campaign {campaign_id} not found", "ERROR")
                return {"status": "error", "message": "Campaign not found"}
            
            log_task(f"Campaign:  '{campaign.name}', Status: {campaign.status}")
            
            if campaign.status != 'Sending':
                log_task(f"Campaign not in 'Sending' status.  Current:  {campaign.status}", "WARNING")
                return {"status": "skipped", "message": f"Status is {campaign.status}"}
            
            # Get SMTP profile
            smtp_profile = campaign.smtp_profile
            if not smtp_profile:
                log_task("No SMTP profile assigned", "ERROR")
                campaign.status = 'Failed'
                db.session.commit()
                return {"status": "error", "message": "No SMTP profile"}
            
            log_task(f"SMTP Profile: {smtp_profile.profile_name}")
            
            # Get SMTP config
            smtp_config = smtp_profile.to_dict()
            if not smtp_config.get('password'):
                log_task("SMTP password not configured", "ERROR")
                campaign.status = 'Failed'
                db.session.commit()
                return {"status": "error", "message": "SMTP password missing"}
            
            log_task(f"SMTP Server: {smtp_config.get('server')}:{smtp_config.get('port')}")
            
            # Initialize SMTP handler
            try:
                smtp_handler = SMTPHandler(smtp_config)
                log_task("SMTP Handler initialized")
            except Exception as e: 
                log_task(f"SMTP Handler init failed: {e}", "ERROR")
                campaign.status = 'Failed'
                db.session.commit()
                return {"status": "error", "message": str(e)}
            
            # Sending config
            batch_size = campaign.throttle_amount or 20
            delay_seconds = campaign.throttle_delay or 60
            
            log_task(f"Config: batch={batch_size}, delay={delay_seconds}s")
            
            # Get attachments
            attachments = []
            if hasattr(campaign, 'get_attachments'):
                try:
                    attachments = campaign.get_attachments() or []
                except: 
                    pass
            
            total_sent = 0
            total_failed = 0
            batch_num = 0
            
            # Main sending loop
            while True:
                batch_num += 1
                
                # Refresh campaign
                db.session.expire_all()
                campaign = Campaign.query.get(campaign_id)
                
                if not campaign:
                    log_task("Campaign deleted during send", "ERROR")
                    break
                
                if campaign.status != 'Sending':
                    log_task(f"Status changed to '{campaign.status}'.  Stopping.", "WARNING")
                    break
                
                # Get queued recipients
                recipients = campaign.recipients.filter_by(status='Queued').limit(batch_size).all()
                
                if not recipients:
                    log_task("No more queued recipients.  Completing campaign.")
                    campaign.status = 'Completed'
                    campaign.completed_at = datetime.utcnow()
                    campaign.sent_count = total_sent
                    campaign.failed_count = total_failed
                    db.session.commit()
                    break
                
                log_task(f"--- Batch #{batch_num}:  {len(recipients)} recipients ---")
                
                # Process each recipient
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
                            log_task(f"Personalization error: {pe}", "WARNING")
                            subject = campaign.subject
                            body_html = campaign.body_html
                            body_plain = campaign.body_plain or ""
                        
                        # Send email
                        log_task(f"Sending to:  {recipient.email}")
                        
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
                            log_task(f"✅ Sent:  {recipient.email}")
                        else:
                            recipient.status = 'Failed'
                            recipient.status_message = str(error_msg)[:250] if error_msg else 'Unknown'
                            total_failed += 1
                            log_task(f"❌ Failed: {recipient.email} - {error_msg}", "ERROR")
                        
                        db.session.commit()
                        
                    except Exception as e: 
                        log_task(f"Exception for {recipient.email}: {e}", "ERROR")
                        recipient.status = 'Failed'
                        recipient.status_message = str(e)[:250]
                        total_failed += 1
                        db.session.commit()
                
                log_task(f"Batch #{batch_num} done. Sent: {total_sent}, Failed: {total_failed}")
                
                # Check remaining
                remaining = campaign.recipients.filter_by(status='Queued').count()
                
                if remaining > 0 and delay_seconds > 0:
                    log_task(f"Waiting {delay_seconds}s.  {remaining} remaining.")
                    time.sleep(delay_seconds)
            
            # Cleanup
            try:
                smtp_handler.disconnect()
            except:
                pass
            
            log_task("=" * 60)
            log_task(f"🏁 CAMPAIGN COMPLETE: {campaign.name if campaign else campaign_id}")
            log_task(f"   Sent: {total_sent}, Failed: {total_failed}")
            log_task("=" * 60)
            
            return {"status": "completed", "sent": total_sent, "failed":  total_failed}
            
        except Exception as e:
            log_task(f"💥 CRITICAL ERROR: {e}", "ERROR")
            log_task(traceback.format_exc(), "ERROR")
            
            try:
                db.session.rollback()
                campaign = Campaign.query.get(campaign_id)
                if campaign: 
                    campaign.status = 'Failed'
                    db.session.commit()
            except:
                pass
            
            raise self.retry(exc=e, countdown=60)


@celery.task(bind=True, name='app.tasks.send_single_email_task')
def send_single_email_task(self, recipient_id, campaign_id):
    """Send a single email."""
    from app import create_app
    from app.models import Campaign, Recipient
    from app.core_logic.smtp_handler import SMTPHandler
    from app.core_logic.personalization import PersonalizationEngine
    
    log_task(f"Single email:  recipient={recipient_id}, campaign={campaign_id}")
    
    app = create_app()
    
    with app.app_context():
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
            except: 
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
            handler.disconnect()
            
            return {"status": "sent" if success else "failed", "message": message}
            
        except Exception as e:
            log_task(f"Single email error: {e}", "ERROR")
            return {"status": "error", "message": str(e)}


@celery.task(name='app.tasks.process_scheduled_campaigns')
def process_scheduled_campaigns():
    """Check and start scheduled campaigns."""
    from app import create_app
    from app.models import Campaign
    
    log_task("Running process_scheduled_campaigns")
    
    app = create_app()
    
    with app.app_context():
        now = datetime.utcnow()
        scheduled = Campaign.query.filter(
            Campaign.status == 'Scheduled',
            Campaign.scheduled_at <= now
        ).all()
        
        count = 0
        for campaign in scheduled: 
            log_task(f"Starting scheduled:  {campaign.name}")
            campaign.status = 'Sending'
            campaign.started_at = now
            db.session.commit()
            send_campaign_task.delay(campaign.id)
            count += 1
        
        return {"processed": count}


@celery.task(name='app.tasks.reset_daily_smtp_counts')
def reset_daily_smtp_counts():
    """Reset daily SMTP counts."""
    from app import create_app
    from app.models import SMTPServer
    
    log_task("Running reset_daily_smtp_counts")
    
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
        return {"reset": updated}


@celery.task(name='app.tasks.process_sequence_automation')
def process_sequence_automation():
    """Process sequence automations."""
    return {"status": "ok"}


@celery.task(name='app.tasks.check_imap_replies')
def check_imap_replies():
    """Check IMAP replies."""
    return {"status": "checked"}


@celery.task(name='app.tasks.cleanup_old_data')
def cleanup_old_data():
    """Cleanup old data."""
    return {"status": "cleaned"}


@celery.task(name='app.tasks.generate_campaign_report')
def generate_campaign_report(campaign_id, user_email):
    """Generate campaign report."""
    return {"status": "completed"}
