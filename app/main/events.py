from flask import request
from flask_login import current_user
from flask_socketio import emit, join_room, leave_room, disconnect
from app import socketio, db
from app.models import Campaign, Notification
from datetime import datetime
import functools

def authenticated_only(f):
    """Decorator to require authentication for socket events."""
    @functools.wraps(f)
    def wrapped(*args, **kwargs):
        if not current_user.is_authenticated:
            disconnect()
            return
        return f(*args, **kwargs)
    return wrapped

# ==================== CAMPAIGN NAMESPACE ====================

@socketio.on('connect', namespace='/campaigns')
def handle_campaign_connect():
    """Handle client connection to campaigns namespace."""
    if not current_user.is_authenticated:
        return False
    emit('status', {'message': 'Connected to campaign channel'})

@socketio.on('join_campaign_room', namespace='/campaigns')
@authenticated_only
def handle_join_campaign_room(data):
    """Client joins a room to receive updates for a specific campaign."""
    campaign_id = data.get('campaign_id')
    if campaign_id:
        room = f'campaign_{campaign_id}'
        join_room(room)
        emit('status', {'message': f'Joined room for campaign {campaign_id}'})

@socketio.on('leave_campaign_room', namespace='/campaigns')
@authenticated_only
def handle_leave_campaign_room(data):
    """Client leaves a campaign room."""
    campaign_id = data.get('campaign_id')
    if campaign_id:
        room = f'campaign_{campaign_id}'
        leave_room(room)
        emit('status', {'message': f'Left room for campaign {campaign_id}'})

def broadcast_campaign_progress(campaign_id, sent, failed, total, message=None):
    """Broadcast campaign sending progress."""
    room = f'campaign_{campaign_id}'
    progress = round((sent + failed) / total * 100, 1) if total > 0 else 0
    
    payload = {
        'campaign_id': campaign_id,
        'sent': sent,
        'failed': failed,
        'total': total,
        'progress': progress,
        'message': message,
        'timestamp': datetime.utcnow().isoformat()
    }
    socketio.emit('progress_update', payload, namespace='/campaigns', room=room)

def broadcast_campaign_status_change(campaign_id, status, message=None):
    """
    Broadcast a change in the campaign's overall status.
    This is crucial for telling the frontend when to stop polling or refresh.
    """
    room = f'campaign_{campaign_id}'
    payload = {
        'campaign_id': campaign_id,
        'status': status,
        'message': message,
        'timestamp': datetime.utcnow().isoformat()
    }
    socketio.emit('status_update', payload, namespace='/campaigns', room=room)

# ==================== NOTIFICATIONS NAMESPACE ====================

@socketio.on('connect', namespace='/notifications')
def handle_notification_connect():
    """Handle client connection to notifications namespace."""
    if not current_user.is_authenticated:
        return False
    
    room = f'user_{current_user.id}'
    join_room(room)
    
    unread_count = Notification.query.filter_by(user_id=current_user.id, read=False).count()
    emit('status', {'message': 'Connected to notifications', 'unread_count': unread_count})

def send_notification(user_id, title, message, notification_type='info'):
    """Send a real-time notification to a user."""
    notification = Notification(
        user_id=user_id,
        title=title,
        message=message,
        type=notification_type
    )
    db.session.add(notification)
    db.session.commit()
    
    room = f'user_{user_id}'
    payload = {
        'id': notification.id,
        'title': title,
        'message': message,
        'type': notification_type,
        'created_at': notification.created_at.isoformat()
    }
    socketio.emit('new_notification', payload, namespace='/notifications', room=room)
