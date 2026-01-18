from app import celery, db, create_app
from app.models import Campaign, Recipient
from app.core_logic.smtp_handler import SMTPHandler
from app.core_logic.personalization import PersonalizationEngine
from flask import url_for
import time
import logging

# We do NOT create the app globally here to avoid circular imports during startup.
# Celery will use the app factory pattern via the worker command.

logger = logging.getLogger(__name__)

@celery.task(bind=True)
def send_campaign_task(self, campaign_id):
    # Create an app context manually for the task execution
    app = create_app()
    with app.app_context():
        campaign = Campaign.query.get(campaign_id)
        if not campaign:
            return "Campaign not found"
        
        logger.info(f"Starting campaign {campaign_id}: {campaign.name}")
        
        # Get pending recipients
        recipients = campaign.recipients.filter_by(status='Queued').all()
        total = len(recipients)
        
        # Throttling logic
        throttle_amount = campaign.throttle_amount
        throttle_delay = campaign.throttle_delay
        
        count = 0
        
        for r in recipients:
            # Check if campaign was paused/stopped
            db.session.refresh(campaign)
            if campaign.status == 'Paused':
                logger.info(f"Campaign {campaign_id} paused.")
                break
                
            # Send the email
            send_single_email_task.apply(args=(r.id,))
            count += 1
            
            # Handle Throttling
            if throttle_amount > 0 and throttle_delay > 0:
                if count % throttle_amount == 0:
                    logger.info(f"Throttling: Pausing for {throttle_delay} seconds...")
                    time.sleep(throttle_delay)
        
        # Check if done
        remaining = campaign.recipients.filter_by(status='Queued').count()
        if remaining == 0 and campaign.status != 'Paused':
            campaign.status = 'Completed'
            db.session.commit()

@celery.task(bind=True)
def send_single_email_task(self, recipient_id):
    # This task might be called individually or by the group task
    # If called by group task within the same process/worker, app context might persist,
    # but strictly for Celery, it's safer to ensure context.
    
    # Note: If using 'apply' synchronously above, we are already in context.
    # If using 'delay' (async), we need context.
    # To be safe, we check if current_app is available.
    from flask import current_app
    if not current_app:
        app = create_app()
        ctx = app.app_context()
        ctx.push()
    
    recipient = Recipient.query.get(recipient_id)
    if not recipient or recipient.status != 'Queued':
        return
        
    recipient.status = 'Sending'
    db.session.commit()
    
    try:
        campaign = recipient.campaign
        smtp_profile = campaign.smtp_profile
        
        if not smtp_profile:
            raise Exception("No SMTP Profile linked")
            
        smtp_handler = SMTPHandler(smtp_profile.to_dict())
        
        # Personalization
        engine = PersonalizationEngine(campaign, recipient)
        subject, body = engine.personalize()
        
        # Unsubscribe Link
        # Note: _external=True needs SERVER_NAME config in some Flask setups, 
        # or it relies on Request context which might be missing in Celery.
        # Fallback to a hardcoded base URL if needed.
        unsub_link = url_for('main.unsubscribe', recipient_id=recipient.id, campaign_id=campaign.id, _external=True)
        
        success, msg = smtp_handler.send_email_sync(
            to_email=recipient.email,
            subject=subject,
            html_content=body,
            unsubscribe_url=unsub_link
        )
        
        if success:
            recipient.status = 'Sent'
            recipient.sent_at = datetime.utcnow()
        else:
            recipient.status = 'Failed'
            recipient.status_message = msg
            
    except Exception as e:
        recipient.status = 'Failed'
        recipient.status_message = str(e)
        logger.error(f"Error sending to {recipient.email}: {e}")
        
    db.session.commit()
