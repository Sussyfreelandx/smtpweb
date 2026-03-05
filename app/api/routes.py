from flask import request, jsonify, current_app, g
from functools import wraps
from app import db
from app.api import bp
from app.models import (
    APIKey, User, Campaign, Recipient, SMTPServer, Suppression,
    Tag, Segment, Webhook, WebhookDelivery
)
from app.utils import is_valid_email, log_activity
from datetime import datetime, timedelta
import json
from flask_login import current_user

# ==================== API AUTHENTICATION ====================

def require_api_key(f):
    """
    Decorator to require valid API key OR valid session cookie.
    Allows frontend to access API via session, and external tools via Key.
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        api_key = None
        
        # 1. Check for API Key Headers
        auth_header = request.headers.get('Authorization')
        if auth_header and auth_header.startswith('Bearer '):
            api_key = auth_header[7:]
        
        if not api_key:
            api_key = request.headers.get('X-API-Key')
        
        if not api_key: 
            api_key = request.args.get('api_key')
        
        # 2. If API Key found, validate it
        if api_key:
            key_prefix = api_key[:8] if len(api_key) >= 8 else api_key
            api_key_record = APIKey.query.filter_by(key_prefix=key_prefix).first()
            
            if not api_key_record or not api_key_record.verify_key(api_key):
                return jsonify({
                    'error': 'Invalid API key',
                    'message': 'The provided API key is invalid'
                }), 401
            
            if not api_key_record.is_valid():
                return jsonify({
                    'error': 'API key expired or inactive',
                    'message': 'Your API key has expired or been deactivated'
                }), 401
            
            # Update usage stats
            api_key_record.last_used_at = datetime.utcnow()
            api_key_record.request_count += 1
            db.session.commit()
            
            # Store user in g for access in route
            g.current_user = User.query.get(api_key_record.user_id)
            g.api_key = api_key_record
            g.auth_method = 'api_key'
            
            return f(*args, **kwargs)

        # 3. If NO API Key, check for Session Cookie (Browser access)
        if current_user.is_authenticated:
            g.current_user = current_user
            g.api_key = None # No specific key used
            g.auth_method = 'session'
            return f(*args, **kwargs)

        # 4. Neither found
        return jsonify({
            'error': 'Authentication required',
            'message': 'Please provide an API key or log in.'
        }), 401
    
    return decorated_function


def check_scope(required_scope):
    """
    Check if user/key has required scope.
    Session-based users (admins/owners) implicitly have all scopes.
    """
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            # If authenticated via session, allow access (assuming dashboard users have full rights for now)
            # You can add role checks here if needed (e.g. if g.current_user.role == 'viewer')
            if getattr(g, 'auth_method', '') == 'session':
                return f(*args, **kwargs)

            # If authenticated via API Key, check scopes
            if getattr(g, 'api_key', None):
                scopes = g.api_key.get_scopes()
                if required_scope not in scopes and 'admin' not in scopes:
                    return jsonify({
                        'error': 'Insufficient permissions',
                        'message': f'This endpoint requires the "{required_scope}" scope'
                    }), 403
            
            return f(*args, **kwargs)
        return decorated_function
    return decorator


# ==================== HEALTH CHECK ====================

@bp.route('/health')
def health_check():
    """API health check endpoint."""
    return jsonify({
        'status': 'healthy',
        'version': '1.0.0',
        'timestamp': datetime.utcnow().isoformat()
    })


# ==================== CAMPAIGNS API ====================

@bp.route('/campaigns', methods=['GET'])
@require_api_key
@check_scope('read')
def list_campaigns():
    """List all campaigns for the authenticated user."""
    page = request.args.get('page', 1, type=int)
    per_page = min(request.args.get('per_page', 20, type=int), 100)
    status = request.args.get('status')
    
    query = Campaign.query.filter_by(user_id=g.current_user.id)
    
    if status: 
        query = query.filter_by(status=status)
    
    campaigns = query.order_by(Campaign.created_at.desc()).paginate(
        page=page, per_page=per_page, error_out=False
    )
    
    return jsonify({
        'campaigns': [c.to_dict() for c in campaigns.items],
        'pagination': {
            'page': campaigns.page,
            'per_page': campaigns.per_page,
            'total': campaigns.total,
            'pages': campaigns.pages,
            'has_next': campaigns.has_next,
            'has_prev': campaigns.has_prev
        }
    })


@bp.route('/campaigns/<int:campaign_id>', methods=['GET'])
@require_api_key
@check_scope('read')
def get_campaign(campaign_id):
    """Get a specific campaign."""
    campaign = Campaign.query.get_or_404(campaign_id)
    
    if campaign.user_id != g.current_user.id:
        return jsonify({'error': 'Not found'}), 404
    
    return jsonify({
        'campaign': campaign.to_dict(),
        'analytics': campaign.get_analytics()
    })


@bp.route('/campaigns', methods=['POST'])
@require_api_key
@check_scope('write')
def create_campaign():
    """Create a new campaign."""
    data = request.get_json()
    
    if not data: 
        return jsonify({'error': 'No data provided'}), 400
    
    required_fields = ['name', 'subject', 'body_html']
    missing = [f for f in required_fields if f not in data]
    if missing:
        return jsonify({
            'error': 'Missing required fields',
            'fields': missing
        }), 400
    
    try:
        campaign = Campaign(
            name=data['name'],
            subject=data['subject'],
            body_html=data['body_html'],
            body_plain=data.get('body_plain', ''),
            preheader=data.get('preheader', ''),
            tracking_enabled=data.get('tracking_enabled', True),
            user_id=g.current_user.id,
            status='Draft'
        )
        
        # Optional fields
        if 'smtp_profile_id' in data:
            profile = SMTPServer.query.get(data['smtp_profile_id'])
            if profile and profile.user_id == g.current_user.id:
                campaign.smtp_profile_id = data['smtp_profile_id']
        
        if 'scheduled_at' in data:
            campaign.scheduled_at = datetime.fromisoformat(data['scheduled_at'])
            campaign.status = 'Scheduled'
        
        db.session.add(campaign)
        db.session.commit()
        
        log_activity(f"API: Campaign created: {campaign.name}", "SUCCESS")
        
        return jsonify({
            'message': 'Campaign created successfully',
            'campaign': campaign.to_dict()
        }), 201
    
    except Exception as e: 
        db.session.rollback()
        return jsonify({'error': str(e)}), 500


@bp.route('/campaigns/<int:campaign_id>', methods=['PUT', 'PATCH'])
@require_api_key
@check_scope('write')
def update_campaign(campaign_id):
    """Update a campaign."""
    campaign = Campaign.query.get_or_404(campaign_id)
    
    if campaign.user_id != g.current_user.id:
        return jsonify({'error': 'Not found'}), 404
    
    if campaign.status == 'Sending':
        return jsonify({'error': 'Cannot update a campaign that is currently sending'}), 400
    
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400
    
    try:
        updatable_fields = [
            'name', 'subject', 'body_html', 'body_plain', 'preheader',
            'tracking_enabled', 'throttle_amount', 'throttle_delay'
        ]
        
        for field in updatable_fields: 
            if field in data:
                setattr(campaign, field, data[field])
        
        campaign.updated_at = datetime.utcnow()
        db.session.commit()
        
        return jsonify({
            'message': 'Campaign updated successfully',
            'campaign': campaign.to_dict()
        })
    
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500


@bp.route('/campaigns/<int:campaign_id>', methods=['DELETE'])
@require_api_key
@check_scope('write')
def delete_campaign(campaign_id):
    """Delete a campaign."""
    campaign = Campaign.query.get_or_404(campaign_id)
    
    if campaign.user_id != g.current_user.id:
        return jsonify({'error': 'Not found'}), 404
    
    if campaign.status == 'Sending':
        return jsonify({'error': 'Cannot delete a campaign that is currently sending'}), 400
    
    try:
        db.session.delete(campaign)
        db.session.commit()
        
        log_activity(f"API: Campaign deleted: {campaign.name}", "WARNING")
        
        return jsonify({'message': 'Campaign deleted successfully'})
    
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500


@bp.route('/campaigns/<int:campaign_id>/start', methods=['POST'])
@require_api_key
@check_scope('write')
def start_campaign(campaign_id):
    """Start sending a campaign."""
    campaign = Campaign.query.get_or_404(campaign_id)
    
    if campaign.user_id != g.current_user.id:
        return jsonify({'error': 'Not found'}), 404
    
    if campaign.status == 'Sending':
        return jsonify({'error': 'Campaign is already sending'}), 400
    
    queued_count = campaign.recipients.filter_by(status='Queued').count()
    if queued_count == 0:
        return jsonify({'error': 'No queued recipients'}), 400
    
    if not campaign.smtp_profile:
        return jsonify({'error': 'No mailer profile configured'}), 400
    
    try:
        campaign.status = 'Sending'
        campaign.started_at = datetime.utcnow()
        db.session.commit()
        
        # Start sending task
        from app.tasks import send_campaign_task
        send_campaign_task.delay(campaign_id)
        
        log_activity(f"API: Campaign started: {campaign.name}", "SUCCESS")
        
        return jsonify({
            'message': 'Campaign started successfully',
            'campaign': campaign.to_dict()
        })
    
    except Exception as e: 
        db.session.rollback()
        return jsonify({'error': str(e)}), 500


@bp.route('/campaigns/<int:campaign_id>/pause', methods=['POST'])
@require_api_key
@check_scope('write')
def pause_campaign(campaign_id):
    """Pause a sending campaign."""
    campaign = Campaign.query.get_or_404(campaign_id)
    
    if campaign.user_id != g.current_user.id:
        return jsonify({'error': 'Not found'}), 404
    
    if campaign.status != 'Sending':
        return jsonify({'error': 'Campaign is not currently sending'}), 400
    
    campaign.status = 'Paused'
    db.session.commit()
    
    return jsonify({
        'message': 'Campaign paused successfully',
        'campaign': campaign.to_dict()
    })


@bp.route('/campaigns/<int:campaign_id>/stop', methods=['POST'])
@require_api_key
@check_scope('write')
def stop_campaign(campaign_id):
    """Stop a campaign."""
    campaign = Campaign.query.get_or_404(campaign_id)
    
    if campaign.user_id != g.current_user.id:
        return jsonify({'error': 'Not found'}), 404
    
    if campaign.status not in ['Sending', 'Paused']: 
        return jsonify({'error': 'Campaign is not active'}), 400
    
    campaign.status = 'Stopped'
    campaign.completed_at = datetime.utcnow()
    db.session.commit()
    
    return jsonify({
        'message': 'Campaign stopped successfully',
        'campaign': campaign.to_dict()
    })


# ==================== RECIPIENTS API ====================

@bp.route('/campaigns/<int:campaign_id>/recipients', methods=['GET'])
@require_api_key
@check_scope('read')
def list_recipients(campaign_id):
    """List recipients for a campaign."""
    campaign = Campaign.query.get_or_404(campaign_id)
    
    if campaign.user_id != g.current_user.id:
        return jsonify({'error': 'Not found'}), 404
    
    page = request.args.get('page', 1, type=int)
    per_page = min(request.args.get('per_page', 50, type=int), 100)
    status = request.args.get('status')
    
    query = campaign.recipients
    
    if status:
        query = query.filter_by(status=status)
    
    recipients = query.paginate(page=page, per_page=per_page, error_out=False)
    
    return jsonify({
        'recipients': [r.to_dict() for r in recipients.items],
        'pagination': {
            'page': recipients.page,
            'per_page': recipients.per_page,
            'total': recipients.total,
            'pages': recipients.pages
        }
    })


@bp.route('/campaigns/<int:campaign_id>/recipients', methods=['POST'])
@require_api_key
@check_scope('write')
def add_recipients(campaign_id):
    """Add recipients to a campaign."""
    campaign = Campaign.query.get_or_404(campaign_id)
    
    if campaign.user_id != g.current_user.id:
        return jsonify({'error': 'Not found'}), 404
    
    if campaign.status == 'Sending':
        return jsonify({'error': 'Cannot add recipients to a sending campaign'}), 400
    
    data = request.get_json()
    if not data or 'recipients' not in data: 
        return jsonify({'error': 'No recipients provided'}), 400
    
    recipients_data = data['recipients']
    if not isinstance(recipients_data, list):
        return jsonify({'error': 'Recipients must be a list'}), 400
    
    added = 0
    skipped = 0
    errors = []
    
    for r_data in recipients_data:
        email = r_data.get('email', '').strip().lower() if isinstance(r_data, dict) else str(r_data).strip().lower()
        
        if not is_valid_email(email):
            skipped += 1
            errors.append(f"Invalid email: {email}")
            continue
        
        # Check if already exists
        if Recipient.query.filter_by(campaign_id=campaign.id, email=email).first():
            skipped += 1
            continue
        
        # Check suppression
        is_suppressed = Suppression.query.filter_by(email=email).first()
        
        recipient = Recipient(
            email=email,
            campaign_id=campaign.id,
            data=json.dumps(r_data if isinstance(r_data, dict) else {'email': email}),
            status='Suppressed' if is_suppressed else 'Queued'
        )
        db.session.add(recipient)
        added += 1
    
    campaign.total_recipients = campaign.recipients.count() + added
    db.session.commit()
    
    return jsonify({
        'message': f'Added {added} recipients, skipped {skipped}',
        'added': added,
        'skipped': skipped,
        'errors': errors[:10] if errors else []
    }), 201


@bp.route('/campaigns/<int:campaign_id>/recipients/<int:recipient_id>', methods=['DELETE'])
@require_api_key
@check_scope('write')
def delete_recipient(campaign_id, recipient_id):
    """Delete a recipient from a campaign."""
    campaign = Campaign.query.get_or_404(campaign_id)
    
    if campaign.user_id != g.current_user.id:
        return jsonify({'error': 'Not found'}), 404
    
    recipient = Recipient.query.get_or_404(recipient_id)
    
    if recipient.campaign_id != campaign_id: 
        return jsonify({'error': 'Not found'}), 404
    
    db.session.delete(recipient)
    campaign.total_recipients = campaign.recipients.count() - 1
    db.session.commit()
    
    return jsonify({'message': 'Recipient deleted successfully'})


# ==================== SUPPRESSION API ====================

@bp.route('/suppression', methods=['GET'])
@require_api_key
@check_scope('read')
def list_suppressions():
    """List suppressed emails."""
    page = request.args.get('page', 1, type=int)
    per_page = min(request.args.get('per_page', 50, type=int), 100)
    
    suppressions = Suppression.query.order_by(Suppression.created_at.desc()).paginate(
        page=page, per_page=per_page, error_out=False
    )
    
    return jsonify({
        'suppressions': [
            {
                'id': s.id,
                'email': s.email,
                'reason': s.reason,
                'source': s.source,
                'created_at': s.created_at.isoformat() if s.created_at else None
            }
            for s in suppressions.items
        ],
        'pagination': {
            'page': suppressions.page,
            'per_page': suppressions.per_page,
            'total': suppressions.total,
            'pages': suppressions.pages
        }
    })


@bp.route('/suppression', methods=['POST'])
@require_api_key
@check_scope('write')
def add_suppression():
    """Add email(s) to suppression list."""
    data = request.get_json()
    
    if not data: 
        return jsonify({'error': 'No data provided'}), 400
    
    emails = data.get('emails', [])
    if isinstance(emails, str):
        emails = [emails]
    
    if not emails:
        email = data.get('email')
        if email:
            emails = [email]
    
    if not emails:
        return jsonify({'error': 'No emails provided'}), 400
    
    reason = data.get('reason', 'API')
    
    added = 0
    skipped = 0
    
    for email in emails:
        email = email.strip().lower()
        
        if not is_valid_email(email):
            skipped += 1
            continue
        
        if Suppression.query.filter_by(email=email).first():
            skipped += 1
            continue
        
        suppression = Suppression(
            email=email,
            reason=reason,
            source='api',
            user_id=g.current_user.id
        )
        db.session.add(suppression)
        added += 1
    
    db.session.commit()
    
    return jsonify({
        'message': f'Added {added} emails, skipped {skipped}',
        'added': added,
        'skipped': skipped
    }), 201


@bp.route('/suppression/<email>', methods=['DELETE'])
@require_api_key
@check_scope('write')
def remove_suppression(email):
    """Remove email from suppression list."""
    suppression = Suppression.query.filter_by(email=email.lower()).first()
    
    if not suppression:
        return jsonify({'error': 'Email not found in suppression list'}), 404
    
    db.session.delete(suppression)
    db.session.commit()
    
    return jsonify({'message': 'Email removed from suppression list'})


@bp.route('/suppression/check/<email>', methods=['GET'])
@require_api_key
@check_scope('read')
def check_suppression(email):
    """Check if an email is suppressed."""
    suppression = Suppression.query.filter_by(email=email.lower()).first()
    
    return jsonify({
        'email': email.lower(),
        'suppressed': suppression is not None,
        'reason': suppression.reason if suppression else None,
        'suppressed_at': suppression.created_at.isoformat() if suppression else None
    })


# ==================== ANALYTICS API ====================

@bp.route('/analytics/summary', methods=['GET'])
@require_api_key
@check_scope('read')
def analytics_summary():
    """Get analytics summary."""
    days = request.args.get('days', 30, type=int)
    start_date = datetime.utcnow() - timedelta(days=days)
    
    # Get campaigns in date range
    campaigns = Campaign.query.filter(
        Campaign.user_id == g.current_user.id,
        Campaign.created_at >= start_date
    ).all()
    
    total_sent = 0
    total_opened = 0
    total_clicked = 0
    total_bounced = 0
    
    for campaign in campaigns:
        analytics = campaign.get_analytics()
        total_sent += analytics['sent']
        total_opened += analytics['opened']
        total_clicked += analytics['clicked']
        total_bounced += analytics['bounced']
    
    return jsonify({
        'period_days': days,
        'campaigns_count': len(campaigns),
        'total_sent': total_sent,
        'total_opened': total_opened,
        'total_clicked': total_clicked,
        'total_bounced': total_bounced,
        'open_rate': round((total_opened / total_sent * 100), 2) if total_sent > 0 else 0,
        'click_rate': round((total_clicked / total_sent * 100), 2) if total_sent > 0 else 0,
        'bounce_rate': round((total_bounced / total_sent * 100), 2) if total_sent > 0 else 0
    })


@bp.route('/analytics/campaigns/<int:campaign_id>', methods=['GET'])
@require_api_key
@check_scope('read')
def campaign_analytics(campaign_id):
    """Get detailed analytics for a campaign."""
    campaign = Campaign.query.get_or_404(campaign_id)
    
    if campaign.user_id != g.current_user.id:
        return jsonify({'error': 'Not found'}), 404
    
    analytics = campaign.get_analytics()
    
    # Get hourly breakdown if campaign has been sent
    hourly_data = []
    if campaign.sent_count > 0:
        # Group recipients by hour they were opened
        opens_by_hour = db.session.query(
            db.func.extract('hour', Recipient.opened_at).label('hour'),
            db.func.count(Recipient.id).label('count')
        ).filter(
            Recipient.campaign_id == campaign_id,
            Recipient.opened_at.isnot(None)
        ).group_by('hour').all()
        
        hourly_data = [{'hour': int(h), 'opens': c} for h, c in opens_by_hour if h is not None]
    
    return jsonify({
        'campaign_id': campaign_id,
        'campaign_name': campaign.name,
        'status': campaign.status,
        'analytics': analytics,
        'hourly_opens': hourly_data
    })


# ==================== SMTP PROFILES API ====================

@bp.route('/smtp-profiles', methods=['GET'])
@require_api_key
@check_scope('read')
def list_smtp_profiles():
    """List SMTP profiles."""
    profiles = SMTPServer.query.filter_by(user_id=g.current_user.id).all()
    
    return jsonify({
        'profiles': [
            {
                'id': p.id,
                'name': p.profile_name,
                'server': p.server,
                'port': p.port,
                'username': p.username,
                'sender_email': p.sender_email,
                'is_active': p.is_active,
                'daily_limit': p.daily_limit,
                'sent_today': p.sent_today,
                'warmup_enabled': p.warmup_enabled
            }
            for p in profiles
        ]
    })


# ==================== WEBHOOKS API ====================

@bp.route('/webhooks', methods=['GET'])
@require_api_key
@check_scope('read')
def list_webhooks():
    """List configured webhooks."""
    webhooks = Webhook.query.filter_by(user_id=g.current_user.id).all()
    
    return jsonify({
        'webhooks': [
            {
                'id': w.id,
                'name': w.name,
                'url': w.url,
                'events': w.get_events(),
                'is_active': w.is_active,
                'last_triggered_at': w.last_triggered_at.isoformat() if w.last_triggered_at else None,
                'success_count': w.success_count,
                'failure_count': w.failure_count
            }
            for w in webhooks
        ]
    })


@bp.route('/webhooks', methods=['POST'])
@require_api_key
@check_scope('write')
def create_webhook():
    """Create a new webhook."""
    data = request.get_json()
    
    if not data or 'name' not in data or 'url' not in data: 
        return jsonify({'error': 'Name and URL are required'}), 400
    
    webhook = Webhook(
        name=data['name'],
        url=data['url'],
        user_id=g.current_user.id,
        is_active=data.get('is_active', True)
    )
    
    webhook.set_events(data.get('events', ['email.sent', 'email.opened', 'email.clicked']))
    webhook.generate_secret()
    
    db.session.add(webhook)
    db.session.commit()
    
    return jsonify({
        'message': 'Webhook created successfully',
        'webhook': {
            'id': webhook.id,
            'name': webhook.name,
            'url': webhook.url,
            'secret': webhook.secret,
            'events': webhook.get_events()
        }
    }), 201


@bp.route('/webhooks/<int:webhook_id>', methods=['DELETE'])
@require_api_key
@check_scope('write')
def delete_webhook(webhook_id):
    """Delete a webhook."""
    webhook = Webhook.query.get_or_404(webhook_id)
    
    if webhook.user_id != g.current_user.id:
        return jsonify({'error': 'Not found'}), 404
    
    db.session.delete(webhook)
    db.session.commit()
    
    return jsonify({'message': 'Webhook deleted successfully'})


# ==================== CAMPAIGN STATUS API ====================

@bp.route('/campaign/<int:campaign_id>/status', methods=['GET'])
@require_api_key
@check_scope('read')
def api_campaign_status(campaign_id):
    """Get live status of a campaign. Accessible by frontend session or API key."""
    campaign = Campaign.query.get_or_404(campaign_id)

    if campaign.user_id != g.current_user.id:
        return jsonify({'error': 'Unauthorized'}), 403

    total = int(campaign.total_recipients or 0)
    sent = campaign.recipients.filter_by(status='Sent').count()
    failed = campaign.recipients.filter_by(status='Failed').count()

    progress = round(((sent + failed) / total * 100), 1) if total > 0 else 0.0

    return jsonify({
        'campaign': {
            'id': campaign.id,
            'status': campaign.status,
            'analytics': {
                'sent': sent,
                'failed': failed,
                'total': total
            }
        },
        'status': campaign.status,
        'sent': sent,
        'failed': failed,
        'total': total,
        'progress': progress
    })


# ==================== ERROR HANDLERS ====================

@bp.errorhandler(400)
def bad_request(error):
    return jsonify({'error': 'Bad Request', 'message': str(error)}), 400


@bp.errorhandler(401)
def unauthorized(error):
    return jsonify({'error': 'Unauthorized', 'message': 'Authentication required'}), 401


@bp.errorhandler(403)
def forbidden(error):
    return jsonify({'error': 'Forbidden', 'message': 'Access denied'}), 403


@bp.errorhandler(404)
def not_found(error):
    return jsonify({'error': 'Not Found', 'message': 'Resource not found'}), 404


@bp.errorhandler(500)
def internal_error(error):
    return jsonify({'error': 'Internal Server Error', 'message': 'An unexpected error occurred'}), 500
