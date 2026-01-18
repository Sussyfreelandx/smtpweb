import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from flask import url_for
from app import celery, db, create_app
from app.models import Campaign, Recipient, Attachment
from app.core_logic.smtp_handler import SMTPHandler
from app.core_logic.personalization import PersonalizationEngine

# Create a standalone app context for workers
app = create_app()
app.app_context().push()

def check_campaign_status(campaign_id):
    """Refreshes campaign status from DB."""
    # We use a new session to ensure we get the latest data committed by the web UI
    with app.app_context():
        c = Campaign.query.get(campaign_id)
        return c.status if c else 'Stopped'

def process_single_recipient(recipient_id, smtp_config, subject_template, body_template, attachment_paths):
    """
    Worker function for ThreadPoolExecutor. 
    Handles personalization and sending for one recipient.
    """
    with app.app_context():
        recipient = Recipient.query.get(recipient_id)
        if not recipient or recipient.status != 'Queued':
            return

        recipient.status = 'Sending'
        db.session.commit()

        try:
            # Re-fetch campaign to get sender details specific to personalization
            campaign = recipient.campaign
            
            # Initialize Helpers
            smtp_handler = SMTPHandler(smtp_config)
            personalizer = PersonalizationEngine(campaign, recipient)
            
            # Personalize
            p_subject, p_body = personalizer.personalize()

            # Send
            unsubscribe_url = url_for('core_logic.unsubscribe', campaign_id=campaign.id, recipient_id=recipient.id, _external=True)
            
            success, message = smtp_handler.send_email_sync(
                to_email=recipient.email,
                subject=p_subject,
                html_content=p_body,
                unsubscribe_url=unsubscribe_url,
                attachments=attachment_paths
            )

            if success:
                recipient.status = 'Sent'
                recipient.sent_at = datetime.utcnow()
            else:
                recipient.status = 'Failed'
                recipient.status_message = message

        except Exception as e:
            recipient.status = 'Failed'
            recipient.status_message = str(e)
        
        db.session.commit()

@celery.task(bind=True)
def send_campaign_task(self, campaign_id):
    """
    Main background task. Manages the list, threads, throttling, and status checks.
    """
    with app.app_context():
        campaign = Campaign.query.get(campaign_id)
        if not campaign:
            return "Campaign not found"

        # Set status to Running
        campaign.status = 'Running'
        db.session.commit()

        # Gather Config
        smtp_config = campaign.smtp_profile.to_dict()
        attachment_paths = [a.file_path for a in campaign.attachments.all()]
        
        # Parallel & Throttle Settings
        max_workers = campaign.parallel_workers or 1
        throttle_amount = campaign.throttle_amount or 0
        throttle_interval = campaign.throttle_interval or 0

        # Get Queued Recipients
        recipients = campaign.recipients.filter_by(status='Queued').all()
        total_recipients = len(recipients)
        processed_count = 0

        # Create Thread Pool
        # Note: We use threads here because SMTP is I/O bound.
        executor = ThreadPoolExecutor(max_workers=max_workers)
        futures = []

        for recipient in recipients:
            # 1. Check for Pause/Stop signals
            current_status = check_campaign_status(campaign.id)
            
            if current_status == 'Stopped':
                # Cancel pending futures if possible and exit
                executor.shutdown(wait=False)
                return "Campaign Stopped by User"
            
            while current_status == 'Paused':
                time.sleep(2) # Wait loop
                current_status = check_campaign_status(campaign.id)
                if current_status == 'Stopped':
                    executor.shutdown(wait=False)
                    return "Campaign Stopped during Pause"

            # 2. Throttling Logic
            if throttle_amount > 0 and processed_count > 0:
                if processed_count % throttle_amount == 0:
                    # Wait for interval
                    time.sleep(throttle_interval)

            # 3. Submit Task to Thread Pool
            # We pass simple IDs and data structs to avoid threading issues with SQLAlchmey objects
            f = executor.submit(
                process_single_recipient, 
                recipient.id, 
                smtp_config, 
                campaign.subject, 
                campaign.body, 
                attachment_paths
            )
            futures.append(f)
            processed_count += 1

        # Wait for all emails to finish
        for future in as_completed(futures):
            pass # We can collect results here if needed

        executor.shutdown(wait=True)

        # Final Status Update
        # Re-fetch campaign to ensure we don't overwrite if user stopped it at the very end
        c_final = Campaign.query.get(campaign_id)
        if c_final.status == 'Running':
            c_final.status = 'Completed'
            db.session.commit()

    return f"Campaign {campaign_id} processing finished."
