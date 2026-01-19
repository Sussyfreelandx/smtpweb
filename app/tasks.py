import time
from datetime import datetime
from flask import url_for
from celery import group, shared_task
from app import db, create_app
from app.models import Campaign, Recipient
from app.core_logic.smtp_handler import SMTPHandler
from app.core_logic.personalization import PersonalizationEngine
from app.utils import log_activity

app = create_app()
app.app_context().push()

@shared_task(bind=True)
def send_campaign_task(self, campaign_id):
    """
    Orchestrator: Batches recipients and delegates to workers.
    """
    with app.app_context():
        campaign = Campaign.query.get(campaign_id)
        if not campaign or campaign.status != 'Sending': return

        # Configuration
        batch_size = campaign.throttle_amount or 20
        delay_seconds = campaign.throttle_delay or 60
        
        log_activity(f"Starting campaign '{campaign.name}'. Batch: {batch_size}", "INFO")

        recipients = campaign.recipients.filter_by(status='Queued').all()
        total = len(recipients)
        
        if total == 0:
            campaign.status = 'Completed'
            db.session.commit()
            return

        # Process in batches
        for i in range(0, total, batch_size):
            db.session.refresh(campaign)
            if campaign.status != 'Sending':
                log_activity(f"Campaign '{campaign.name}' paused/stopped.", "WARNING")
                break

            batch = recipients[i : i + batch_size]
            batch_ids = [r.id for r in batch]
            
            log_activity(f"Processing batch {i//batch_size + 1}: {len(batch)} emails.", "INFO")
            
            # Send the entire batch IDs to a single worker for connection reuse
            send_batch_task.delay(campaign_id, batch_ids)
            
            if i + batch_size < total:
                time.sleep(delay_seconds)

        # Final Status Check
        db.session.refresh(campaign)
        remaining = campaign.recipients.filter_by(status='Queued').count()
        if remaining == 0 and campaign.status == 'Sending':
            campaign.status = 'Completed'
            db.session.commit()
            log_activity(f"Campaign '{campaign.name}' completed.", "SUCCESS")

@shared_task(bind=True)
def send_batch_task(self, campaign_id, recipient_ids):
    """
    Worker: Opens ONE SMTP connection and sends to multiple recipients.
    This drastically improves speed and reliability.
    """
    with app.app_context():
        campaign = Campaign.query.get(campaign_id)
        if not campaign: return

        smtp_profile = campaign.smtp_profile
        if not smtp_profile:
            log_activity("Worker failed: No SMTP profile.", "ERROR")
            return

        # Initialize SMTP Handler ONCE
        config = smtp_profile.to_dict()
        smtp_handler = SMTPHandler(config)
        
        # Connect once
        connected, error_msg = smtp_handler.connect()
        if not connected:
            log_activity(f"Worker SMTP Connection Failed: {error_msg}", "ERROR")
            # Mark all in batch as failed so they don't hang
            Recipient.query.filter(Recipient.id.in_(recipient_ids)).update(
                {'status': 'Failed', 'status_message': f'SMTP Connect Error: {error_msg}'},
                synchronize_session=False
            )
            db.session.commit()
            return

        # Send loop reuse connection
        for rid in recipient_ids:
            recipient = Recipient.query.get(rid)
            if not recipient or recipient.status != 'Queued': continue

            # Check pause status periodically
            if campaign.status != 'Sending': break

            recipient.status = 'Sending'
            db.session.commit()

            try:
                personalizer = PersonalizationEngine(campaign, recipient)
                p_subject, p_body = personalizer.personalize()
                
                unsub_token = recipient.get_tracking_token('unsubscribe')
                unsub_url = url_for('main.unsubscribe', token=unsub_token, _external=True)

                # Use the existing connection
                success, msg = smtp_handler.send_message_existing_conn(
                    recipient.email, p_subject, p_body, unsub_url
                )

                if success:
                    recipient.status = 'Sent'
                    recipient.sent_at = datetime.now()
                    recipient.status_message = "OK"
                else:
                    recipient.status = 'Failed'
                    recipient.status_message = msg
            except Exception as e:
                recipient.status = 'Failed'
                recipient.status_message = str(e)
            
            db.session.commit()

        # Close connection after batch
        smtp_handler.quit()
