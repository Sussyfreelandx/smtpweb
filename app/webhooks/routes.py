from flask import request, jsonify, current_app
from app import db
from app.webhooks import bp
from app.models import Webhook, WebhookDelivery, Recipient, Campaign
from app.utils import log_activity
from datetime import datetime
import hashlib
import hmac
import json
import requests
import threading


def verify_webhook_signature(payload, signature, secret):
    """Verify incoming webhook signature."""
    if not secret or not signature:
        return False
    
    expected_signature = hmac.new(
        secret.encode(),
        payload.encode(),
        hashlib.sha256
    ).hexdigest()
    
    return hmac.compare_digest(f"sha256={expected_signature}", signature)


def send_webhook(webhook, event, payload):
    """Send a webhook notification."""
    if not webhook.is_active:
        return False, "Webhook is inactive"
    
    headers = {
        'Content-Type': 'application/json',
        'X-Webhook-Event': event,
        'X-Webhook-Timestamp': datetime.utcnow().isoformat()
    }
    
    # Add signature if secret is configured
    if webhook.secret:
        payload_str = json.dumps(payload, sort_keys=True)
        signature = hmac.new(
            webhook.secret.encode(),
            payload_str.encode(),
            hashlib.sha256
        ).hexdigest()
        headers['X-Webhook-Signature'] = f"sha256={signature}"
    
    # Create delivery record
    delivery = WebhookDelivery(
        webhook_id=webhook.id,
        event=event,
        payload=json.dumps(payload)
    )
    
    try:
        start_time = datetime.utcnow()
        response = requests.post(
            webhook.url,
            json=payload,
            headers=headers,
            timeout=30
        )
        end_time = datetime.utcnow()
        
        delivery.status_code = response.status_code
        delivery.response_body = response.text[:1000] if response.text else None
        delivery.response_time_ms = int((end_time - start_time).total_seconds() * 1000)
        delivery.success = 200 <= response.status_code < 300
        
        if delivery.success:
            webhook.success_count += 1
        else:
            webhook.failure_count += 1
            delivery.error_message = f"HTTP {response.status_code}"
        
        webhook.last_triggered_at = datetime.utcnow()
        
    except requests.Timeout:
        delivery.success = False
        delivery.error_message = "Request timed out"
        webhook.failure_count += 1
        webhook.last_error = "Timeout"
    
    except requests.RequestException as e:
        delivery.success = False
        delivery.error_message = str(e)[:500]
        webhook.failure_count += 1
        webhook.last_error = str(e)[:255]
    
    except Exception as e:
        delivery.success = False
        delivery.error_message = str(e)[:500]
        webhook.failure_count += 1
    
    db.session.add(delivery)
    db.session.commit()
    
    return delivery.success, delivery.error_message


def trigger_webhooks_async(event, payload, user_id=None, team_id=None):
    """Trigger all webhooks for an event asynchronously."""
    def send_webhooks():
        from app import create_app
        app = create_app()
        
        with app.app_context():
            query = Webhook.query.filter_by(is_active=True)
            
            if user_id:
                query = query.filter_by(user_id=user_id)
            if team_id:
                query = query.filter_by(team_id=team_id)
            
            webhooks = query.all()
            
            for webhook in webhooks:
                events = webhook.get_events()
                if event in events or '*' in events:
                    send_webhook(webhook, event, payload)
    
    thread = threading.Thread(target=send_webhooks, daemon=True)
    thread.start()


def trigger_email_event(event_type, recipient, campaign=None):
    """Trigger webhook for email events."""
    if not campaign:
        campaign = recipient.campaign
    
    payload = {
        'event': event_type,
        'timestamp': datetime.utcnow().isoformat(),
        'data': {
            'recipient_id': recipient.id,
            'email': recipient.email,
            'campaign_id': campaign.id,
            'campaign_name': campaign.name
        }
    }
    
    if event_type == 'email.opened':
        payload['data']['opened_at'] = recipient.opened_at.isoformat() if recipient.opened_at else None
        payload['data']['open_count'] = recipient.open_count
    
    elif event_type == 'email.clicked':
        payload['data']['clicked_at'] = recipient.clicked_at.isoformat() if recipient.clicked_at else None
        payload['data']['click_count'] = recipient.click_count
        payload['data']['clicked_links'] = recipient.get_clicked_links()
    
    elif event_type == 'email.bounced':
        payload['data']['bounced_at'] = recipient.bounced_at.isoformat() if recipient.bounced_at else None
        payload['data']['bounce_reason'] = recipient.status_message
    
    elif event_type == 'email.unsubscribed':
        payload['data']['unsubscribed_at'] = recipient.unsubscribed_at.isoformat() if recipient.unsubscribed_at else None
    
    trigger_webhooks_async(event_type, payload, user_id=campaign.user_id)


def trigger_campaign_event(event_type, campaign):
    """Trigger webhook for campaign events."""
    payload = {
        'event': event_type,
        'timestamp': datetime.utcnow().isoformat(),
        'data': {
            'campaign_id': campaign.id,
            'campaign_name': campaign.name,
            'status': campaign.status,
            'analytics': campaign.get_analytics()
        }
    }
    
    trigger_webhooks_async(event_type, payload, user_id=campaign.user_id)


# ==================== INCOMING WEBHOOK ENDPOINTS ====================

@bp.route('/incoming/bounce', methods=['POST'])
def incoming_bounce():
    """Handle incoming bounce notifications from email providers."""
    try:
        data = request.get_json()
        
        if not data:
            return jsonify({'error': 'No data provided'}), 400
        
        # Handle different provider formats
        email = None
        bounce_type = 'hard'
        message = ''
        
        # Generic format
        if 'email' in data:
            email = data['email']
            bounce_type = data.get('type', 'hard')
            message = data.get('message', '')
        
        # AWS SES format
        elif 'bounce' in data:
            bounce_data = data['bounce']
            if 'bouncedRecipients' in bounce_data: 
                for recipient in bounce_data['bouncedRecipients']:
                    email = recipient.get('emailAddress')
                    break
            bounce_type = bounce_data.get('bounceType', 'Permanent').lower()
        
        # SendGrid format
        elif 'sg_event_id' in data or 'event' in data:
            email = data.get('email')
            bounce_type = 'hard' if data.get('event') == 'bounce' else 'soft'
            message = data.get('reason', '')
        
        if email:
            # Find and update recipient
            recipients = Recipient.query.filter_by(email=email.lower()).order_by(
                Recipient.sent_at.desc()
            ).limit(5).all()
            
            for recipient in recipients:
                if recipient.status == 'Sent':
                    recipient.status = 'Bounced'
                    recipient.bounced_at = datetime.utcnow()
                    recipient.status_message = message[:255] if message else f'{bounce_type} bounce'
                    
                    # Trigger webhook
                    trigger_email_event('email.bounced', recipient)
                    break
            
            db.session.commit()
            
            log_activity(f"Webhook: Bounce received for {email}", "WARNING")
        
        return jsonify({'message': 'Bounce processed'}), 200
    
    except Exception as e: 
        current_app.logger.error(f"Bounce webhook error: {e}")
        return jsonify({'error': str(e)}), 500


@bp.route('/incoming/complaint', methods=['POST'])
def incoming_complaint():
    """Handle incoming complaint/spam notifications."""
    try:
        data = request.get_json()
        
        if not data: 
            return jsonify({'error': 'No data provided'}), 400
        
        email = data.get('email')
        
        if email:
            email = email.lower()
            
            # Add to suppression list
            from app.models import Suppression
            if not Suppression.query.filter_by(email=email).first():
                suppression = Suppression(
                    email=email,
                    reason='Spam complaint',
                    source='webhook'
                )
                db.session.add(suppression)
            
            # Update any sent recipients
            recipients = Recipient.query.filter_by(email=email, status='Sent').all()
            for recipient in recipients:
                recipient.status = 'Complained'
            
            db.session.commit()
            
            log_activity(f"Webhook: Complaint received for {email}", "ERROR")
        
        return jsonify({'message': 'Complaint processed'}), 200
    
    except Exception as e:
        current_app.logger.error(f"Complaint webhook error: {e}")
        return jsonify({'error': str(e)}), 500


@bp.route('/test', methods=['POST'])
def test_webhook():
    """Test endpoint for webhook configuration."""
    return jsonify({
        'message': 'Webhook test successful',
        'timestamp': datetime.utcnow().isoformat(),
        'headers': dict(request.headers),
        'body': request.get_json()
    })