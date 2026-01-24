from flask import (render_template, flash, redirect, url_for, request,
                   jsonify, current_app, Response, send_file, abort, session)
from flask_login import login_user, logout_user, current_user, login_required
from werkzeug.utils import secure_filename
from app import db, cache, socketio
from app.models import (
    User, UserRole, Campaign, Recipient, SMTPServer, Suppression,
    GlobalSettings, Sequence, SequenceRecipient, Tag, Segment,
    EmailTemplate, Team, APIKey, Webhook, Notification, ActivityLog,
    DailyStats, HourlyStats, UserSettings, ConsentRecord
)
from app.core_logic.deliverability import DeliverabilityHelper
from app.core_logic.ai_handler import AIHandler
from app.core_logic.smtp_handler import SMTPHandler
from app.utils import (
    log_activity, get_logs, is_valid_email, html_to_plain_text,
    allowed_file, parse_csv_file
)
from app.main import bp
from flask_wtf import FlaskForm
from wtforms import StringField, SubmitField, TextAreaField, BooleanField
from wtforms.validators import DataRequired, Email, Optional
import csv
import io
import json
import os
import re
import time
from datetime import datetime, timedelta
from sqlalchemy import func

# ==================== FORMS ====================


class DeliverabilityForm(FlaskForm):
    domain_ip = StringField('Domain or IP', validators=[DataRequired()])
    check_auth = SubmitField('Check Authentication')
    check_blacklist = SubmitField('Check Blacklist')


class SuppressionForm(FlaskForm):
    email = StringField('Email Address', validators=[DataRequired(), Email()])
    reason = StringField('Reason', default="Manual")
    submit = SubmitField('Add to Suppression List')


class SMTPProfileForm(FlaskForm):
    name = StringField('Profile Name', validators=[DataRequired()])
    server = StringField('SMTP Server', validators=[DataRequired()])
    port = StringField('Port', default='587')
    username = StringField('Username', validators=[DataRequired()])
    password = StringField('Password')
    sender_name = StringField('Sender Name')
    sender_email = StringField('Sender Email', validators=[Optional(), Email()])
    use_tls = BooleanField('Use TLS', default=True)
    use_ssl = BooleanField('Use SSL', default=False)
    is_active = BooleanField('Active', default=True)
    daily_limit = StringField('Daily Limit', default='500')
    priority = StringField('Priority', default='1')
    submit = SubmitField('Save Profile')


class TeamForm(FlaskForm):
    name = StringField('Team Name', validators=[DataRequired()])
    description = TextAreaField('Description')
    submit = SubmitField('Create Team')


class WebhookForm(FlaskForm):
    name = StringField('Webhook Name', validators=[DataRequired()])
    url = StringField('Webhook URL', validators=[DataRequired()])
    submit = SubmitField('Save Webhook')


# ==================== HELPER FUNCTIONS ====================


def get_or_create_global_settings():
    """Get or create global settings."""
    settings = GlobalSettings.query.first()
    if not settings:
        settings = GlobalSettings()
        db.session.add(settings)
        db.session.commit()
    return settings


def emit_campaign_update(campaign_id, data):
    """Emit real-time campaign update via WebSocket."""
    try:
        socketio.emit('campaign_update', {
            'campaign_id': campaign_id,
            **data
        }, namespace='/campaigns', room=f'campaign_{campaign_id}')
    except Exception as e:
        current_app.logger.error(f"WebSocket emit error: {e}")


def log_user_activity(action, description=None, object_type=None, object_id=None):
    """Log user activity."""
    try:
        activity = ActivityLog(
            user_id=current_user.id if current_user.is_authenticated else None,
            action=action,
            description=description,
            object_type=object_type,
            object_id=object_id,
            ip_address=request.remote_addr,
            user_agent=request.headers.get('User-Agent', '')[:255]
        )
        db.session.add(activity)
        db.session.commit()
    except Exception as e:
        current_app.logger.error(f"Activity log error: {e}")


def create_notification(user_id, title, message, notification_type='info', related_type=None, related_id=None):
    """Create a notification for a user and emit it."""
    try:
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

        payload = {
            'id': notification.id,
            'title': notification.title,
            'message': notification.message,
            'type': notification.type,
            'related_type': notification.related_type,
            'related_id': notification.related_id,
            'read': notification.read,
            'created_at': notification.created_at.isoformat() if notification.created_at else None
        }

        # Emit real-time notification
        socketio.emit('notification', payload, namespace='/notifications', room=f'user_{user_id}')

        return notification
    except Exception as e:
        current_app.logger.error(f"Notification error: {e}")
        return None


# ==================== AUTHENTICATION ROUTES ====================


@bp.route('/login', methods=['GET', 'POST'])
def login():
    """User login."""
    if current_user.is_authenticated:
        return redirect(url_for('main.index'))

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        remember = bool(request.form.get('remember', False))

        user = User.query.filter(
            (User.username == username) | (User.email == username)
        ).first()

        if user and user.check_password(password):
            if not user.is_active:
                flash('Your account has been deactivated. Please contact support.', 'danger')
                return redirect(url_for('main.login'))

            # Check 2FA if enabled
            if user.two_factor_enabled:
                session['pending_2fa_user_id'] = user.id
                return redirect(url_for('main.verify_2fa'))

            login_user(user, remember=remember)
            user.last_login = datetime.utcnow()
            db.session.commit()

            log_user_activity('login', 'User logged in')
            log_activity(f"User {user.username} logged in", "SUCCESS")

            next_page = request.args.get('next')
            if next_page and next_page.startswith('/'):
                return redirect(next_page)
            return redirect(url_for('main.index'))

        flash('Invalid username or password', 'danger')
        log_activity(f"Failed login attempt for: {username}", "WARNING")

    return render_template('login.html', title='Sign In')


@bp.route('/verify-2fa', methods=['GET', 'POST'])
def verify_2fa():
    """Verify 2FA token."""
    user_id = session.get('pending_2fa_user_id')
    if not user_id:
        return redirect(url_for('main.login'))

    user = User.query.get(user_id)
    if not user:
        session.pop('pending_2fa_user_id', None)
        return redirect(url_for('main.login'))

    if request.method == 'POST':
        token = request.form.get('token', '').strip()

        if user.verify_2fa_token(token):
            session.pop('pending_2fa_user_id', None)
            login_user(user)
            user.last_login = datetime.utcnow()
            db.session.commit()

            log_user_activity('login_2fa', 'User logged in with 2FA')
            flash('Successfully logged in!', 'success')
            return redirect(url_for('main.index'))
        else:
            flash('Invalid verification code. Please try again.', 'danger')

    return render_template('verify_2fa.html', title='Two-Factor Authentication')


@bp.route('/logout')
def logout():
    """User logout."""
    if current_user.is_authenticated:
        log_user_activity('logout', 'User logged out')
        log_activity(f"User {current_user.username} logged out", "INFO")
    logout_user()
    flash('You have been logged out.', 'info')
    return redirect(url_for('main.login'))


@bp.route('/register', methods=['GET', 'POST'])
def register():
    """User registration."""
    if current_user.is_authenticated:
        return redirect(url_for('main.index'))

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        confirm_password = request.form.get('confirm_password', '')

        # Validation
        errors = []

        if len(username) < 3:
            errors.append('Username must be at least 3 characters long.')

        if not is_valid_email(email):
            errors.append('Please enter a valid email address.')

        if len(password) < 8:
            errors.append('Password must be at least 8 characters long.')

        if password != confirm_password:
            errors.append('Passwords do not match.')

        if User.query.filter_by(username=username).first():
            errors.append('Username already exists.')

        if User.query.filter_by(email=email).first():
            errors.append('Email already registered.')

        if errors:
            for error in errors:
                flash(error, 'danger')
            return redirect(url_for('main.register'))

        # Create user
        user = User(username=username, email=email)
        user.set_password(password)

        # Create default settings
        db.session.add(user)
        db.session.flush()

        user_settings = UserSettings(user_id=user.id)
        db.session.add(user_settings)

        db.session.commit()

        log_activity(f"New user registered: {username}", "SUCCESS")
        flash('Registration successful! Please login.', 'success')
        return redirect(url_for('main.login'))

    return render_template('register.html', title='Register')


# ==================== DASHBOARD & MAIN ROUTES ====================


@bp.route('/')
@bp.route('/index')
@login_required
def index():
    """Dashboard view."""
    all_campaigns = Campaign.query.filter_by(user_id=current_user.id)
    recent_campaigns = all_campaigns.order_by(Campaign.created_at.desc()).limit(10).all()
    total_campaigns = all_campaigns.count()
    active_campaigns = all_campaigns.filter_by(status='Sending').count()

    today = datetime.utcnow().date()
    week_ago = today - timedelta(days=7)

    recent_stats = db.session.query(
        func.coalesce(func.sum(DailyStats.emails_sent), 0).label('sent'),
        func.coalesce(func.sum(DailyStats.unique_opens), 0).label('opens'),
        func.coalesce(func.sum(DailyStats.unique_clicks), 0).label('clicks')
    ).filter(
        DailyStats.user_id == current_user.id,
        DailyStats.date >= week_ago
    ).first()

    stats = {
        'total_campaigns': total_campaigns,
        'active_campaigns': active_campaigns,
        'emails_sent_week': int(recent_stats.sent) if recent_stats and getattr(recent_stats, 'sent', None) is not None else 0,
        'opens_week': int(recent_stats.opens) if recent_stats and getattr(recent_stats, 'opens', None) is not None else 0,
        'clicks_week': int(recent_stats.clicks) if recent_stats and getattr(recent_stats, 'clicks', None) is not None else 0
    }

    notifications = Notification.query.filter_by(
        user_id=current_user.id,
        read=False
    ).order_by(Notification.created_at.desc()).limit(5).all()

    return render_template('dashboard.html',
                           title='Dashboard',
                           campaigns=all_campaigns,
                           recent_campaigns=recent_campaigns,
                           stats=stats,
                           notifications=notifications)


# ==================== CAMPAIGN ROUTES ====================


@bp.route('/campaign/<int:campaign_id>')
@login_required
def view_campaign(campaign_id):
    """View single campaign with recipients and analytics."""
    campaign = Campaign.query.get_or_404(campaign_id)

    if campaign.user_id != current_user.id:
        flash("You do not have permission to view this campaign.", "danger")
        return redirect(url_for('main.index'))

    page = request.args.get('page', 1, type=int)
    status_filter = request.args.get('status', '')
    search = request.args.get('search', '')

    query = campaign.recipients

    if status_filter:
        query = query.filter_by(status=status_filter)

    if search:
        query = query.filter(Recipient.email.ilike(f'%{search}%'))

    recipients = query.order_by(Recipient.id.asc()).paginate(
        page=page, per_page=50, error_out=False
    )

    analytics = campaign.get_analytics()

    ab_stats = None
    if campaign.ab_testing_enabled:
        a_recipients = campaign.recipients.filter_by(ab_version='A')
        b_recipients = campaign.recipients.filter_by(ab_version='B')

        a_sent = a_recipients.filter_by(status='Sent').count()
        b_sent = b_recipients.filter_by(status='Sent').count()
        a_opened = a_recipients.filter(Recipient.opened_at.isnot(None)).count()
        b_opened = b_recipients.filter(Recipient.opened_at.isnot(None)).count()
        a_clicked = a_recipients.filter(Recipient.clicked_at.isnot(None)).count()
        b_clicked = b_recipients.filter(Recipient.clicked_at.isnot(None)).count()

        ab_stats = {
            'a_sent': a_sent,
            'b_sent': b_sent,
            'a_opened': a_opened,
            'b_opened': b_opened,
            'a_clicked': a_clicked,
            'b_clicked': b_clicked,
            'a_open_rate': round((a_opened / a_sent * 100), 1) if a_sent > 0 else 0,
            'b_open_rate': round((b_opened / b_sent * 100), 1) if b_sent > 0 else 0,
            'a_click_rate': round((a_clicked / a_sent * 100), 1) if a_sent > 0 else 0,
            'b_click_rate': round((b_clicked / b_sent * 100), 1) if b_sent > 0 else 0,
        }

    status_counts = db.session.query(
        Recipient.status,
        func.count(Recipient.id)
    ).filter_by(campaign_id=campaign.id).group_by(Recipient.status).all()

    return render_template('campaign.html',
                           title=campaign.name,
                           campaign=campaign,
                           recipients=recipients,
                           analytics=analytics,
                           ab_stats=ab_stats,
                           status_counts=dict(status_counts),
                           status_filter=status_filter,
                           search=search)


@bp.route('/campaign/new', methods=['GET', 'POST'])
@login_required
def new_campaign():
    """Create new campaign."""
    smtp_profiles = SMTPServer.query.filter_by(user_id=current_user.id, is_active=True).all()
    templates = EmailTemplate.query.filter(
        (EmailTemplate.user_id == current_user.id) | (EmailTemplate.is_public == True)
    ).all()
    tags = Tag.query.filter_by(user_id=current_user.id).all()
    segments = Segment.query.filter_by(user_id=current_user.id).all()

    global_settings = get_or_create_global_settings()

    if request.method == 'POST':
        try:
            ab_enabled = 'ab_testing_enabled' in request.form
            tracking_enabled = 'tracking_enabled' in request.form
            warmup_mode = 'warmup_mode' in request.form
            smtp_rotation = 'smtp_rotation_enabled' in request.form
            smart_send = 'smart_send_enabled' in request.form

            body_html = request.form.get('body_html', '')
            body_plain = html_to_plain_text(body_html)

            campaign = Campaign(
                name=request.form.get('campaign_name', 'Untitled Campaign'),
                subject=request.form.get('subject', ''),
                preheader=request.form.get('preheader', ''),
                body_html=body_html,
                body_plain=body_plain,
                ab_testing_enabled=ab_enabled,
                subject_b=request.form.get('subject_b'),
                body_b=request.form.get('body_b'),
                ab_split_ratio=int(request.form.get('ab_split_ratio', 50)),
                burner_domain=request.form.get('burner_domain') or global_settings.burner_domain,
                lure_path=request.form.get('lure_path') or global_settings.lure_path,
                smtp_profile_id=int(request.form.get('smtp_profile_id')) if request.form.get('smtp_profile_id') else None,
                throttle_amount=int(request.form.get('throttle_amount', global_settings.default_throttle_amount or 20)),
                throttle_delay=int(request.form.get('throttle_delay', global_settings.default_throttle_delay or 60)),
                tracking_enabled=tracking_enabled,
                warmup_mode=warmup_mode,
                smtp_rotation_enabled=smtp_rotation,
                smart_send_enabled=smart_send,
                user_id=current_user.id,
                status='Draft'
            )

            scheduled_date = request.form.get('scheduled_date')
            scheduled_time = request.form.get('scheduled_time')
            if scheduled_date and scheduled_time:
                try:
                    campaign.scheduled_at = datetime.strptime(
                        f"{scheduled_date} {scheduled_time}",
                        "%Y-%m-%d %H:%M"
                    )
                    campaign.status = 'Scheduled'
                except ValueError:
                    pass

            template_id = request.form.get('template_id')
            if template_id:
                campaign.template_id = int(template_id)

            db.session.add(campaign)
            db.session.flush()

            file = request.files.get('recipients_file')
            if file and file.filename:
                recipients_added, errors = parse_csv_file(file, campaign.id)
                if errors:
                    for error in errors[:5]:
                        flash(error, 'warning')
                flash(f'Loaded {recipients_added} recipients.', 'info')
                # Update total_recipients to reflect inserted rows
                campaign.total_recipients = campaign.recipients.count()

            tag_ids = request.form.getlist('tags')
            for tag_id in tag_ids:
                tag = Tag.query.get(tag_id)
                if tag:
                    campaign.tags.append(tag)

            db.session.commit()

            log_user_activity('campaign_created', f'Created campaign: {campaign.name}', 'campaign', campaign.id)
            log_activity(f"Campaign '{campaign.name}' created with {campaign.total_recipients} recipients", "SUCCESS")

            flash('Campaign created successfully!', 'success')
            return redirect(url_for('main.view_campaign', campaign_id=campaign.id))

        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f"Error creating campaign: {e}")
            log_activity(f"Error creating campaign: {str(e)}", "ERROR")
            flash(f"Error creating campaign: {str(e)}", "danger")

    return render_template('create_campaign.html',
                           title='New Campaign',
                           smtp_profiles=smtp_profiles,
                           templates=templates,
                           tags=tags,
                           segments=segments,
                           default_burner=global_settings.burner_domain or '',
                           default_lure=global_settings.lure_path or '',
                           default_throttle_amount=global_settings.default_throttle_amount or 20,
                           default_throttle_delay=global_settings.default_throttle_delay or 60)


@bp.route('/campaign/<int:campaign_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_campaign(campaign_id):
    """Edit existing campaign."""
    campaign = Campaign.query.get_or_404(campaign_id)

    if campaign.user_id != current_user.id:
        flash("You do not have permission to edit this campaign.", "danger")
        return redirect(url_for('main.index'))

    if campaign.status == 'Sending':
        flash("Cannot edit a campaign that is currently sending.", "warning")
        return redirect(url_for('main.view_campaign', campaign_id=campaign.id))

    smtp_profiles = SMTPServer.query.filter_by(user_id=current_user.id, is_active=True).all()
    templates = EmailTemplate.query.filter(
        (EmailTemplate.user_id == current_user.id) | (EmailTemplate.is_public == True)
    ).all()

    if request.method == 'POST':
        try:
            campaign.name = request.form.get('campaign_name', campaign.name)
            campaign.subject = request.form.get('subject', campaign.subject)
            campaign.preheader = request.form.get('preheader', '')
            campaign.body_html = request.form.get('body_html', campaign.body_html)
            campaign.body_plain = html_to_plain_text(campaign.body_html)

            campaign.ab_testing_enabled = 'ab_testing_enabled' in request.form
            campaign.subject_b = request.form.get('subject_b')
            campaign.body_b = request.form.get('body_b')
            campaign.ab_split_ratio = int(request.form.get('ab_split_ratio', 50))

            campaign.smtp_profile_id = int(request.form.get('smtp_profile_id')) if request.form.get('smtp_profile_id') else None
            campaign.throttle_amount = int(request.form.get('throttle_amount', 20))
            campaign.throttle_delay = int(request.form.get('throttle_delay', 60))

            campaign.tracking_enabled = 'tracking_enabled' in request.form
            campaign.warmup_mode = 'warmup_mode' in request.form
            campaign.smtp_rotation_enabled = 'smtp_rotation_enabled' in request.form

            scheduled_date = request.form.get('scheduled_date')
            scheduled_time = request.form.get('scheduled_time')
            if scheduled_date and scheduled_time:
                try:
                    campaign.scheduled_at = datetime.strptime(
                        f"{scheduled_date} {scheduled_time}",
                        "%Y-%m-%d %H:%M"
                    )
                    if campaign.status == 'Draft':
                        campaign.status = 'Scheduled'
                except ValueError:
                    pass
            else:
                campaign.scheduled_at = None
                if campaign.status == 'Scheduled':
                    campaign.status = 'Draft'

            campaign.updated_at = datetime.utcnow()
            db.session.commit()

            log_user_activity('campaign_updated', f'Updated campaign: {campaign.name}', 'campaign', campaign.id)
            flash('Campaign updated successfully!', 'success')
            return redirect(url_for('main.view_campaign', campaign_id=campaign.id))

        except Exception as e:
            db.session.rollback()
            flash(f"Error updating campaign: {str(e)}", "danger")

    return render_template('edit_campaign.html',
                           title=f'Edit: {campaign.name}',
                           campaign=campaign,
                           smtp_profiles=smtp_profiles,
                           templates=templates)


@bp.route('/campaign/<int:campaign_id>/duplicate', methods=['POST'])
@login_required
def duplicate_campaign(campaign_id):
    """Duplicate an existing campaign."""
    campaign = Campaign.query.get_or_404(campaign_id)

    if campaign.user_id != current_user.id:
        flash("You do not have permission.", "danger")
        return redirect(url_for('main.index'))

    try:
        new_campaign_obj = Campaign(
            name=f"{campaign.name} (Copy)",
            subject=campaign.subject,
            preheader=campaign.preheader,
            body_html=campaign.body_html,
            body_plain=campaign.body_plain,
            ab_testing_enabled=campaign.ab_testing_enabled,
            subject_b=campaign.subject_b,
            body_b=campaign.body_b,
            ab_split_ratio=campaign.ab_split_ratio,
            burner_domain=campaign.burner_domain,
            lure_path=campaign.lure_path,
            smtp_profile_id=campaign.smtp_profile_id,
            throttle_amount=campaign.throttle_amount,
            throttle_delay=campaign.throttle_delay,
            tracking_enabled=campaign.tracking_enabled,
            warmup_mode=campaign.warmup_mode,
            smtp_rotation_enabled=campaign.smtp_rotation_enabled,
            user_id=current_user.id,
            status='Draft'
        )

        db.session.add(new_campaign_obj)
        db.session.commit()

        log_user_activity('campaign_duplicated', f'Duplicated campaign: {campaign.name}', 'campaign', new_campaign_obj.id)
        flash('Campaign duplicated successfully!', 'success')
        return redirect(url_for('main.edit_campaign', campaign_id=new_campaign_obj.id))

    except Exception as e:
        db.session.rollback()
        flash(f"Error duplicating campaign: {str(e)}", "danger")
        return redirect(url_for('main.view_campaign', campaign_id=campaign.id))


@bp.route('/campaign/<int:campaign_id>/add_recipient', methods=['POST'])
@login_required
def add_recipient_manual(campaign_id):
    """Manually add a recipient to campaign."""
    campaign = Campaign.query.get_or_404(campaign_id)

    if campaign.user_id != current_user.id:
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403

    email = request.form.get('email', '').strip().lower()

    if not email:
        return jsonify({'success': False, 'message': 'Email required'})

    if not is_valid_email(email):
        return jsonify({'success': False, 'message': 'Invalid email format'})

    exists = Recipient.query.filter_by(campaign_id=campaign.id, email=email).first()
    if exists:
        return jsonify({'success': False, 'message': 'Email already in list'})

    is_suppressed = Suppression.query.filter_by(email=email).first()

    recipient = Recipient(
        email=email,
        campaign_id=campaign.id,
        data=json.dumps({'email': email}),
        status='Suppressed' if is_suppressed else 'Queued',
        status_message='Suppressed by global list' if is_suppressed else None
    )

    db.session.add(recipient)
    db.session.flush()
    campaign.total_recipients = campaign.recipients.count()
    db.session.commit()

    log_activity(f"Manually added {email} to campaign {campaign.name}", "INFO")
    return jsonify({'success': True, 'message': 'Recipient added'})


@bp.route('/campaign/<int:campaign_id>/control/<action>')
@login_required
def campaign_control(campaign_id, action):
    """Control campaign (start, pause, stop, retry)."""
    campaign = Campaign.query.get_or_404(campaign_id)

    if campaign.user_id != current_user.id:
        flash("You do not have permission.", "danger")
        return redirect(url_for('main.index'))

    try:
        if action == 'start':
            queued_count = campaign.recipients.filter_by(status='Queued').count()

            if queued_count == 0:
                flash('No queued recipients to send to.', 'warning')
                return redirect(url_for('main.view_campaign', campaign_id=campaign.id))

            if not campaign.smtp_profile and not campaign.smtp_rotation_enabled:
                flash('No SMTP profile configured for this campaign.', 'danger')
                return redirect(url_for('main.view_campaign', campaign_id=campaign.id))

            if campaign.smtp_profile:
                smtp_config = campaign.smtp_profile.to_dict()
                if not smtp_config.get('password'):
                    flash('SMTP password not configured. Please update your SMTP profile.', 'danger')
                    return redirect(url_for('main.view_campaign', campaign_id=campaign.id))

            campaign.status = 'Sending'
            campaign.started_at = datetime.utcnow()
            db.session.commit()

            from app.tasks import send_campaign_task
            send_campaign_task.delay(campaign_id)

            log_user_activity('campaign_started', f'Started campaign: {campaign.name}', 'campaign', campaign.id)
            log_activity(f"Started campaign: {campaign.name}", "SUCCESS")

            emit_campaign_update(campaign_id, {'status': 'Sending', 'action': 'started'})

            flash('Campaign started successfully!', 'success')

        elif action == 'pause':
            if campaign.status != 'Sending':
                flash('Campaign is not currently sending.', 'warning')
                return redirect(url_for('main.view_campaign', campaign_id=campaign.id))

            campaign.status = 'Paused'
            db.session.commit()

            log_user_activity('campaign_paused', f'Paused campaign: {campaign.name}', 'campaign', campaign.id)
            log_activity(f"Paused campaign: {campaign.name}", "WARNING")
            emit_campaign_update(campaign_id, {'status': 'Paused', 'action': 'paused'})

            flash('Campaign paused.', 'warning')

        elif action == 'resume':
            # Support both 'resume' and 'start' flows: in UI resume often uses 'start'
            if campaign.status not in ['Paused', 'Draft', 'Stopped']:
                flash('Campaign cannot be resumed from its current state.', 'warning')
                return redirect(url_for('main.view_campaign', campaign_id=campaign.id))

            campaign.status = 'Sending'
            db.session.commit()

            from app.tasks import send_campaign_task
            send_campaign_task.delay(campaign_id)

            log_activity(f"Resumed campaign: {campaign.name}", "SUCCESS")
            emit_campaign_update(campaign_id, {'status': 'Sending', 'action': 'resumed'})

            flash('Campaign resumed.', 'success')

        elif action == 'stop':
            if campaign.status not in ['Sending', 'Paused']:
                flash('Campaign is not active.', 'warning')
                return redirect(url_for('main.view_campaign', campaign_id=campaign.id))

            campaign.status = 'Stopped'
            campaign.completed_at = datetime.utcnow()
            db.session.commit()

            log_user_activity('campaign_stopped', f'Stopped campaign: {campaign.name}', 'campaign', campaign.id)
            log_activity(f"Stopped campaign: {campaign.name}", "ERROR")
            emit_campaign_update(campaign_id, {'status': 'Stopped', 'action': 'stopped'})

            flash('Campaign stopped.', 'danger')

        elif action == 'retry':
            failed = campaign.recipients.filter(Recipient.status.in_(['Failed', 'Bounced'])).all()

            for r in failed:
                r.status = 'Queued'
                r.status_message = None
                r.attempts = 0

            db.session.commit()

            log_activity(f"Queued {len(failed)} failed recipients for retry.", "INFO")
            flash(f'Queued {len(failed)} failed recipients for retry.', 'info')

    except Exception as e:
        db.session.rollback()
        log_activity(f"Campaign control error ({action}): {str(e)}", "ERROR")
        flash(f"Error: {str(e)}", "danger")

    return redirect(url_for('main.view_campaign', campaign_id=campaign.id))


@bp.route('/campaign/<int:campaign_id>/validate_list')
@login_required
def validate_list(campaign_id):
    """Validate recipient list with MX checks."""
    campaign = Campaign.query.get_or_404(campaign_id)

    if campaign.user_id != current_user.id:
        flash("You do not have permission.", "danger")
        return redirect(url_for('main.index'))

    recipients = campaign.recipients.filter_by(status='Queued').limit(500).all()
    helper = DeliverabilityHelper()

    valid_count = 0
    invalid_count = 0

    for r in recipients:
        try:
            domain = r.email.split('@')[1]
            mx_result = helper.check_mx_record(domain)

            status = None
            if isinstance(mx_result, tuple):
                # Some helper implementations return (status, records)
                status = mx_result[0]
            else:
                status = mx_result

            # Normalize 'Valid' check
            if isinstance(status, str) and status.lower().startswith("valid"):
                valid_count += 1
            else:
                r.status = 'Invalid'
                r.status_message = f"MX Check: {status}"
                invalid_count += 1
        except Exception:
            r.status = 'Invalid'
            r.status_message = "Invalid email format"
            invalid_count += 1

    db.session.commit()

    log_activity(f"Validated {valid_count + invalid_count} recipients. {valid_count} valid, {invalid_count} invalid.", "INFO")
    flash(f"Validated {valid_count + invalid_count} emails. {valid_count} valid, {invalid_count} invalid.", "info")

    return redirect(url_for('main.view_campaign', campaign_id=campaign.id))


@bp.route('/campaign/<int:campaign_id>/clear_list')
@login_required
def clear_recipient_list(campaign_id):
    """Clear all recipients from campaign."""
    campaign = Campaign.query.get_or_404(campaign_id)

    if campaign.user_id != current_user.id:
        flash("You do not have permission.", "danger")
        return redirect(url_for('main.index'))

    try:
        Recipient.query.filter_by(campaign_id=campaign.id).delete()
        campaign.total_recipients = 0
        db.session.commit()

        log_user_activity('recipients_cleared', f'Cleared recipient list for {campaign.name}', 'campaign', campaign.id)
        log_activity(f"Cleared recipient list for {campaign.name}", "WARNING")
        flash("Recipient list cleared.", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Error: {e}", "danger")

    return redirect(url_for('main.view_campaign', campaign_id=campaign.id))


@bp.route('/campaign/<int:campaign_id>/export')
@login_required
def export_campaign_report(campaign_id):
    """Export campaign report as CSV."""
    campaign = Campaign.query.get_or_404(campaign_id)

    if campaign.user_id != current_user.id:
        flash("You do not have permission.", "danger")
        return redirect(url_for('main.index'))

    recipients = campaign.recipients.all()

    def generate():
        data = io.StringIO()
        w = csv.writer(data)
        w.writerow([
            'Email', 'Status', 'AB Version', 'Sent At', 'Opened At', 'Clicked At',
            'Open Count', 'Click Count', 'Engagement Score', 'Attempts', 'Error Message'
        ])
        yield data.getvalue()
        data.seek(0)
        data.truncate(0)

        for r in recipients:
            w.writerow([
                r.email,
                r.status,
                r.ab_version or '',
                r.sent_at.strftime('%Y-%m-%d %H:%M:%S') if r.sent_at else '',
                r.opened_at.strftime('%Y-%m-%d %H:%M:%S') if r.opened_at else '',
                r.clicked_at.strftime('%Y-%m-%d %H:%M:%S') if r.clicked_at else '',
                r.open_count or 0,
                r.click_count or 0,
                r.engagement_score or 0,
                r.attempts or 0,
                r.status_message or ''
            ])
            yield data.getvalue()
            data.seek(0)
            data.truncate(0)

    response = Response(generate(), mimetype='text/csv')
    response.headers.set("Content-Disposition", "attachment",
                         filename=f"campaign_{campaign.id}_report.csv")
    return response


@bp.route('/campaign/<int:campaign_id>/delete', methods=['POST'])
@login_required
def delete_campaign(campaign_id):
    """Delete a campaign."""
    campaign = Campaign.query.get_or_404(campaign_id)

    if campaign.user_id != current_user.id:
        flash("You do not have permission.", "danger")
        return redirect(url_for('main.index'))

    if campaign.status == 'Sending':
        flash("Cannot delete a campaign that is currently sending. Stop it first.", "danger")
        return redirect(url_for('main.view_campaign', campaign_id=campaign.id))

    try:
        campaign_name = campaign.name
        db.session.delete(campaign)
        db.session.commit()

        log_user_activity('campaign_deleted', f'Deleted campaign: {campaign_name}')
        log_activity(f"Deleted campaign: {campaign_name}", "WARNING")
        flash("Campaign deleted.", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Error deleting campaign: {e}", "danger")

    return redirect(url_for('main.index'))


# ==================== SMTP SETTINGS ====================


@bp.route('/settings/smtp', methods=['GET', 'POST'])
@login_required
def smtp_profiles():
    """Manage SMTP profiles."""
    if request.method == 'POST':
        try:
            profile_id = request.form.get('profile_id')

            if profile_id:
                profile = SMTPServer.query.get(profile_id)
                if not profile or profile.user_id != current_user.id:
                    flash("Profile not found.", "danger")
                    return redirect(url_for('main.smtp_profiles'))
            else:
                profile = SMTPServer(user_id=current_user.id)

            profile.profile_name = request.form.get('name', '')
            profile.server = request.form.get('server', '')
            profile.port = int(request.form.get('port', 587))
            profile.username = request.form.get('username', '')
            profile.sender_name = request.form.get('sender_name', '')
            profile.sender_email = request.form.get('sender_email', '')
            profile.reply_to_email = request.form.get('reply_to_email', '')
            profile.use_tls = 'use_tls' in request.form
            profile.use_ssl = 'use_ssl' in request.form
            profile.is_active = 'is_active' in request.form
            profile.daily_limit = int(request.form.get('daily_limit', 500))
            profile.hourly_limit = int(request.form.get('hourly_limit', 100))
            profile.priority = int(request.form.get('priority', 1))

            profile.warmup_enabled = 'warmup_enabled' in request.form
            if profile.warmup_enabled and not profile.warmup_start_date:
                profile.warmup_start_date = datetime.utcnow().date()

            profile.imap_server = request.form.get('imap_server', '')
            profile.imap_port = int(request.form.get('imap_port', 993))
            profile.imap_username = request.form.get('imap_username', '')

            imap_password = request.form.get('imap_password')
            if imap_password and imap_password.strip():
                profile.set_imap_password(imap_password)

            password = request.form.get('password')
            if password and password.strip():
                profile.set_password(password)

            db.session.add(profile)
            db.session.commit()

            log_user_activity('smtp_profile_saved', f'SMTP Profile saved: {profile.profile_name}', 'smtp_server', profile.id)
            log_activity(f"SMTP Profile saved: {profile.profile_name}", "SUCCESS")
            flash('SMTP Profile Saved!', 'success')

        except Exception as e:
            db.session.rollback()
            log_activity(f"Error saving SMTP profile: {e}", "ERROR")
            flash(f"Error saving profile: {str(e)}", "danger")

        return redirect(url_for('main.smtp_profiles'))

    profiles = SMTPServer.query.filter_by(user_id=current_user.id).order_by(SMTPServer.priority).all()
    return render_template('smtp_profiles.html', title='SMTP Profiles', profiles=profiles)


@bp.route('/settings/smtp/test', methods=['POST'])
@login_required
def test_smtp_connection():
    """Test SMTP connection."""
    try:
        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'message': 'No data provided'}), 400

        profile_id = data.get('profile_id')
        if not profile_id:
            return jsonify({'success': False, 'message': 'Profile ID required'}), 400

        profile = SMTPServer.query.get(profile_id)
        if not profile:
            return jsonify({'success': False, 'message': 'Profile not found'}), 404

        if profile.user_id != current_user.id:
            return jsonify({'success': False, 'message': 'Unauthorized'}), 403

        smtp_config = profile.to_dict()
        if not smtp_config.get('password'):
            return jsonify({'success': False, 'message': 'Password not set for this profile'}), 400

        handler = SMTPHandler(smtp_config)
        success, msg = handler.test_connection()

        profile.last_test_at = datetime.utcnow()
        profile.last_test_result = success
        db.session.commit()

        if success:
            log_activity(f"SMTP Test successful for {profile.profile_name}", "SUCCESS")
            return jsonify({'success': True, 'message': f'Connection successful!'})
        else:
            log_activity(f"SMTP Test failed for {profile.profile_name}: {msg}", "ERROR")
            return jsonify({'success': False, 'message': f'Failed: {msg}'})

    except Exception as e:
        log_activity(f"SMTP Test error: {str(e)}", "ERROR")
        return jsonify({'success': False, 'message': f'Error: {str(e)}'}), 500


@bp.route('/settings/smtp/delete/<int:profile_id>', methods=['POST'])
@login_required
def delete_smtp_profile(profile_id):
    """Delete SMTP profile."""
    profile = SMTPServer.query.get_or_404(profile_id)

    if profile.user_id != current_user.id:
        flash("You do not have permission.", "danger")
        return redirect(url_for('main.smtp_profiles'))

    campaigns_using = Campaign.query.filter_by(smtp_profile_id=profile_id).count()
    if campaigns_using > 0:
        flash(f'Cannot delete: {campaigns_using} campaigns are using this profile.', 'danger')
        return redirect(url_for('main.smtp_profiles'))

    try:
        profile_name = profile.profile_name
        db.session.delete(profile)
        db.session.commit()

        log_user_activity('smtp_profile_deleted', f'Deleted SMTP profile: {profile_name}')
        flash('Profile deleted.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f"Error: {e}", "danger")

    return redirect(url_for('main.smtp_profiles'))


# ==================== SUPPRESSION LIST ====================


@bp.route('/settings/suppression', methods=['GET', 'POST'])
@login_required
def suppression_list():
    """Manage suppression list."""
    form = SuppressionForm()

    if form.validate_on_submit():
        email = form.email.data.lower().strip()

        if not Suppression.query.filter_by(email=email).first():
            suppression = Suppression(
                email=email,
                reason=form.reason.data or 'Manual',
                source='manual',
                user_id=current_user.id
            )
            db.session.add(suppression)
            db.session.commit()

            log_activity(f"Suppressed: {email}", "WARNING")
            flash(f'{email} added to suppression list.', 'success')
        else:
            flash(f'{email} is already suppressed.', 'warning')

        return redirect(url_for('main.suppression_list'))

    page = request.args.get('page', 1, type=int)
    search = request.args.get('search', '')

    query = Suppression.query
    if search:
        query = query.filter(Suppression.email.ilike(f'%{search}%'))

    pagination = query.order_by(Suppression.created_at.desc()).paginate(
        page=page, per_page=50, error_out=False
    )

    return render_template('suppression.html',
                           title='Suppression List',
                           form=form,
                           pagination=pagination,
                           search=search)


@bp.route('/settings/suppression/delete/<int:suppressed_id>', methods=['POST'])
@login_required
def delete_suppressed_email(suppressed_id):
    """Remove email from suppression list."""
    item = Suppression.query.get_or_404(suppressed_id)

    try:
        email = item.email
        db.session.delete(item)
        db.session.commit()

        log_activity(f"Removed from suppression: {email}", "INFO")
        flash('Removed from suppression list.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f"Error: {e}", "danger")

    return redirect(url_for('main.suppression_list'))


@bp.route('/settings/suppression/import', methods=['POST'])
@login_required
def import_suppression_list():
    """Import suppression list from CSV."""
    file = request.files.get('file')

    if not file or not file.filename:
        flash('No file selected.', 'danger')
        return redirect(url_for('main.suppression_list'))

    try:
        stream = io.StringIO(file.stream.read().decode("UTF-8"), newline=None)
        csv_reader = csv.reader(stream)

        count = 0
        skipped = 0

        for row in csv_reader:
            if row and row[0]:
                email = row[0].strip().lower()

                if is_valid_email(email) and not Suppression.query.filter_by(email=email).first():
                    reason = row[1] if len(row) > 1 else "Imported"
                    suppression = Suppression(
                        email=email,
                        reason=reason,
                        source='import',
                        user_id=current_user.id
                    )
                    db.session.add(suppression)
                    count += 1
                else:
                    skipped += 1

        db.session.commit()

        log_activity(f"Imported {count} emails to suppression list", "SUCCESS")
        flash(f'Imported {count} emails. Skipped {skipped}.', 'success')

    except Exception as e:
        db.session.rollback()
        flash(f'Error importing: {e}', 'danger')

    return redirect(url_for('main.suppression_list'))


@bp.route('/settings/suppression/export')
@login_required
def export_suppression_list():
    """Export suppression list as CSV."""
    items = Suppression.query.all()

    def generate():
        data = io.StringIO()
        w = csv.writer(data)
        w.writerow(['Email', 'Reason', 'Source', 'Date Added'])
        yield data.getvalue()
        data.seek(0)
        data.truncate(0)

        for item in items:
            w.writerow([
                item.email,
                item.reason or '',
                item.source or '',
                item.created_at.strftime('%Y-%m-%d %H:%M:%S') if item.created_at else ''
            ])
            yield data.getvalue()
            data.seek(0)
            data.truncate(0)

    response = Response(generate(), mimetype='text/csv')
    response.headers.set("Content-Disposition", "attachment", filename="suppression_list.csv")
    return response


# ==================== GLOBAL SETTINGS ====================


@bp.route('/settings/general', methods=['GET', 'POST'])
@login_required
def general_settings():
    """Manage global settings."""
    settings = get_or_create_global_settings()

    if request.method == 'POST':
        try:
            settings.burner_domain = request.form.get('burner_domain', '')
            settings.lure_path = request.form.get('lure_path', '')
            settings.default_throttle_amount = int(request.form.get('default_throttle_amount', 20))
            settings.default_throttle_delay = int(request.form.get('default_throttle_delay', 60))
            settings.default_tracking_domain = request.form.get('default_tracking_domain', '')

            settings.ai_provider = request.form.get('ai_provider', 'openai')
            settings.local_ai_url = request.form.get('local_ai_url', '')

            settings.gdpr_enabled = 'gdpr_enabled' in request.form
            settings.data_retention_days = int(request.form.get('data_retention_days', 365))

            pdf_file = request.files.get('template_pdf')
            if pdf_file and pdf_file.filename:
                if allowed_file(pdf_file.filename, {'pdf'}):
                    filename = secure_filename(pdf_file.filename)
                    upload_folder = current_app.config.get('UPLOAD_FOLDER', 'uploads')
                    os.makedirs(upload_folder, exist_ok=True)
                    path = os.path.join(upload_folder, filename)
                    pdf_file.save(path)
                    settings.template_pdf_path = path
                    log_activity(f"New PDF template uploaded: {filename}", "INFO")
                else:
                    flash("Invalid file type. Only PDF allowed.", "warning")

            settings.updated_at = datetime.utcnow()
            db.session.commit()

            log_user_activity('settings_updated', 'Global settings updated')
            log_activity("Global settings updated.", "SUCCESS")
            flash("Settings updated successfully.", "success")

        except Exception as e:
            db.session.rollback()
            flash(f"Error: {e}", "danger")

        return redirect(url_for('main.general_settings'))

    return render_template('settings_general.html', title='General Settings', settings=settings)


# ==================== DELIVERABILITY TOOLS ====================


@bp.route('/tools/deliverability', methods=['GET', 'POST'])
@login_required
def deliverability_tools():
    """Deliverability checking tools."""
    form = DeliverabilityForm()
    results = None
    helper = DeliverabilityHelper()

    if form.validate_on_submit():
        target = form.domain_ip.data.strip()

        if form.check_auth.data:
            auth_results = helper.check_domain_authentication(target)
            results = {
                'type': 'auth',
                'target': target,
                'auth': auth_results
            }
        elif form.check_blacklist.data:
            blacklist_result = helper.check_blacklist(target)
            results = {
                'type': 'blacklist',
                'target': target,
                'blacklist': blacklist_result
            }

    return render_template('deliverability.html',
                           title='Deliverability Tools',
                           form=form,
                           results=results)


@bp.route('/tools/spam_check', methods=['POST'])
@login_required
def spam_check():
    """Perform spam check on content."""
    try:
        data = request.get_json()
        subject = data.get('subject', '')
        body = data.get('body', '')
        check_type = data.get('type', 'basic')

        helper = DeliverabilityHelper()

        if check_type == 'ai':
            ai_handler = AIHandler()
            success, result = ai_handler.analyze_for_spam(subject, body)
            return jsonify({'success': success, 'result': result})
        else:
            result = helper.basic_spam_check(subject, body)
            return jsonify({'success': True, 'result': result})

    except Exception as e:
        return jsonify({'success': False, 'result': str(e)})


@bp.route('/tools/link_check', methods=['POST'])
@login_required
def link_check():
    """Check health of links in content."""
    try:
        data = request.get_json()
        content = data.get('content', '')

        helper = DeliverabilityHelper()
        results = helper.check_link_health(content)

        return jsonify({'success': True, 'results': results})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@bp.route('/tools/ajax_analyze', methods=['POST'])
@login_required
def deliverability_tools_ajax():
    """AI-powered deliverability analysis."""
    try:
        data = request.get_json()
        ai_handler = AIHandler()

        success, result = ai_handler.analyze_for_spam(
            data.get('subject', ''),
            data.get('body', '')
        )

        return jsonify({'success': success, 'result': result})
    except Exception as e:
        return jsonify({'success': False, 'result': str(e)})


# ==================== AI TOOLS ====================


@bp.route('/tools/ai_rewrite', methods=['POST'])
@login_required
def ai_rewrite():
    """AI rewrite content."""
    try:
        data = request.get_json()
        content = data.get('content')

        if not content:
            return jsonify({'success': False, 'result': 'No content provided'})

        ai_handler = AIHandler()
        success, result = ai_handler.rewrite_content(content)

        return jsonify({'success': success, 'result': result})
    except Exception as e:
        return jsonify({'success': False, 'result': str(e)})


@bp.route('/tools/ai_subject', methods=['POST'])
@login_required
def ai_subject():
    """AI generate subject lines."""
    try:
        data = request.get_json()
        content = data.get('content')

        if not content:
            return jsonify({'success': False, 'result': 'No content provided'})

        ai_handler = AIHandler()
        success, result = ai_handler.generate_subjects(content)

        return jsonify({'success': success, 'result': result})
    except Exception as e:
        return jsonify({'success': False, 'result': str(e)})


@bp.route('/tools/css_inline', methods=['POST'])
@login_required
def css_inline():
    """Inline CSS in HTML content."""
    try:
        data = request.get_json()
        content = data.get('content', '')

        try:
            import css_inline
            inliner = css_inline.CSSInliner()
            result = inliner.inline(content)
            return jsonify({'success': True, 'result': result})
        except ImportError:
            return jsonify({'success': False, 'result': 'css_inline library not installed'})
    except Exception as e:
        return jsonify({'success': False, 'result': str(e)})


# ==================== API LOGS ROUTE ====================


@bp.route('/api/logs')
@login_required
def api_get_logs():
    """Get recent activity logs."""
    logs = get_logs()
    recent_logs = logs[:10]

    log_list = []
    for entry in recent_logs:
        # get_logs returns dict entries with string timestamp, message, level
        ts = entry.get('timestamp') if isinstance(entry, dict) else getattr(entry, 'timestamp', '')
        level = entry.get('level') if isinstance(entry, dict) else getattr(entry, 'level', '')
        message = entry.get('message') if isinstance(entry, dict) else getattr(entry, 'message', '')
        log_list.append({
            'timestamp': ts,
            'level': level,
            'message': message
        })

    return jsonify(log_list)


# ==================== CAMPAIGN STATUS API ====================


@bp.route('/api/campaign/<int:campaign_id>/status')
@login_required
def api_campaign_status(campaign_id):
    """Get live status of a campaign."""
    campaign = Campaign.query.get_or_404(campaign_id)

    if campaign.user_id != current_user.id:
        return jsonify({'error': 'Unauthorized'}), 403

    total = int(campaign.total_recipients or 0)
    sent = campaign.recipients.filter_by(status='Sent').count()
    failed = campaign.recipients.filter_by(status='Failed').count()

    progress = round((sent / total * 100), 1) if total > 0 else 0.0

    return jsonify({
        'status': campaign.status,
        'sent': sent,
        'failed': failed,
        'total': total,
        'progress': progress
    })


# ==================== ANALYTICS ====================


@bp.route('/analytics')
@login_required
def analytics_dashboard():
    """Analytics dashboard."""
    days = request.args.get('days', 30, type=int)
    end_date = datetime.utcnow().date()
    start_date = end_date - timedelta(days=days)

    daily_stats = db.session.query(
        DailyStats.date,
        func.coalesce(func.sum(DailyStats.emails_sent), 0).label('sent'),
        func.coalesce(func.sum(DailyStats.unique_opens), 0).label('opens'),
        func.coalesce(func.sum(DailyStats.unique_clicks), 0).label('clicks')
    ).filter(
        DailyStats.user_id == current_user.id,
        DailyStats.date >= start_date
    ).group_by(DailyStats.date).order_by(DailyStats.date).all()

    daily_labels_list = [stat.date.strftime('%Y-%m-%d') for stat in daily_stats]
    daily_counts_list = [int(stat.sent or 0) for stat in daily_stats]

    daily_data = {
        'chart_labels': daily_labels_list,
        'chart_data': daily_counts_list
    }

    hourly_stats = db.session.query(
        HourlyStats.hour_of_day,
        func.coalesce(func.sum(HourlyStats.total_opens), 0).label('opens')
    ).filter(HourlyStats.user_id == current_user.id).group_by(HourlyStats.hour_of_day).all()

    hourly_labels_list = [f"{i}:00" for i in range(24)]
    hourly_counts_list = [0] * 24

    for stat in hourly_stats:
        hour = int(getattr(stat, 'hour_of_day', 0))
        if 0 <= hour < 24:
            hourly_counts_list[hour] = int(getattr(stat, 'opens', 0) or 0)

    hourly_data = {
        'chart_labels': hourly_labels_list,
        'chart_data': hourly_counts_list
    }

    total_sent = db.session.query(func.count(Recipient.id)).join(Campaign).filter(
        Campaign.user_id == current_user.id, Recipient.status == 'Sent'
    ).scalar() or 0

    total_failed = db.session.query(func.count(Recipient.id)).join(Campaign).filter(
        Campaign.user_id == current_user.id, Recipient.status == 'Failed'
    ).scalar() or 0

    total_opens = db.session.query(func.count(Recipient.id)).join(Campaign).filter(
        Campaign.user_id == current_user.id, Recipient.opened_at != None
    ).scalar() or 0

    total_clicks = db.session.query(func.count(Recipient.id)).join(Campaign).filter(
        Campaign.user_id == current_user.id, Recipient.clicked_at != None
    ).scalar() or 0

    open_rate = round((total_opens / total_sent * 100), 1) if total_sent > 0 else 0
    click_rate = round((total_clicks / total_sent * 100), 1) if total_sent > 0 else 0

    summary = {
        'total_sent': int(total_sent),
        'total_failed': int(total_failed),
        'total_opens': int(total_opens),
        'total_clicks': int(total_clicks),
        'avg_open_rate': open_rate,
        'avg_click_rate': click_rate
    }

    return render_template('analytics.html',
                           title='Analytics',
                           summary=summary,
                           daily_data=daily_data,
                           hourly_data=hourly_data,
                           days=days)


@bp.route('/settings/suppression/bulk_add', methods=['POST'])
@login_required
def bulk_add_suppression():
    """Bulk add emails to suppression list from text input."""
    emails_text = request.form.get('emails', '')
    reason = request.form.get('reason', 'Manual')

    if not emails_text.strip():
        flash('No emails provided.', 'warning')
        return redirect(url_for('main.suppression_list'))

    emails_text = emails_text.replace(',', '\n')
    emails = [e.strip().lower() for e in emails_text.split('\n') if e.strip()]

    count = 0
    duplicates = 0
    invalid = 0

    email_regex = re.compile(r'^[^\s@]+@[^\s@]+\.[^\s@]+$')

    for email in emails:
        if not email_regex.match(email):
            invalid += 1
            continue

        if Suppression.query.filter_by(email=email).first():
            duplicates += 1
            continue

        suppression = Suppression(email=email, reason=reason, user_id=current_user.id)
        db.session.add(suppression)
        count += 1

    try:
        db.session.commit()
        flash(f'Added {count} emails to suppression list. {duplicates} duplicates skipped. {invalid} invalid.', 'success')
        log_activity(f"Bulk added {count} emails to suppression list", "SUCCESS")
    except Exception as e:
        db.session.rollback()
        flash(f'Error adding emails: {str(e)}', 'danger')

    return redirect(url_for('main.suppression_list'))


@bp.route('/settings/suppression/bulk_delete', methods=['POST'])
@login_required
def bulk_delete_suppression():
    """Bulk delete emails from suppression list."""
    ids = request.form.getlist('ids')

    if not ids:
        flash('No emails selected.', 'warning')
        return redirect(url_for('main.suppression_list'))

    try:
        count = Suppression.query.filter(Suppression.id.in_(ids)).delete(synchronize_session=False)
        db.session.commit()
        flash(f'Removed {count} emails from suppression list.', 'success')
        log_activity(f"Bulk deleted {count} emails from suppression list", "WARNING")
    except Exception as e:
        db.session.rollback()
        flash(f'Error deleting emails: {str(e)}', 'danger')

    return redirect(url_for('main.suppression_list'))


# ==================== API KEY MANAGEMENT ====================


@bp.route('/settings/api-keys')
@login_required
def api_keys():
    """Manage API Keys."""
    keys = APIKey.query.filter_by(user_id=current_user.id).all()
    # Check if a new key was just created and stored in session/flash (handled in template)
    return render_template('api_keys.html', title='API Keys', api_keys=keys)


@bp.route('/settings/api-keys/create', methods=['POST'])
@login_required
def create_api_key():
    """Create a new API Key."""
    name = request.form.get('name', 'New API Key').strip()
    scopes_list = request.form.getlist('scopes')  # ['read', 'write', 'send']
    expires_in_days = int(request.form.get('expires_in', 0))

    if not name:
        flash('Key name is required.', 'danger')
        return redirect(url_for('main.api_keys'))

    try:
        # Generate raw key
        raw_key = APIKey.generate_key()
        
        # Calculate expiry
        expires_at = None
        if expires_in_days > 0:
            expires_at = datetime.utcnow() + timedelta(days=expires_in_days)

        api_key = APIKey(
            name=name,
            user_id=current_user.id,
            expires_at=expires_at
        )
        api_key.set_key(raw_key)
        api_key.set_scopes(scopes_list)

        db.session.add(api_key)
        db.session.commit()

        log_user_activity('api_key_created', f'Created API key: {name}')
        log_activity(f"API Key created: {name}", "SUCCESS")

        # Pass raw key to template ONLY ONCE
        return render_template('api_keys.html', 
                               title='API Keys', 
                               api_keys=APIKey.query.filter_by(user_id=current_user.id).all(),
                               new_api_key=raw_key)

    except Exception as e:
        db.session.rollback()
        flash(f"Error creating API key: {e}", "danger")
        return redirect(url_for('main.api_keys'))


@bp.route('/settings/api-keys/revoke/<int:key_id>', methods=['POST'])
@login_required
def revoke_api_key(key_id):
    """Revoke an API Key."""
    key = APIKey.query.get_or_404(key_id)

    if key.user_id != current_user.id:
        flash("You do not have permission.", "danger")
        return redirect(url_for('main.api_keys'))

    try:
        db.session.delete(key)
        db.session.commit()
        log_user_activity('api_key_revoked', f'Revoked API key: {key.name}')
        flash('API Key revoked.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f"Error revoking key: {e}", "danger")

    return redirect(url_for('main.api_keys'))
