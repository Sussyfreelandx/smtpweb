import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import url_for
from app import celery, db, create_app
from app.models import Campaign, Recipient, SMTPServer, ActivityLog
from app.core_logic.smtp_handler import SMTPHandler
from app.core_logic.personalization import PersonalizationEngine
from datetime import datetime

app = create_app()
app.app_context().push()

def _log_to_db(campaign_id, message, log_type='info'):
    """Helper to write logs to DB within task context."""
    try:
        # Create new connection/session for logging to avoid threading issues
        with app.app_context():
            log = ActivityLog(campaign_id=campaign_id, message=message, log_type=log_type)
            db.session.add(log)
            db.session.commit()
    except Exception as e:
        print(f"Logging failed: {e}")

@celery.task(bind=True)
def send_campaign_task(self, campaign_id):
    """
    Orchestrates the sending process with Parallel Workers and Throttling.
    """
    with app.app_context():
        campaign = Campaign.query.get(campaign_id)
        if not campaign: return

        campaign.status = 'Sending'
        db.session.commit()
        _log_to_db(campaign_id, "🚀 Campaign sending started.", "info")

        recipients = campaign.recipients.filter_by(status='Queued').all()
        total_recipients = len(recipients)
        
        max_workers = campaign.parallel_workers if campaign.parallel_workers > 0 else 1
        throttle_count = campaign.throttle_amount
        throttle_delay = campaign.throttle_delay
        if campaign.throttle_unit == 'Minutes':
            throttle_delay = throttle_delay * 60

        smtp_profile = campaign.smtp_profile
        if not smtp_profile:
            campaign.status = 'Failed'
            db.session.commit()
            _log_to_db(campaign_id, "❌ Critical: No SMTP Profile found. Stopping.", "error")
            return

        smtp_config = smtp_profile.to_dict()
        _log_to_db(campaign_id, f"Using SMTP Profile: {smtp_profile.profile_name} with {max_workers} workers.", "info")

        processed_count = 0
        
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = []
            
            for i, recipient in enumerate(recipients):
                # Status Check
                db.session.refresh(campaign)
                if campaign.status == 'Paused':
                    _log_to_db(campaign_id, "⏸️ Campaign Paused. Waiting...", "warning")
                    while campaign.status == 'Paused':
                        time.sleep(2)
                        db.session.refresh(campaign)
                        if campaign.status == 'Stopped': break
                    if campaign.status != 'Stopped':
                        _log_to_db(campaign_id, "▶️ Campaign Resumed.", "info")
                
                if campaign.status == 'Stopped':
                    _log_to_db(campaign_id, "⏹️ Campaign Stopped by user.", "error")
                    break

                # Throttling
                if processed_count > 0 and throttle_count > 0:
                    if processed_count % throttle_count == 0:
                        _log_to_db(campaign_id, f"⏱️ Throttling: Pausing for {throttle_delay}s...", "warning")
                        time.sleep(throttle_delay)

                futures.append(executor.submit(
                    _send_single_email_thread, 
                    recipient.id, 
                    campaign.id, 
                    smtp_config
                ))
                
                processed_count += 1

            for future in as_completed(futures):
                try: future.result()
                except Exception as e: 
                    pass

        db.session.refresh(campaign)
        if campaign.status != 'Stopped':
            remaining = campaign.recipients.filter_by(status='Queued').count()
            if remaining == 0:
                campaign.status = 'Completed'
                _log_to_db(campaign_id, "🏁 Campaign Completed Successfully.", "success")
            else:
                campaign.status = 'Completed (Partial)'
                _log_to_db(campaign_id, "🏁 Campaign Completed (Partial). Some items failed or were skipped.", "warning")
        
        db.session.commit()

def _send_single_email_thread(recipient_id, campaign_id, smtp_config):
    with app.app_context():
        recipient = Recipient.query.get(recipient_id)
        campaign = Campaign.query.get(campaign_id)
        if not recipient: return

        recipient.status = 'Sending'
        db.session.commit()

        try:
            personalizer = PersonalizationEngine(campaign, recipient)
            p_subject, p_body = personalizer.personalize()

            unsub_token = recipient.get_tracking_token('unsubscribe')
            unsub_url = url_for('main.unsubscribe', token=unsub_token, _external=True)

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
                _log_to_db(campaign_id, f"📤 Sent to: {recipient.email}", "success")
            else:
                recipient.status = 'Failed'
                recipient.status_message = message
                _log_to_db(campaign_id, f"❌ Failed: {recipient.email} - {message}", "error")

        except Exception as e:
            recipient.status = 'Failed'
            recipient.status_message = str(e)
            _log_to_db(campaign_id, f"❌ Error processing {recipient.email}: {e}", "error")

        db.session.commit()
