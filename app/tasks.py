import time
import logging
import traceback
from datetime import datetime, timedelta
from app import db, celery

logger = logging.getLogger(__name__)


def log_task(message, level="INFO"):
    """Helper to log task activity with timestamp."""
    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    formatted = f"[{timestamp}] TASK-{level}:  {message}"
    print(formatted)
    
    if level == "ERROR":
        logger.error(message)
    elif level == "WARNING":
        logger.warning(message)
    else:
        logger.info(message)


@celery.task(bind=True, name='app.tasks.send_campaign_task', max_retries=3, default_retry_delay=60)
def send_campaign_task(self, campaign_id):
    """
    Main Celery task to send a campaign.
    This task is triggered when user clicks START on a campaign.
    """
    from app import create_app
    from app.models import Campaign, Recipient, SMTPServer
    from app.core_logic.smtp_handler import SMTPHandler
    from app.core_logic.personalization import PersonalizationEngine
    
    log_task(f"="*60)
    log_task(f"📧 TASK RECEIVED: send_campaign_task")
    log_task(f"   Campaign ID: {campaign_id}")
    log_task(f"   Task ID: {self.request.id}")
    log_task(f"="*60)
    
    app = create_app()
    
    with app.app_context():
        try:
            # Load campaign
            campaign = Campaign.query.get(campaign_id)
            
            if not campaign:
                log_task(f"Campaign {campaign_id} not found in database", "ERROR")
                return {"status": "error", "message": "Campaign not found"}
            
            log_task(f"Campaign loaded: '{campaign.name}'")
            log_task(f"Current status: {campaign.status}")
            
            # Verify campaign is in Sending status
            if campaign.status != 'Sending':
                log_task(f"Campaign is not in 'Sending' status.  Current:  {campaign.status}", "WARNING")
                return {"status": "skipped", "message": f"Campaign status is {campaign.status}, not Sending"}
            
            # Get SMTP profile
            smtp_profile = campaign.smtp_profile
            if not smtp_profile:
                log_task("No SMTP profile assigned to campaign", "ERROR")
                campaign.status = 'Failed'
                db.session.commit()
                return {"status": "error", "message": "No SMTP profile assigned"}
            
            log_task(f"SMTP Profile: {smtp_profile.profile_name}")
            
            # Get SMTP configuration
            smtp_config = smtp_profile.to_dict()
            
            if not smtp_config.get('password'):
                log_task("SMTP password is not configured", "ERROR")
                campaign.status = 'Failed'
                db.session.commit()
                return {"status": "error", "message": "SMTP password not configured"}
            
            log_task(f"SMTP Server: {smtp_config.get('server')}:{smtp_config.get('port')}")
            
            # Initialize SMTP handler
            try:
                smtp_handler = SMTPHandler(smtp_config)
                log_task("SMTP Handler initialized successfully")
            except Exception as e:
                log_task(f"Failed to initialize SMTP Handler: {e}", "ERROR")
                campaign.status = 'Failed'
                db.session.commit()
                return {"status": "error", "message": f"SMTP init failed: {str(e)}"}
            
            # Get sending configuration
            batch_size = campaign.throttle_amount or 20
            delay_seconds = campaign.throttle_delay or 60
            
            log_task(f"Sending config: batch_size={batch_size}, delay={delay_seconds}s")
            
            # Get attachments if any
            attachments = []
            if hasattr(campaign, 'get_attachments'):
                try:
                    attachments = campaign.get_attachments() or []
                except: 
                    pass
            
            total_sent = 0
            total_failed = 0
            batch_number = 0
            
            # Main sending loop
            while True:
                batch_number += 1
                
                # Refresh campaign from database
                db.session.expire_all()
                campaign = Campaign.query.get(campaign_id)
                
                if not campaign:
                    log_task("Campaign was deleted during sending", "ERROR")
                    break
                
                if campaign.status != 'Sending':
                    log_task(f"Campaign status changed to '{campaign.status}'.  Stopping.", "WARNING")
                    break
                
                # Get next batch of queued recipients
                recipients = campaign.recipients.filter_by(status='Queued').limit(batch_size).all()
                
                if not recipients:
                    log_task("No more queued recipients. Campaign complete!")
                    campaign.status = 'Completed'
                    campaign.completed_at = datetime.utcnow()
                    campaign.sent_count = total_sent
                    campaign.failed_count = total_failed
                    db.session.commit()
                    break
                
                log_task(f"--- Batch #{batch_number}:  Processing {len(recipients)} recipients ---")
                
                # Process each recipient in batch
                for recipient in recipients: 
                    try:
                        # Update status to Sending
                        recipient.status = 'Sending'
                        recipient.attempts = (recipient.attempts or 0) + 1
                        db.session.commit()
                        
                        # Personalize content
                        try:
                            personalizer = PersonalizationEngine(campaign, recipient)
                            subject, body_html, body_plain = personalizer.personalize()
                        except Exception as e:
                            log_task(f"Personalization error for {recipient.email}: {e}", "WARNING")
                            subject = campaign.subject
                            body_html = campaign.body_html
                            body_plain = campaign.body_plain or ""
                        
                        # Send email
                        log_task(f"Sending to:  {recipient.email}")
                        
                        success, error_message = smtp_handler.send_email(
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
                            log_task(f"✅ SUCCESS: {recipient.email}")
                        else:
                            recipient.status = 'Failed'
                            recipient.status_message = str(error_message)[:250] if error_message else 'Unknown error'
                            total_failed += 1
                            log_task(f"❌ FAILED: {recipient.email} - {error_message}", "ERROR")
                        
                        db.session.commit()
                        
                    except Exception as e:
                        log_task(f"Exception sending to {recipient.email}: {e}", "ERROR")
                        recipient.status = 'Failed'
                        recipient.status_message = str(e)[:250]
                        total_failed += 1
                        db.session.commit()
                
                log_task(f"Batch #{batch_number} complete. Sent:  {total_sent}, Failed: {total_failed}")
                
                # Check if more recipients remain
                remaining = campaign.recipients.filter_by(status='Queued').count()
                
                if remaining > 0 and delay_seconds > 0:
                    log_task(f"Waiting {delay_seconds}s before next batch.  {remaining} recipients remaining.")
                    time.sleep(delay_seconds)
            
            # Cleanup
            try:
                smtp_handler.disconnect()
            except:
                pass
            
            log_task(f"="*60)
            log_task(f"🏁 CAMPAIGN FINISHED: {campaign.name}")
            log_task(f"   Total Sent: {total_sent}")
            log_task(f"   Total Failed: {total_failed}")
            log_task(f"   Final Status: {campaign.status}")
            log_task(f"="*60)
            
            return {
                "status": "completed",
                "campaign_id": campaign_id,
                "sent":  total_sent,
                "failed": total_failed
            }
            
        except Exception as e: 
            log_task(f"💥 CRITICAL ERROR in send_campaign_task: {e}", "ERROR")
            log_task(traceback.format_exc(), "ERROR")
            
            try:
                db.session.rollback()
                campaign = Campaign.query.get(campaign_id)
                if campaign: 
                    campaign.status = 'Failed'
                    db.session.commit()
            except:
                pass
            
            # Retry the task
            raise self.retry(exc=e, countdown=60)


@celery.task(bind=True, name='app.tasks.send_single_email_task')
def send_single_email_task(self, recipient_id, campaign_id):
    """Send a single email (used for testing or manual sends)."""
    from app import create_app
    from app.models import Campaign, Recipient
    from app.core_logic.smtp_handler import SMTPHandler
    from app.core_logic.personalization import PersonalizationEngine
    
    log_task(f"Single email task:  recipient_id={recipient_id}, campaign_id={campaign_id}")
    
    app = create_app()
    
    with app.app_context():
        try:
            recipient = Recipient.query.get(recipient_id)
            campaign = Campaign.query.get(campaign_id)
            
            if not recipient or not campaign:
                return {"status": "error", "message": "Recipient or campaign not found"}
            
            smtp_profile = campaign.smtp_profile
            if not smtp_profile:
                return {"status": "error", "message": "No SMTP profile"}
            
            config = smtp_profile.to_dict()
            if not config.get('password'):
                return {"status": "error", "message": "SMTP password missing"}
            
            handler = SMTPHandler(config)
            
            recipient.status = 'Sending'
            recipient.attempts = (recipient.attempts or 0) + 1
            db.session.commit()
            
            # Personalize
            try:
                engine = PersonalizationEngine(campaign, recipient)
                subject, body_html, body_plain = engine.personalize()
            except: 
                subject = campaign.subject
                body_html = campaign.body_html
                body_plain = campaign.body_plain or ""
            
            # Send
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
            log_task(f"Error in send_single_email_task: {e}", "ERROR")
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
            log_task(f"Starting scheduled campaign: {campaign.name}")
            campaign.status = 'Sending'
            campaign.started_at = now
            db.session.commit()
            
            send_campaign_task.delay(campaign.id)
            count += 1
        
        return {"processed": count}


@celery.task(name='app.tasks.reset_daily_smtp_counts')
def reset_daily_smtp_counts():
    """Reset daily SMTP send counts."""
    from app import create_app
    from app.models import SMTPServer
    
    log_task("Running reset_daily_smtp_counts")
    
    app = create_app()
    
    with app.app_context():
        today = datetime.utcnow().date()
        
        updated = SMTPServer.query.filter(
            SMTPServer.last_reset_date != today
        ).update(
            {'sent_today': 0, 'last_reset_date':  today},
            synchronize_session=False
        )
        
        db.session.commit()
        log_task(f"Reset {updated} SMTP profiles")
        
        return {"profiles_reset": updated}


@celery.task(name='app.tasks.process_sequence_automation')
def process_sequence_automation():
    """Process sequence automations."""
    log_task("Running process_sequence_automation")
    return {"status": "ok"}


@celery.task(name='app.tasks.check_imap_replies')
def check_imap_replies():
    """Check for IMAP replies."""
    log_task("Running check_imap_replies")
    return {"status":  "checked"}


@celery.task(name='app.tasks.cleanup_old_data')
def cleanup_old_data():
    """Cleanup old data."""
    log_task("Running cleanup_old_data")
    return {"status": "cleaned"}


@celery.task(name='app.tasks.generate_campaign_report')
def generate_campaign_report(campaign_id, user_email):
    """Generate campaign report."""
    log_task(f"Running generate_campaign_report for campaign {campaign_id}")
    return {"status": "completed"}
