import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import url_for
from app import celery, db, create_app
from app.models import Campaign, Recipient, SMTPServer
from app.core_logic.smtp_handler import SMTPHandler
from app.core_logic.personalization import PersonalizationEngine
from datetime import datetime

# Initialize app context for Celery workers
app = create_app()
app.app_context().push()

@celery.task(bind=True)
def send_campaign_task(self, campaign_id):
    """
    Orchestrates the sending process with Parallel Workers and Throttling.
    """
    with app.app_context():
        campaign = Campaign.query.get(campaign_id)
        if not campaign: return

        # Update status to Sending
        campaign.status = 'Sending'
        db.session.commit()

        # Get Queued Recipients
        recipients = campaign.recipients.filter_by(status='Queued').all()
        total_recipients = len(recipients)
        
        # Configuration
        max_workers = campaign.parallel_workers if campaign.parallel_workers > 0 else 1
        throttle_count = campaign.throttle_amount
        throttle_delay = campaign.throttle_delay
        if campaign.throttle_unit == 'Minutes':
            throttle_delay = throttle_delay * 60

        # SMTP Setup (Handle Rotation if needed later, for now uses primary)
        smtp_profile = campaign.smtp_profile
        if not smtp_profile:
            campaign.status = 'Failed'
            db.session.commit()
            return

        # Prepare Shared SMTP config for workers
        smtp_config = smtp_profile.to_dict()

        processed_count = 0
        
        # We process in chunks to handle throttling correctly
        # Create a thread pool
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = []
            
            for i, recipient in enumerate(recipients):
                # --- LIVE STATUS CHECK ---
                # We must refresh the campaign object from DB to check for Pause/Stop signals
                db.session.refresh(campaign)
                if campaign.status == 'Paused':
                    # Spin wait loop
                    while campaign.status == 'Paused':
                        time.sleep(2)
                        db.session.refresh(campaign)
                        if campaign.status == 'Stopped': break
                
                if campaign.status == 'Stopped':
                    print(f"Campaign {campaign_id} stopped by user.")
                    break

                # --- THROTTLING LOGIC ---
                if processed_count > 0 and throttle_count > 0:
                    if processed_count % throttle_count == 0:
                        print(f"Throttling: Sleeping for {throttle_delay} seconds...")
                        time.sleep(throttle_delay)

                # Submit task to thread pool
                futures.append(executor.submit(
                    _send_single_email_thread, 
                    recipient.id, 
                    campaign.id, 
                    smtp_config
                ))
                
                processed_count += 1

            # Wait for all threads to finish
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as e:
                    print(f"Thread error: {e}")

        # Final Status Update
        db.session.refresh(campaign)
        if campaign.status != 'Stopped':
            # Check if any are still queued (failed or skipped)
            remaining = campaign.recipients.filter_by(status='Queued').count()
            campaign.status = 'Completed' if remaining == 0 else 'Completed (Partial)'
        
        db.session.commit()

def _send_single_email_thread(recipient_id, campaign_id, smtp_config):
    """
    Worker function executed inside a thread.
    Needs its own app context because it's in a thread.
    """
    with app.app_context():
        recipient = Recipient.query.get(recipient_id)
        campaign = Campaign.query.get(campaign_id)
        
        if not recipient: return

        # Mark as sending
        recipient.status = 'Sending'
        db.session.commit()

        try:
            # Personalization
            personalizer = PersonalizationEngine(campaign, recipient)
            p_subject, p_body = personalizer.personalize()

            # Generate Unsubscribe Link
            unsub_token = recipient.get_tracking_token('unsubscribe')
            unsub_url = url_for('main.unsubscribe', token=unsub_token, _external=True)

            # Send via SMTP Handler
            # Note: SMTPHandler creates a new connection per send to be thread-safe 
            # or relies on internal locking if using a shared connection.
            # For robustness in high worker counts, new connection per email is safer 
            # unless using a connection pool library. Here we do new connection.
            handler = SMTPHandler(smtp_config)
            success, message = handler.send_email_sync(
                to_email=recipient.email,
                subject=p_subject,
                html_content=p_body,
                unsubscribe_url=unsub_url
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
