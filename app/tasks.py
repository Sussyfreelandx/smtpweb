import asyncio
from app import celery, db, create_app
from app.models import Campaign, Recipient, User
from app.core_logic.smtp_handler import SMTPHandler
from app.core_logic.personalization import PersonalizationEngine
from app.core_logic.ai_handler import AIHandler, LocalAIHandler
from app.core_logic.deliverability import DeliverabilityHelper
from flask import current_app
import json
import imaplib
import email
from email.header import decode_header
import re
from datetime import datetime, timedelta

@celery.task(bind=True)
def send_campaign_task(self, campaign_id):
    """Celery task to send a whole campaign."""
    app = create_app()
    with app.app_context():
        campaign = Campaign.query.get(campaign_id)
        if not campaign:
            return

        # Gather all recipient IDs for this campaign
        recipient_ids = [r.id for r in campaign.recipients.filter_by(status='Queued')]

        for recipient_id in recipient_ids:
            # Launch a separate task for each email
            send_single_email_task.delay(recipient_id)

@celery.task(bind=True)
def send_single_email_task(self, recipient_id):
    """Celery task that sends one email."""
    app = create_app()
    with app.app_context():
        recipient = Recipient.query.get(recipient_id)
        if not recipient or recipient.status != 'Queued':
            return

        campaign = recipient.campaign
        
        recipient.status = 'Sending'
        db.session.commit()

        smtp_config = {
            'server': campaign.smtp_server,
            'port': campaign.smtp_port,
            'username': campaign.smtp_username,
            'password': campaign.smtp_password,
            'sender_name': campaign.smtp_sender_name,
            'sender_email': campaign.smtp_sender_email,
            'use_tls': True,
            'use_ssl': False,
        }
        
        smtp_handler = SMTPHandler(smtp_config)
        
        # Initialize personalization engine
        engine = PersonalizationEngine(
            recipient_data=recipient.get_data(),
            sender_name=campaign.smtp_sender_name,
            base_url=current_app.config['BASE_URL']
        )
        
        # Personalize subject and body
        personalized_subject = engine.personalize(campaign.subject)
        
        # Add tracking to body before personalizing
        tracked_html_body = engine.add_tracking_to_content(campaign.body_html, campaign.id, recipient.id)
        personalized_body = engine.personalize(tracked_html_body)

        success, message = asyncio.run(smtp_handler.send_email_async(
            to_email=recipient.email,
            subject=personalized_subject,
            html_content=personalized_body,
            unsubscribe_url=engine.get_unsubscribe_url(campaign.id, recipient.id)
        ))

        if success:
            recipient.status = 'Sent'
            recipient.sent_at = datetime.utcnow()
        else:
            recipient.status = 'Failed'
            recipient.status_message = message

        db.session.commit()

@celery.task(name='tasks.check_replies_task')
def check_replies_task(user_id):
    """
    Checks for replies for a given user's campaigns via IMAP.
    This should be run periodically.
    """
    app = create_app()
    with app.app_context():
        user = User.query.get(user_id)
        # Assuming you store IMAP credentials per user or in general config
        # Here we pull from the main config for simplicity
        imap_server = current_app.config.get('IMAP_SERVER')
        imap_port = current_app.config.get('IMAP_PORT')
        imap_user = current_app.config.get('IMAP_USERNAME')
        imap_pass = current_app.config.get('IMAP_PASSWORD')

        if not all([imap_server, imap_user, imap_pass]):
            current_app.logger.warning(f"IMAP settings not configured for user {user_id}. Skipping reply check.")
            return "IMAP not configured."

        try:
            mail = imaplib.IMAP4_SSL(imap_server, imap_port)
            mail.login(imap_user, imap_pass)
            mail.select("inbox")

            # Search for emails since the last check (e.g., last 24 hours)
            date_str = (datetime.now() - timedelta(days=1)).strftime("%d-%b-%Y")
            status, messages = mail.search(None, f'(SINCE "{date_str}")')
            if status != 'OK':
                return "Failed to search inbox."

            found_count = 0
            for num in messages[0].split():
                try:
                    status, msg_data = mail.fetch(num, '(RFC822)')
                    for response_part in msg_data:
                        if isinstance(response_part, tuple):
                            msg = email.message_from_bytes(response_part[1])
                            from_header = decode_header(msg["From"])[0][0]
                            from_header_str = from_header.decode() if isinstance(from_header, bytes) else from_header
                            
                            sender_match = re.search(r'<(.+?)>', from_header_str)
                            sender_email = sender_match.group(1).lower() if sender_match else from_header_str.lower()
                            
                            # Find recipient in the database
                            replied_recipient = Recipient.query.join(Campaign).filter(
                                User.id == user_id,
                                Recipient.email == sender_email,
                                Recipient.status.in_(['Sent', 'Opened', 'Clicked'])
                            ).first()

                            if replied_recipient:
                                replied_recipient.status = 'Replied'
                                db.session.commit()
                                found_count += 1
                except Exception as e:
                    current_app.logger.error(f"Error processing single reply: {e}")
                    continue

            mail.close()
            mail.logout()
            return f"Found {found_count} replies."

        except Exception as e:
            current_app.logger.error(f"IMAP Check Error: {e}")
            return "IMAP connection failed."
