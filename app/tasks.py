import asyncio
import json
from datetime import datetime
from celery.utils.log import get_task_logger

from app import celery, db
from app.models import Campaign, Recipient
from core_logic.smtp_handler import SMTPHandler
from core_logic.personalization import build_personalization_context, personalize_content

import css_inline

logger = get_task_logger(__name__)

@celery.task(bind=True)
def send_campaign_task(self, campaign_id):
    """Celery task to queue up sending for a whole campaign."""
    campaign = db.session.get(Campaign, campaign_id)
    if not campaign:
        logger.warning(f"Campaign {campaign_id} not found.")
        return 'Campaign not found'

    recipient_ids = [r.id for r in campaign.recipients.filter(Recipient.status.in_(['Queued', 'Failed']))]
    logger.info(f"Queuing {len(recipient_ids)} emails for campaign {campaign.name} ({campaign_id}).")

    for recipient_id in recipient_ids:
        send_single_email_task.delay(recipient_id)
    
    return f'Queued {len(recipient_ids)} emails for campaign {campaign_id}.'


@celery.task(bind=True, autoretry_for=(Exception,), retry_kwargs={'max_retries': 3, 'countdown': 60})
def send_single_email_task(self, recipient_id):
    """Celery task that sends one email, with full personalization and tracking."""
    recipient = db.session.get(Recipient, recipient_id)
    if not recipient:
        logger.warning(f"Recipient {recipient_id} not found in database.")
        return f'Recipient {recipient_id} not found.'
    
    if recipient.status == 'Unsubscribed':
        return f'Skipping unsubscribed recipient {recipient.email}.'

    campaign = recipient.campaign
    
    recipient.status = 'Sending'
    db.session.commit()

    try:
        smtp_config = {
            'server': campaign.smtp_server, 'port': campaign.smtp_port,
            'username': campaign.smtp_username, 'password': campaign.smtp_password,
            'sender_name': campaign.smtp_sender_name, 'sender_email': campaign.smtp_sender_email,
            'use_tls': True, 'use_ssl': (campaign.smtp_port == 465),
        }
        smtp_handler = SMTPHandler(smtp_config)
        
        # --- 1. Build Personalization Context ---
        recipient_data = json.loads(recipient.data) if recipient.data else {}
        context = build_personalization_context(recipient.email, recipient_data)

        # --- 2. Add Tracking and Unsubscribe Links to Context ---
        base_url = "http://your-render-app-url.onrender.com" # IMPORTANT: Change this in production
        context['tracking_pixel_url'] = url_for('main.track_open', public_id=recipient.public_id, _external=True)
        context['unsubscribe_url'] = url_for('main.unsubscribe', public_id=recipient.public_id, _external=True)
        
        # --- 3. Personalize Content ---
        rendered_subject, rendered_body = personalize_content(campaign.subject, campaign.body_html, context)

        # --- 4. Add Tracking Links and Pixel to HTML Body ---
        # Wrap links
        def wrap_link(match):
            url = match.group(1)
            # Avoid wrapping already tracked links or unsubscribe links
            if 'track/click' in url or 'unsubscribe' in url:
                return match.group(0)
            
            click_track_url = url_for('main.track_click', public_id=recipient.public_id, url=url, _external=True)
            return f'href="{click_track_url}"'

        final_body = re.sub(r'href=["\'](https?://[^"\']+)["\']', wrap_link, rendered_body, flags=re.IGNORECASE)
        
        # Add tracking pixel
        pixel_img = f'<img src="{context["tracking_pixel_url"]}" width="1" height="1" alt="" style="display:none;"/>'
        if '</body>' in final_body.lower():
            final_body = final_body.replace('</body>', f'{pixel_img}</body>')
        else:
            final_body += pixel_img

        # --- 5. Inline CSS ---
        try:
            inliner = css_inline.CSSInliner()
            final_body = inliner.inline(final_body)
        except Exception as e:
            logger.warning(f"CSS inlining failed for recipient {recipient.id}: {e}")

        # --- 6. Send Email ---
        loop = asyncio.get_event_loop()
        success, message = loop.run_until_complete(smtp_handler.send_email_async(
            to_email=recipient.email,
            subject=rendered_subject,
            html_content=final_body,
            unsubscribe_url=context['unsubscribe_url']
        ))

        if success:
            recipient.status = 'Sent'
            recipient.sent_at = datetime.utcnow()
        else:
            recipient.status = 'Failed'
            recipient.status_message = message

        db.session.commit()
        return f"Processed recipient {recipient_id}: {recipient.status}"

    except Exception as e:
        logger.error(f"Error sending to recipient {recipient.id}: {e}", exc_info=True)
        recipient.status = 'Failed'
        recipient.status_message = str(e)
        db.session.commit()
        raise self.retry(exc=e)
