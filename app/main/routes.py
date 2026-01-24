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

# ==================== AUTHENTICATION ROUTES ====================

@bp.route('/login', methods=['GET', 'POST'])
def login():
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
                flash('Your account has been deactivated.', 'danger')
                return redirect(url_for('main.login'))

            if user.two_factor_enabled:
                session['pending_2fa_user_id'] = user.id
                return redirect(url_for('main.verify_2fa'))

            login_user(user, remember=remember)
            user.last_login = datetime.utcnow()
            db.session.commit()
            
            log_activity(f"User {user.username} logged in", "SUCCESS")
            return redirect(url_for('main.index'))

        flash('Invalid username or password', 'danger')

    return render_template('login.html', title='Sign In')

@bp.route('/verify-2fa', methods=['GET', 'POST'])
def verify_2fa():
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
            flash('Logged in successfully!', 'success')
            return redirect(url_for('main.index'))
        else:
            flash('Invalid code.', 'danger')

    return render_template('verify_2fa.html', title='2FA Verify')

@bp.route('/logout')
def logout():
    logout_user()
    flash('You have been logged out.', 'info')
    return redirect(url_for('main.login'))

@bp.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('main.index'))

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        confirm_password = request.form.get('confirm_password', '')

        if password != confirm_password:
            flash('Passwords do not match', 'danger')
            return redirect(url_for('main.register'))

        if User.query.filter_by(username=username).first():
            flash('Username taken', 'danger')
            return redirect(url_for('main.register'))
            
        if User.query.filter_by(email=email).first():
            flash('Email already registered', 'danger')
            return redirect(url_for('main.register'))

        user = User(username=username, email=email)
        user.set_password(password)
        db.session.add(user)
        db.session.commit() # Commit to get ID
        
        # Create settings
        settings = UserSettings(user_id=user.id)
        db.session.add(settings)
        db.session.commit()

        flash('Registration successful! Please login.', 'success')
        return redirect(url_for('main.login'))

    return render_template('register.html', title='Register')

# ==================== DASHBOARD & CAMPAIGN ROUTES ====================

@bp.route('/')
@bp.route('/index')
@login_required
def index():
    # Fetch campaigns
    campaigns = Campaign.query.filter_by(user_id=current_user.id).order_by(Campaign.created_at.desc()).limit(10).all()
    
    # Simple stats
    total_campaigns = Campaign.query.filter_by(user_id=current_user.id).count()
    active_campaigns = Campaign.query.filter_by(user_id=current_user.id, status='Sending').count()
    
    # Weekly stats
    week_ago = datetime.utcnow().date() - timedelta(days=7)
    stats = db.session.query(
        func.coalesce(func.sum(DailyStats.emails_sent), 0).label('sent'),
        func.coalesce(func.sum(DailyStats.unique_opens), 0).label('opens'),
        func.coalesce(func.sum(DailyStats.unique_clicks), 0).label('clicks')
    ).filter(
        DailyStats.user_id == current_user.id,
        DailyStats.date >= week_ago
    ).first()

    stats_dict = {
        'total_campaigns': total_campaigns,
        'active_campaigns': active_campaigns,
        'emails_sent_week': int(stats.sent) if stats else 0,
        'opens_week': int(stats.opens) if stats else 0,
        'clicks_week': int(stats.clicks) if stats else 0
    }
    
    return render_template('dashboard.html', 
                           title='Dashboard',
                           recent_campaigns=campaigns,
                           campaigns=Campaign.query.filter_by(user_id=current_user.id), # For count check
                           stats=stats_dict)

@bp.route('/campaign/new', methods=['GET', 'POST'])
@login_required
def new_campaign():
    smtp_profiles = SMTPServer.query.filter_by(user_id=current_user.id, is_active=True).all()
    
    if request.method == 'POST':
        try:
            name = request.form.get('campaign_name', 'Untitled')
            subject = request.form.get('subject')
            body_html = request.form.get('body_html', '')
            
            campaign = Campaign(
                name=name,
                subject=subject,
                body_html=body_html,
                body_plain=html_to_plain_text(body_html),
                user_id=current_user.id,
                status='Draft',
                smtp_profile_id=request.form.get('smtp_profile_id')
            )
            
            db.session.add(campaign)
            db.session.flush() # get ID
            
            # File Upload
            file = request.files.get('recipients_file')
            if file and file.filename:
                added, errors = parse_csv_file(file, campaign.id)
                campaign.total_recipients = added
                if errors:
                    flash(f"Imported with {len(errors)} errors.", "warning")
            
            db.session.commit()
            flash('Campaign created!', 'success')
            return redirect(url_for('main.view_campaign', campaign_id=campaign.id))
            
        except Exception as e:
            db.session.rollback()
            flash(f"Error: {e}", "danger")
            
    return render_template('create_campaign.html', 
                           title='New Campaign', 
                           smtp_profiles=smtp_profiles,
                           default_throttle_amount=20,
                           default_throttle_delay=60)

@bp.route('/campaign/<int:campaign_id>')
@login_required
def view_campaign(campaign_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    if campaign.user_id != current_user.id:
        return redirect(url_for('main.index'))
        
    page = request.args.get('page', 1, type=int)
    recipients = campaign.recipients.paginate(page=page, per_page=50)
    
    return render_template('campaign.html', 
                           campaign=campaign, 
                           recipients=recipients,
                           analytics=campaign.get_analytics())

@bp.route('/campaign/<int:campaign_id>/control/<action>')
@login_required
def campaign_control(campaign_id, action):
    campaign = Campaign.query.get_or_404(campaign_id)
    if campaign.user_id != current_user.id:
        return redirect(url_for('main.index'))
        
    if action == 'start':
        campaign.status = 'Sending'
        campaign.started_at = datetime.utcnow()
        db.session.commit()
        from app.tasks import send_campaign_task
        send_campaign_task.delay(campaign.id)
        flash('Campaign started!', 'success')
        
    elif action == 'pause':
        campaign.status = 'Paused'
        db.session.commit()
        flash('Campaign paused.', 'info')
        
    elif action == 'stop':
        campaign.status = 'Stopped'
        db.session.commit()
        flash('Campaign stopped.', 'warning')
        
    return redirect(url_for('main.view_campaign', campaign_id=campaign.id))

# ==================== SETTINGS ROUTES ====================

@bp.route('/settings/smtp', methods=['GET', 'POST'])
@login_required
def smtp_profiles():
    if request.method == 'POST':
        profile = SMTPServer(user_id=current_user.id)
        profile.profile_name = request.form.get('name')
        profile.server = request.form.get('server')
        profile.port = int(request.form.get('port', 587))
        profile.username = request.form.get('username')
        profile.set_password(request.form.get('password'))
        profile.sender_email = request.form.get('sender_email')
        profile.use_tls = 'use_tls' in request.form
        
        db.session.add(profile)
        db.session.commit()
        flash('Profile added.', 'success')
        return redirect(url_for('main.smtp_profiles'))
        
    profiles = SMTPServer.query.filter_by(user_id=current_user.id).all()
    return render_template('smtp_profiles.html', title='SMTP Profiles', profiles=profiles)

@bp.route('/settings/suppression', methods=['GET', 'POST'])
@login_required
def suppression_list():
    form = SuppressionForm()
    if form.validate_on_submit():
        s = Suppression(
            email=form.email.data,
            reason=form.reason.data,
            user_id=current_user.id,
            source='Manual'
        )
        db.session.add(s)
        db.session.commit()
        flash('Email suppressed.', 'success')
        return redirect(url_for('main.suppression_list'))
        
    page = request.args.get('page', 1, type=int)
    pagination = Suppression.query.filter_by(user_id=current_user.id).paginate(page=page, per_page=50)
    return render_template('suppression.html', title='Suppression List', form=form, pagination=pagination)

# ==================== ANALYTICS & TOOLS ====================

@bp.route('/analytics')
@login_required
def analytics_dashboard():
    # Simplified placeholder for analytics route
    return render_template('analytics.html', 
                           title='Analytics',
                           summary={'total_sent': 0},
                           daily_data={'chart_labels': [], 'chart_data': []},
                           hourly_data={'chart_labels': [], 'chart_data': []})

@bp.route('/tools/deliverability', methods=['GET', 'POST'])
@login_required
def deliverability_tools():
    form = DeliverabilityForm()
    results = None
    if form.validate_on_submit():
        helper = DeliverabilityHelper()
        if form.check_auth.data:
            results = {'type': 'auth', 'auth': helper.check_domain_authentication(form.domain_ip.data)}
        elif form.check_blacklist.data:
            results = {'type': 'blacklist', 'blacklist': helper.check_blacklist(form.domain_ip.data)}
            
    return render_template('deliverability.html', title='Tools', form=form, results=results)
