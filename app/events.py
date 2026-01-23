from flask_login import current_user
from flask_socketio import emit, join_room, leave_room, disconnect
from app import socketio, db
from app.models import Campaign, Notification, Team
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

    emit('connected', {
        'message': 'Connected to campaign updates',
        'user_id': current_user.id,
        'timestamp': datetime.utcnow().isoformat()
    })


@socketio.on('disconnect', namespace='/campaigns')
def handle_campaign_disconnect():
    """Handle client disconnection from campaigns namespace."""
    pass


@socketio.on('join_campaign', namespace='/campaigns')
@authenticated_only
def handle_join_campaign(data):
    """Join a campaign room for real-time updates."""
    campaign_id = data.get('campaign_id')

    if not campaign_id:
        emit('error', {'message': 'Campaign ID required'})
        return

    campaign = Campaign.query.get(campaign_id)

    # Allow owner, campaign author, team members, admins
    has_access = False
    if not campaign:
        emit('error', {'message': 'Campaign not found'})
        return

    try:
        if campaign.user_id == current_user.id:
            has_access = True
        elif hasattr(current_user, 'role') and str(current_user.role).lower() in ('admin', 'owner'):
            has_access = True
        elif campaign.team_id:
            team = Team.query.get(campaign.team_id)
            if team and current_user in team.members:
                has_access = True
    except Exception:
        has_access = False

    if not has_access:
        emit('error', {'message': 'Campaign not found or access denied'})
        return

    room = f'campaign_{campaign_id}'
    join_room(room)

    emit('joined', {
        'campaign_id': campaign_id,
        'room': room,
        'message': f'Joined campaign {campaign.name}'
    })


@socketio.on('leave_campaign', namespace='/campaigns')
@authenticated_only
def handle_leave_campaign(data):
    """Leave a campaign room."""
    campaign_id = data.get('campaign_id')

    if campaign_id:
        room = f'campaign_{campaign_id}'
        leave_room(room)

        emit('left', {
            'campaign_id': campaign_id,
            'message': 'Left campaign room'
        })


@socketio.on('get_campaign_status', namespace='/campaigns')
@authenticated_only
def handle_get_campaign_status(data):
    """Get current campaign status."""
    campaign_id = data.get('campaign_id')

    if not campaign_id:
        emit('error', {'message': 'Campaign ID required'})
        return

    campaign = Campaign.query.get(campaign_id)

    if not campaign:
        emit('error', {'message': 'Campaign not found'})
        return

    # Basic access check: author or team member or admin
    has_access = False
    try:
        if campaign.user_id == current_user.id:
            has_access = True
        elif hasattr(current_user, 'role') and str(current_user.role).lower() in ('admin', 'owner'):
            has_access = True
        elif campaign.team_id:
            team = Team.query.get(campaign.team_id)
            if team and current_user in team.members:
                has_access = True
    except Exception:
        has_access = False

    if not has_access:
        emit('error', {'message': 'Access denied'})
        return

    analytics = {}
    try:
        analytics = campaign.get_analytics()
    except Exception:
        analytics = {}

    emit('campaign_status', {
        'campaign_id': campaign_id,
        'status': campaign.status,
        'analytics': analytics,
        'timestamp': datetime.utcnow().isoformat()
    })


def broadcast_campaign_update(campaign_id, update_type, data):
    """Broadcast campaign update to all connected clients in the room."""
    room = f'campaign_{campaign_id}'

    socketio.emit('campaign_update', {
        'campaign_id': campaign_id,
        'type': update_type,
        'data': data,
        'timestamp': datetime.utcnow().isoformat()
    }, namespace='/campaigns', room=room)


def broadcast_recipient_update(campaign_id, recipient_id, status, data=None):
    """Broadcast recipient status update."""
    room = f'campaign_{campaign_id}'

    payload = {
        'campaign_id': campaign_id,
        'recipient_id': recipient_id,
        'status': status,
        'timestamp': datetime.utcnow().isoformat()
    }

    if data:
        payload['data'] = data

    socketio.emit('recipient_update', payload, namespace='/campaigns', room=room)


def broadcast_campaign_progress(campaign_id, sent, failed, total, current_email=None):
    """Broadcast campaign progress update."""
    room = f'campaign_{campaign_id}'

    progress = round((sent + failed) / total * 100, 1) if total > 0 else 0

    socketio.emit('campaign_progress', {
        'campaign_id': campaign_id,
        'sent': sent,
        'failed': failed,
        'total': total,
        'progress': progress,
        'current_email': current_email,
        'timestamp': datetime.utcnow().isoformat()
    }, namespace='/campaigns', room=room)


# ==================== NOTIFICATIONS NAMESPACE ====================

@socketio.on('connect', namespace='/notifications')
def handle_notification_connect():
    """Handle client connection to notifications namespace."""
    if not current_user.is_authenticated:
        return False

    room = f'user_{current_user.id}'
    join_room(room)

    unread_count = Notification.query.filter_by(
        user_id=current_user.id,
        read=False
    ).count()

    emit('connected', {
        'message': 'Connected to notifications',
        'unread_count': unread_count
    })


@socketio.on('disconnect', namespace='/notifications')
def handle_notification_disconnect():
    """Handle client disconnection from notifications namespace."""
    if current_user.is_authenticated:
        room = f'user_{current_user.id}'
        leave_room(room)


@socketio.on('mark_read', namespace='/notifications')
@authenticated_only
def handle_mark_notification_read(data):
    """Mark a notification as read."""
    notification_id = data.get('notification_id')

    if notification_id:
        notification = Notification.query.get(notification_id)

        if notification and notification.user_id == current_user.id:
            notification.read = True
            notification.read_at = datetime.utcnow()
            db.session.commit()

            emit('notification_read', {
                'notification_id': notification_id,
                'success': True
            })


@socketio.on('mark_all_read', namespace='/notifications')
@authenticated_only
def handle_mark_all_notifications_read():
    """Mark all notifications as read."""
    Notification.query.filter_by(
        user_id=current_user.id,
        read=False
    ).update({
        'read': True,
        'read_at': datetime.utcnow()
    })

    db.session.commit()

    emit('all_notifications_read', {'success': True})


@socketio.on('get_notifications', namespace='/notifications')
@authenticated_only
def handle_get_notifications(data):
    """Get recent notifications."""
    limit = data.get('limit', 10)
    unread_only = data.get('unread_only', False)

    query = Notification.query.filter_by(user_id=current_user.id)

    if unread_only:
        query = query.filter_by(read=False)

    notifications = query.order_by(
        Notification.created_at.desc()
    ).limit(limit).all()

    emit('notifications_list', {
        'notifications': [
            {
                'id': n.id,
                'title': n.title,
                'message': n.message,
                'type': n.type,
                'read': n.read,
                'created_at': n.created_at.isoformat() if n.created_at else None
            }
            for n in notifications
        ]
    })


def send_notification(user_id, title, message, notification_type='info', related_type=None, related_id=None):
    """Send a real-time notification to a user."""
    notification = Notification(
        user_id=user_id,
        title=title,
        message=message,
        type=notification_type,
        related_type=related_type,
        related_id=related_id
    )

    db.session.add(notification)
    db.session.commit()

    room = f'user_{user_id}'

    socketio.emit('new_notification', {
        'id': notification.id,
        'title': title,
        'message': message,
        'type': notification_type,
        'related_type': related_type,
        'related_id': related_id,
        'created_at': notification.created_at.isoformat()
    }, namespace='/notifications', room=room)

    return notification


# ==================== ACTIVITY LOG NAMESPACE ====================

@socketio.on('connect', namespace='/logs')
def handle_logs_connect():
    """Handle client connection to logs namespace."""
    if not current_user.is_authenticated:
        return False

    room = f'logs_{current_user.id}'
    join_room(room)

    emit('connected', {'message': 'Connected to activity logs'})


@socketio.on('disconnect', namespace='/logs')
def handle_logs_disconnect():
    """Handle client disconnection from logs namespace."""
    if current_user.is_authenticated:
        room = f'logs_{current_user.id}'
        leave_room(room)


def broadcast_log_entry(user_id, message, level='INFO'):
    """Broadcast a log entry to connected clients."""
    room = f'logs_{user_id}'

    socketio.emit('log_entry', {
        'timestamp': datetime.utcnow().strftime('%H:%M:%S'),
        'message': message,
        'level': level
    }, namespace='/logs', room=room)


# ==================== HELPER FUNCTIONS ====================

def emit_to_user(user_id, event, data, namespace='/notifications'):
    """Emit an event to a specific user."""
    room = f'user_{user_id}'
    socketio.emit(event, data, namespace=namespace, room=room)


def emit_to_campaign(campaign_id, event, data):
    """Emit an event to all users watching a campaign."""
    room = f'campaign_{campaign_id}'
    socketio.emit(event, data, namespace='/campaigns', room=room)
