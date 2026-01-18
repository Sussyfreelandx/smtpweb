import asyncio
import json
import random
from datetime import datetime
from celery import shared_task
from app import db
from app.models import Campaign, Recipient, SmtpProfile
from app.main.smtp_handler import SMTPHandler
from app.main.personalization import personalize_content
from app.main.deliverability import DeliverabilityHelper

# --- Campaign Sending Tasks ---

@shared_task(bind=True)
def send_campaign_task(self, campaign_id):
    """
    Celery task to send a whole campaign by queuing individual email tasks.
    """
    campaign = Campaign.query.get(campaign_id)
    if not campaign:
        return {'status': 'Error', 'message': 'Campaign not found.'}

    # Get all valid SMTP profiles for rotation
    profile_ids = [int(pid) for pid in campaign.smtp_profile_ids.split(',') if pid]
    smtp_profiles = SmtpProfile.query.filter(SmtpProfile.id.in_(profile_ids)).all()
    if not smtp_profiles:
        # TODO: Mark campaign as failed
        return {'status': 'Error', 'message': 'No valid SMTP profiles configured for this campaign.'}
    
    # Get recipients that are ready to be sent
    recipients = campaign.recipients.filter(Recipient.status.in_(['Queued', 'Failed'])).all()

    # Create a rotating list of SMTP profile dicts
    smtp_configs = [
        {
            'id': p.id, 'server': p.server, 'port': p.port, 'username': p.username,
            'password': p.password, 'sender_name': p.sender_name, 'sender_email': p.sender_email,
            'use_tls': p.use_tls, 'use_ssl': p.use_ssl
        } for p in smtp_profiles
    ]
    
    for i, recipient in enumerate(recipients):
        # Rotate through the SMTP configs for each recipient
        smtp_config_to_use = smtp_configs[i % len(smtp_configs)]
        
        # Send each email as its own task for better concurrency
        send_single_email_task.delay(recipient.id, campaign.id, smtp_config_to_use)
        
    return {'status': 'Success', 'message': f'Queued {len(recipients)} emails for sending.'}

@shared_task(bind=True, autoretry_for=(Exception,), retry_kwargs={'max_retries': 3, 'countdown': 60})
def send_single_email_task(self, recipient_id, campaign_id, smtp_config):
    """
    Celery task that sends one email, handling A/B testing and personalization.
    """
    recipient = Recipient.query.get(recipient_id)
    campaign = Campaign.query.get(campaign_id)
    if not recipient or not campaign or recipient.status not in ['Queued', 'Failed', 'Sending']:
        return
        
    try:
        # Mark as 'Sending'
        recipient.status = 'Sending'
        db.session.commit()

        # --- A/B Testing Logic ---
        use_version_b = False
        if campaign.is_ab_test:
            # Use a simple hash of the email address for consistent A/B splitting
            if (hash(recipient.email) % 100) >= campaign.ab_split_ratio:
                use_version_b = True

        subject = campaign.subject_b if use_version_b else campaign.subject_a
        body_html = campaign.body_html_b if use_version_b else campaign.body_html_a
        recipient.version_sent = 'B' if use_version_b else 'A'
        
        # --- Personalization & Content Processing ---
        helper = DeliverabilityHelper()
        spun_subject = helper.spin(subject)
        spun_body = helper.spin(body_html)

        # Load recipient's data for personalization
        recipient_data = json.loads(recipient.data) if recipient.data else {}

        # Get personalized content
        final_subject, final_body, _ = personalize_content(
            email=recipient.email,
            subject=spun_subject,
            content=spun_body,
            recipient_data=recipient_data,
            smtp_sender_name=smtp_config['sender_name']
        )
        
        # --- SMTP Sending ---
        smtp_handler = SMTPHandler(smtp_config)
        
        # Use asyncio.run to execute the async function in the synchronous Celery worker
        success, message = asyncio.run(smtp_handler.send_email_async(
            to_email=recipient.email,
            subject=final_subject,
            html_content=final_body
            # TODO: Add unsubscribe and tracking URLs
        ))

        # --- Update Recipient Status ---
        if success:
            recipient.status = 'Sent'
            recipient.sent_at = datetime.utcnow()
            recipient.status_message = None
        else:
            recipient.status = 'Failed'
            recipient.status_message = message[:250] # Truncate message if too long

    except Exception as e:
        recipient.status = 'Failed'
        recipient.status_message = f"Critical Error: {str(e)[:150]}"
        raise self.retry(exc=e) # Retry the task on failure
    finally:
        db.session.commit()


# --- Utility Tasks ---

@shared_task(bind=True)
def test_smtp_task(self, smtp_config):
    """Celery task to test an SMTP connection without blocking the web server."""
    self.update_state(state='PROGRESS', meta={'status': 'Testing...', 'result': ''})
    handler = SMTPHandler(smtp_config)
    success, message = handler.test_connection()
    return {'status': 'Complete', 'result': 'success' if success else 'failed', 'message': message}
