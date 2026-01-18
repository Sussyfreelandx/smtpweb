import time
import redis
import os
from flask import current_app
from app import celery, db, create_app
from app.models import Campaign, Recipient, SMTPServer
from app.core_logic.smtp_handler import SMTPHandler
from app.core_logic.personalization import PersonalizationEngine
from datetime import datetime

# Initialize Redis connection for state management
redis_client = redis.from_url(os.environ.get('REDIS_URL', 'redis://localhost:6379/0'))

@celery.task(bind=True)
def send_campaign_task(self, campaign_id):
    """
    Main orchestration task. Handles throttling logic and dispatching.
    """
    app = create_app()
    with app.app_context():
        campaign = Campaign.query.get(campaign_id)
        if not campaign:
            return

        # Set Status to Running
        campaign.status = 'Running'
        db.session.commit()
        
        # Reset Pause/Stop keys
        redis_key_pause = f"campaign_{campaign_id}_pause"
        redis_key_stop = f"campaign_{campaign_id}_stop"
        redis_client.delete(redis_key_pause)
        redis_client.delete(redis_key_stop)

        # Get settings
        throttle_amount = campaign.throttle_amount or 20
        throttle_delay = campaign.throttle_delay or 1
        throttle_unit = campaign.throttle_unit or 'Minutes'
        delay_seconds = throttle_delay * 60 if throttle_unit == 'Minutes' else throttle_delay
        
        recipients = campaign.recipients.filter_by(status='Queued').all()
        total_recipients = len(recipients)
        
        batch_count = 0
        
        for recipient in recipients:
            # CHECK STOP
            if redis_client.get(redis_key_stop):
                campaign.status = 'Stopped'
                db.session.commit()
                return "Campaign Stopped by User"

            # CHECK PAUSE
            while redis_client.get(redis_key_pause):
                campaign.status = 'Paused'
                db.session.commit()
                time.sleep(5) # Wait 5 seconds and check again
                # Verify stop hasn't been pressed while paused
                if redis_client.get(redis_key_stop):
                    campaign.status = 'Stopped'
                    db.session.commit()
                    return "Campaign Stopped by User"
            
            # If we resumed, ensure status is Running
            if campaign.status == 'Paused':
                campaign.status = 'Running'
                db.session.commit()

            # Dispatch Single Email Task (Parallel Execution handled by Celery Workers)
            send_single_email_task.delay(recipient.id)
            batch_count += 1

            # Throttling Logic
            if batch_count >= throttle_amount:
                # Wait before sending next batch
                time.sleep(delay_seconds)
                batch_count = 0
        
        # Final Check (Since tasks are async, this might update before all are physically sent,
        # but it indicates dispatch is done. A separate monitor task is usually better for 'Completed'.)
        campaign.status = 'Completed'
        db.session.commit()

@celery.task(bind=True, autoretry_for=(Exception,), retry_kwargs={'max_retries': 2, 'countdown': 10})
def send_single_email_task(self, recipient_id):
    """
    Worker task to send a single email.
    """
    app = create_app()
    with app.app_context():
        recipient = Recipient.query.get(recipient_id)
        if not recipient or recipient.status not in ['Queued', 'Failed']: # Allow retry
            return

        recipient.status = 'Sending'
        db.session.commit()
        
        try:
            campaign = recipient.campaign
            smtp_profile = campaign.smtp_profile
            
            if not smtp_profile:
                raise Exception("No SMTP Profile linked")

            # Initialize Handler with Profile
            smtp_handler = SMTPHandler(smtp_profile.to_dict())
            
            # Personalize
            personalizer = PersonalizationEngine(campaign, recipient)
            p_subject, p_body = personalizer.personalize()
            
            # Send (Sync SMTP within this worker thread)
            unsubscribe_link = url_for('core_logic.unsubscribe', campaign_id=campaign.id, recipient_id=recipient.id, _external=True)
            
            success, message = smtp_handler.send_email_sync(
                to_email=recipient.email,
                subject=p_subject,
                html_content=p_body,
                unsubscribe_url=unsubscribe_link
            )

            if success:
                recipient.status = 'Sent'
                recipient.sent_at = datetime.utcnow()
                recipient.status_message = "Sent via SMTP"
            else:
                recipient.status = 'Failed'
                recipient.status_message = message
                
        except Exception as e:
            recipient.status = 'Failed'
            recipient.status_message = str(e)
            # Re-raise to trigger Celery retry if configured
            raise e
        finally:
            db.session.commit()

@celery.task
def test_smtp_connection_task(profile_id):
    """Background task to test SMTP."""
    app = create_app()
    with app.app_context():
        profile = SMTPServer.query.get(profile_id)
        if not profile:
            return {'success': False, 'message': 'Profile not found'}
        
        handler = SMTPHandler(profile.to_dict())
        success, msg = handler.test_connection()
        return {'success': success, 'message': msg}
