import asyncio
import json
import re
from datetime import datetime

from app import celery, db, create_app
from app.models import Campaign, Recipient
from app.main.smtp_handler import SMTPHandler
from jinja2 import Environment, exceptions as jinja_exceptions

# This helper function allows tasks to have an application context
def with_app_context(f):
    def wrapper(*args, **kwargs):
        app = create_app()
        with app.app_context():
            return f(*args, **kwargs)
    return wrapper

@celery.task(bind=True)
@with_app_context
def send_campaign_task(self, campaign_id):
    """Celery task to send a whole campaign."""
    campaign = Campaign.query.get(campaign_id)
    if not campaign:
        return

    recipient_ids = [r.id for r in campaign.recipients.filter(Recipient.status.in_(['Queued', 'Failed']))]

    for recipient_id in recipient_ids:
        send_single_email_task.delay(recipient_id)

@celery.task(bind=True, autoretry_for=(Exception,), retry_kwargs={'max_retries': 3, 'countdown': 60})
@with_app_context
def send_single_email_task(self, recipient_id):
    """Celery task that sends one email, with retry logic."""
    recipient = Recipient.query.get(recipient_id)
    if not recipient or recipient.status not in ['Queued', 'Failed']:
        return

    campaign = recipient.campaign
    
    recipient.status = 'Sending'
    db.session.commit()

    smtp_config = {
        'server': campaign.smtp_server, 'port': campaign.smtp_port,
        'username': campaign.smtp_username, 'password': campaign.smtp_password,
        'sender_name': campaign.smtp_sender_name, 'sender_email': campaign.smtp_sender_email,
        'use_tls': True, 'use_ssl': (campaign.smtp_port == 465)
    }
    
    smtp_handler = SMTPHandler(smtp_config)
    
    # --- Personalization Logic (ported from desktop app) ---
    jinja_env = Environment()

    def personalize_content(email, subject, content, recipient_data):
        context = {k.lower(): v for k, v in recipient_data.items()}
        now = datetime.now()
        
        # Autograb Firstname
        found_name = context.get('firstname')
        if not found_name:
            local_part = email.split('@')[0]
            potential_parts = re.split(r'[._\-+]+', local_part)
            valid_parts = [p for p in potential_parts if len(p) > 1 and p.isalpha()]
            generic_words = {'info', 'contact', 'admin', 'support', 'sales', 'mail'}
            if valid_parts and valid_parts[0].lower() not in generic_words:
                found_name = valid_parts[0].capitalize()
                context['firstname'] = found_name
        
        # Greetings
        hour = now.hour
        if 5 <= hour < 12: base_greeting = "Good morning"
        elif 12 <= hour < 18: base_greeting = "Good afternoon"
        else: base_greeting = "Good evening"
        context['greetings'] = f"{base_greeting} {found_name}" if found_name else base_greeting
        context.setdefault('firstname', 'there')
        
        # Autograb Company
        if 'company' not in context:
            domain = email.split('@')[1].lower()
            common_isp_domains = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com"}
            if domain in common_isp_domains:
                context['company'] = "your team"
            else:
                company_part = domain.split('.')[0]
                context['company'] = company_part.capitalize()
        
        # Other dynamic fields
        context['sender_name'] = campaign.smtp_sender_name
        context['currentdate'] = now.strftime("%B %d, %Y")
        
        # TODO: Implement tracking and unsubscribe URLs
        context['unsubscribe_link'] = "#"

        try:
            subject_template = jinja_env.from_string(subject)
            personalized_subject = subject_template.render(context)
            content_template = jinja_env.from_string(content)
            personalized_content = content_template.render(context)
            return personalized_subject, personalized_content
        except jinja_exceptions.TemplateError as e:
            # Fallback if template is invalid
            return subject, content
    # --- End Personalization ---

    recipient_data = recipient.get_data()
    p_subject, p_body = personalize_content(recipient.email, campaign.subject, campaign.body_html, recipient_data)
    
    # Use asyncio.run to execute the async send function
    success, message = asyncio.run(smtp_handler.send_email_async(
        to_email=recipient.email,
        subject=p_subject,
        html_content=p_body
    ))

    if success:
        recipient.status = 'Sent'
        recipient.sent_at = datetime.utcnow()
    else:
        recipient.status = 'Failed'
        recipient.status_message = message

    db.session.commit()
