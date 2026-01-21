import os
import json
from datetime import datetime, timedelta
from flask import (
    render_template, flash, redirect, url_for, request,
    current_app, jsonify, make_response, send_from_directory, g
)
from flask_login import current_user, login_user, logout_user, login_required
from werkzeug.urls import url_parse
from werkzeug.utils import secure_filename
from app import db, login, csrf, limiter
from app.main import bp
from app.models import (
    User, Campaign, Recipient, SMTPServer, Suppression,
    EmailTemplate, GlobalSettings, UserSettings, ActivityLog,
    DailyStats, HourlyStats
)
from app.main.forms import (
    LoginForm, RegistrationForm, EditProfileForm,
    NewCampaignForm, SMTPServerForm, SuppressionForm,
    GlobalSettingsForm
)
from app.utils import (
    log_activity, get_logs, parse_csv_file, export_to_csv,
    is_valid_email, allowed_file, mask_email
)
from app.core_logic.smtp_handler import SMTPHandler
from app.core_logic.deliverability import DeliverabilityHelper
from app.core_logic.ai_handler import AIHandler
from app.tasks import send_campaign_task
from app.main.events import send_notification, broadcast_campaign_progress

# ==========================================
#   BEFORE/AFTER REQUEST HANDLERS
# ==========================================

@bp.before_request
@login_required
def before_request():
    """Update last seen time for authenticated users."""
    current_user.last_login = datetime.utcnow()
    db.session.commit()
    g.csrf_token = csrf._get_token()

# ==========================================
#   CORE APPLICATION ROUTES
# ==========================================

@bp.route('/')
@bp.route('/index')
def index():
    """Main dashboard view."""
    campaigns = Campaign.query.filter_by(user_id=current_user.id).order_by(Campaign.created_at.desc())
    recent_campaigns = campaigns.limit(20).all()
    smtp_count = SMTPServer.query.filter_by(user_id=current_user.id).count()
    
    # Quick stats for today
    today = datetime.utcnow().date()
    stats = DailyStats.query.filter_by(user_id=current_user.id, date=today).first()
    
    return render_template(
        'dashboard.html',
        title='Dashboard',
        campaigns=campaigns,
        recent_campaigns=recent_campaigns,
        smtp_count=smtp_count,
        today_sent=stats.emails_sent if stats else 0,
        today_opened=stats.emails_opened if stats else 0
    )

@bp.route('/message')
def message():
    """Generic message display page."""
    return render_template(
        'message.html',
        message_title=request.args.get('title', 'Notification'),
        message_body=request.args.get('body', 'Something happened.'),
        message_details=request.args.get('details', ''),
        message_type=request.args.get('type', 'info'),
        show_back_button='back' in request.args,
        action_url=request.args.get('action_url'),
        action_text=request.args.get('action_text')
    )

# ==========================================
#   USER AUTHENTICATION ROUTES
# ==========================================

@bp.route('/login', methods=['GET', 'POST'])
@login.unauthorized_handler
def login():
    """User login route."""
    if current_user.is_authenticated:
        return redirect(url_for('main.index'))
    
    form = LoginForm()
    if form.validate_on_submit():
        user = User.query.filter_by(username=form.username.data).first()
        if user is None or not user.check_password(form.password.data):
            flash('Invalid username or password', 'danger')
            return redirect(url_for('main.login'))
        
        login_user(user, remember=form.remember_me.data)
        log_activity(f"User '{user.username}' logged in successfully.", "SUCCESS")
        
        next_page = request.args.get('next')
        if not next_page or url_parse(next_page).netloc != '':
            next_page = url_for('main.index')
        return redirect(next_page)
    
    return render_template('login.html', title='Sign In', form=form)


@bp.route('/logout')
def logout():
    """User logout route."""
    if current_user.is_authenticated:
        log_activity(f"User '{current_user.username}' logged out.", "INFO")
        logout_user()
    return redirect(url_for('main.index'))


@bp.route('/register', methods=['GET', 'POST'])
def register():
    """User registration route."""
    if current_user.is_authenticated:
        return redirect(url_for('main.index'))
        
    form = RegistrationForm()
    if form.validate_on_submit():
        user = User(username=form.username.data, email=form.email.data)
        user.set_password(form.password.data)
        db.session.add(user)
        db.session.commit()
        
        flash('Congratulations, you are now a registered user!', 'success')
        log_activity(f"New user registered: {user.username}", "SUCCESS")
        
        return redirect(url_for('main.login'))
        
    return render_template('register.html', title='Register', form=form)

# ==========================================
#   CAMPAIGN MANAGEMENT ROUTES
# ==========================================

@bp.route('/campaigns/new', methods=['GET', 'POST'])
def new_campaign():
    """Create a new campaign."""
    form = NewCampaignForm()
    form.smtp_profile_id.choices = [
        (s.id, f"{s.profile_name} ({s.sender_email or s.username})")
        for s in SMTPServer.query.filter_by(user_id=current_user.id).all()
    ]
    
    if form.validate_on_submit():
        try:
            # Create Campaign
            campaign = Campaign(
                name=form.campaign_name.data,
                subject=form.subject.data,
                body_html=form.body_html.data,
                smtp_profile_id=form.smtp_profile_id.data,
                user_id=current_user.id,
                status='Draft',
                throttle_amount=form.throttle_amount.data,
                throttle_delay=form.throttle_delay.data,
                tracking_enabled=form.tracking_enabled.data,
                smtp_rotation_enabled=form.smtp_rotation_enabled.data,
                warmup_mode=form.warmup_mode.data,
                ab_testing_enabled=form.ab_testing_enabled.data,
                subject_b=form.subject_b.data if form.ab_testing_enabled.data else None,
                body_b=form.body_b.data if form.ab_testing_enabled.data else None,
                ab_split_ratio=form.ab_split_ratio.data if form.ab_testing_enabled.data else None,
                burner_domain=form.burner_domain.data,
                lure_path=form.lure_path.data
            )
            
            # Handle Scheduling
            if form.scheduled_date.data and form.scheduled_time.data:
                campaign.scheduled_at = datetime.combine(form.scheduled_date.data, form.scheduled_time.data)
                campaign.status = 'Scheduled'
            
            db.session.add(campaign)
            db.session.commit()
            
            # Handle Attachments
            if form.attachments.data and form.attachments.data[0].filename:
                attachment_paths = []
                for file in form.attachments.data:
                    filename = secure_filename(file.filename)
                    path = os.path.join(current_app.config['UPLOAD_FOLDER'], f"c{campaign.id}_{filename}")
                    file.save(path)
                    attachment_paths.append(path)
                campaign.set_attachments(attachment_paths)
            
            # Handle Recipients CSV
            if form.recipients_file.data:
                added, errors = parse_csv_file(form.recipients_file.data, campaign.id)
                campaign.total_recipients = added
                if errors:
                    flash(f"CSV import issues: {'; '.join(errors)}", "warning")
            
            db.session.commit()
            
            log_activity(f"Created new campaign: '{campaign.name}'", "SUCCESS")
            flash(f"Campaign '{campaign.name}' created successfully!", "success")
            
            return redirect(url_for('main.view_campaign', campaign_id=campaign.id))
            
        except Exception as e:
            db.session.rollback()
            log_activity(f"Campaign creation failed: {e}", "ERROR")
            flash(f"Error creating campaign: {e}", "danger")
            
    # GET request logic
    settings = GlobalSettings.query.first()
    if not form.is_submitted() and settings:
        form.default_throttle_amount.data = settings.default_throttle_amount
        form.default_throttle_delay.data = settings.default_throttle_delay
        form.burner_domain.data = settings.burner_domain
        form.lure_path.data = settings.lure_path

    return render_template(
        'create_campaign.html',
        title='New Campaign',
        form=form,
        smtp_profiles=form.smtp_profile_id.choices,
        default_throttle_amount=settings.default_throttle_amount if settings else 20,
        default_throttle_delay=settings.default_throttle_delay if settings else 60,
        default_burner=settings.burner_domain if settings else '',
        default_lure=settings.lure_path if settings else ''
    )


@bp.route('/campaign/<int:campaign_id>')
def view_campaign(campaign_id):
    """View details of a specific campaign."""
    campaign = Campaign.query.get_or_404(campaign_id)
    if campaign.user_id != current_user.id:
        return redirect(url_for('main.index'))
    
    page = request.args.get('page', 1, type=int)
    recipients = campaign.recipients.order_by(Recipient.id.desc()).paginate(
        page=page, per_page=50, error_out=False
    )
    
    analytics = campaign.get_analytics()
    # Ensure 'queued' count is available for the template
    analytics['queued'] = campaign.recipients.filter_by(status='Queued').count()

    ab_stats = None
    if campaign.ab_testing_enabled:
        ab_stats = {
            'a_sent': Recipient.query.filter_by(campaign_id=campaign_id, ab_version='A', status='Sent').count(),
            'b_sent': Recipient.query.filter_by(campaign_id=campaign_id, ab_version='B', status='Sent').count(),
            'a_opened': Recipient.query.filter(Recipient.campaign_id==campaign_id, Recipient.ab_version=='A', Recipient.opened_at.isnot(None)).count(),
            'b_opened': Recipient.query.filter(Recipient.campaign_id==campaign_id, Recipient.ab_version=='B', Recipient.opened_at.isnot(None)).count(),
            'a_clicked': Recipient.query.filter(Recipient.campaign_id==campaign_id, Recipient.ab_version=='A', Recipient.clicked_at.isnot(None)).count(),
            'b_clicked': Recipient.query.filter(Recipient.campaign_id==campaign_id, Recipient.ab_version=='B', Recipient.clicked_at.isnot(None)).count(),
        }
        ab_stats['a_open_rate'] = round((ab_stats['a_opened'] / ab_stats['a_sent'] * 100), 2) if ab_stats['a_sent'] > 0 else 0
        ab_stats['b_open_rate'] = round((ab_stats['b_opened'] / ab_stats['b_sent'] * 100), 2) if ab_stats['b_sent'] > 0 else 0
        ab_stats['a_click_rate'] = round((ab_stats['a_clicked'] / ab_stats['a_sent'] * 100), 2) if ab_stats['a_sent'] > 0 else 0
        ab_stats['b_click_rate'] = round((ab_stats['b_clicked'] / ab_stats['b_sent'] * 100), 2) if ab_stats['b_sent'] > 0 else 0

    return render_template(
        'campaign.html',
        title=f"Campaign: {campaign.name}",
        campaign=campaign,
        recipients=recipients,
        analytics=analytics,
        ab_stats=ab_stats
    )


@bp.route('/campaign/<int:campaign_id>/control/<action>', methods=['GET'])
def campaign_control(campaign_id, action):
    """
    Handle campaign control actions (start, stop, pause, retry).
    REMOVED RECURSION to prevent crashes.
    """
    campaign = Campaign.query.get_or_404(campaign_id)
    if campaign.user_id != current_user.id:
        return redirect(url_for('main.index'))

    original_status = campaign.status
    log_message = f"User '{current_user.username}' performed action '{action}' on campaign '{campaign.name}' (ID: {campaign_id})"
    
    try:
        if action == 'start':
            if original_status in ['Draft', 'Paused', 'Stopped', 'Failed']:
                queued_recipients = campaign.recipients.filter_by(status='Queued').count()
                if queued_recipients == 0:
                    flash(f"Cannot start campaign '{campaign.name}'. No queued recipients found.", 'warning')
                else:
                    campaign.status = 'Sending'
                    campaign.started_at = datetime.utcnow()
                    send_campaign_task.delay(campaign.id)
                    flash(f"Campaign '{campaign.name}' is now sending.", 'success')
                    log_activity(f"Campaign '{campaign.name}' started.", "SUCCESS")
            else:
                flash(f"Campaign is already in '{original_status}' status.", 'info')

        elif action == 'pause':
            if original_status == 'Sending':
                campaign.status = 'Paused'
                flash(f"Campaign '{campaign.name}' has been paused.", 'info')
                log_activity(f"Campaign '{campaign.name}' paused.", "INFO")
            else:
                flash(f"Campaign is not sending, cannot pause.", 'warning')

        elif action == 'stop':
            if original_status in ['Sending', 'Paused']:
                campaign.status = 'Stopped'
                campaign.completed_at = datetime.utcnow()
                flash(f"Campaign '{campaign.name}' has been stopped.", 'warning')
                log_activity(f"Campaign '{campaign.name}' stopped.", "WARNING")
            else:
                flash(f"Campaign is not active, cannot stop.", 'info')

        elif action == 'retry':
            failed_recipients = campaign.recipients.filter_by(status='Failed').update({'status': 'Queued'})
            if failed_recipients > 0:
                campaign.status = 'Queued'
                db.session.commit() # Commit this change before starting
                
                campaign.status = 'Sending' # Now set to sending
                send_campaign_task.delay(campaign.id)
                flash(f"Retrying {failed_recipients} failed recipients.", 'info')
                log_activity(f"Retrying {failed_recipients} recipients for campaign '{campaign.name}'.", "INFO")
            else:
                flash("No failed recipients to retry.", 'info')
        
        else:
            flash(f"Unknown action: {action}", 'danger')
            log_activity(f"Unknown campaign action '{action}' on campaign ID {campaign_id}", "ERROR")

        db.session.commit()
        
        # Broadcast update *after* commit
        if campaign.status != original_status:
            broadcast_campaign_progress(campaign.id, campaign.sent_count, campaign.failed_count, campaign.total_recipients, "status_change")

    except Exception as e:
        db.session.rollback()
        log_activity(f"Error during campaign control '{action}' for campaign {campaign_id}: {e}", "ERROR")
        flash(f"An error occurred: {e}", 'danger')

    return redirect(url_for('main.view_campaign', campaign_id=campaign_id))


@bp.route('/campaign/<int:campaign_id>/delete', methods=['POST'])
def delete_campaign(campaign_id):
    """Delete a campaign and its associated data."""
    campaign = Campaign.query.get_or_404(campaign_id)
    if campaign.user_id != current_user.id:
        return redirect(url_for('main.index'))
    
    if campaign.status == 'Sending':
        flash('Cannot delete a campaign that is currently sending.', 'danger')
        return redirect(url_for('main.view_campaign', campaign_id=campaign_id))
    
    try:
        # Manually delete recipients to ensure cascade works correctly
        Recipient.query.filter_by(campaign_id=campaign.id).delete()
        
        db.session.delete(campaign)
        db.session.commit()
        
        log_activity(f"Deleted campaign: '{campaign.name}'", "WARNING")
        flash(f"Campaign '{campaign.name}' has been deleted.", "success")
    except Exception as e:
        db.session.rollback()
        log_activity(f"Error deleting campaign {campaign_id}: {e}", "ERROR")
        flash(f"Error deleting campaign: {e}", "danger")
        return redirect(url_for('main.view_campaign', campaign_id=campaign_id))
        
    return redirect(url_for('main.index'))

# ==========================================
#   RECIPIENT MANAGEMENT ROUTES
# ==========================================

@bp.route('/campaign/<int:campaign_id>/add_recipient', methods=['POST'])
@csrf.exempt # Exempt for AJAX calls if not using a proper header
def add_recipient_manual(campaign_id):
    """Add a single recipient manually via AJAX."""
    campaign = Campaign.query.get_or_404(campaign_id)
    if campaign.user_id != current_user.id:
        return jsonify({'success': False, 'message': 'Permission denied'}), 403
    
    email = request.form.get('email', '').strip().lower()
    if not is_valid_email(email):
        return jsonify({'success': False, 'message': 'Invalid email address'}), 400
    
    # Check if exists
    if Recipient.query.filter_by(campaign_id=campaign.id, email=email).first():
        return jsonify({'success': False, 'message': 'Recipient already exists in this campaign'}), 409

    # Check suppression
    is_suppressed = Suppression.query.filter_by(email=email).first()
    
    try:
        recipient = Recipient(
            email=email,
            campaign_id=campaign.id,
            status='Suppressed' if is_suppressed else 'Queued',
            status_message='On suppression list' if is_suppressed else None
        )
        db.session.add(recipient)
        campaign.total_recipients = (campaign.total_recipients or 0) + 1
        db.session.commit()
        
        return jsonify({'success': True, 'message': 'Recipient added'})
    except Exception as e:
        db.session.rollback()
        log_activity(f"Error adding recipient: {e}", "ERROR")
        return jsonify({'success': False, 'message': 'Database error'}), 500


@bp.route('/campaign/<int:campaign_id>/clear_list', methods=['GET'])
def clear_recipient_list(campaign_id):
    """Clear all recipients from a campaign."""
    campaign = Campaign.query.get_or_404(campaign_id)
    if campaign.user_id != current_user.id:
        return redirect(url_for('main.index'))
        
    if campaign.status == 'Sending':
        flash('Cannot clear list while campaign is sending.', 'danger')
        return redirect(url_for('main.view_campaign', campaign_id=campaign_id))
    
    try:
        num_deleted = Recipient.query.filter_by(campaign_id=campaign.id).delete()
        campaign.total_recipients = 0
        campaign.sent_count = 0
        campaign.failed_count = 0
        db.session.commit()
        
        flash(f'Cleared {num_deleted} recipients from campaign.', 'success')
        log_activity(f"Cleared {num_deleted} recipients from campaign '{campaign.name}'", "INFO")
    except Exception as e:
        db.session.rollback()
        flash(f'Error clearing list: {e}', 'danger')
        
    return redirect(url_for('main.view_campaign', campaign_id=campaign_id))


@bp.route('/campaign/<int:campaign_id>/validate_list')
def validate_list(campaign_id):
    # Placeholder for list validation feature
    flash('List validation feature coming soon!', 'info')
    return redirect(url_for('main.view_campaign', campaign_id=campaign_id))

# ==========================================
#   SETTINGS ROUTES
# ==========================================

@bp.route('/settings/smtp', methods=['GET', 'POST'])
def smtp_profiles():
    """Manage SMTP server profiles."""
    form = SMTPServerForm()
    
    if form.validate_on_submit():
        try:
            if form.profile_id.data:
                # Update existing
                profile = SMTPServer.query.get(form.profile_id.data)
                if profile.user_id != current_user.id:
                    flash('Invalid profile ID.', 'danger')
                    return redirect(url_for('main.smtp_profiles'))
                
                flash_msg = 'SMTP Profile updated successfully!'
            else:
                # Create new
                profile = SMTPServer(user_id=current_user.id)
                db.session.add(profile)
                flash_msg = 'SMTP Profile added successfully!'
            
            # Populate data
            profile.profile_name = form.name.data
            profile.server = form.server.data
            profile.port = form.port.data
            profile.username = form.username.data
            profile.sender_name = form.sender_name.data
            profile.sender_email = form.sender_email.data
            profile.use_tls = form.use_tls.data
            profile.use_ssl = form.use_ssl.data
            profile.is_active = form.is_active.data
            profile.daily_limit = form.daily_limit.data
            profile.priority = form.priority.data
            
            # IMAP settings
            profile.imap_server = form.imap_server.data
            profile.imap_port = form.imap_port.data
            profile.imap_username = form.imap_username.data

            if form.password.data:
                profile.set_password(form.password.data)
            
            if form.imap_password.data:
                profile.set_imap_password(form.imap_password.data)
            
            db.session.commit()
            
            log_activity(f"Saved SMTP profile: '{profile.profile_name}'", "SUCCESS")
            flash(flash_msg, 'success')
            
            return redirect(url_for('main.smtp_profiles'))
        
        except Exception as e:
            db.session.rollback()
            log_activity(f"Error saving SMTP profile: {e}", "ERROR")
            flash(f'Error saving profile: {e}', 'danger')
    
    profiles = SMTPServer.query.filter_by(user_id=current_user.id).order_by(SMTPServer.priority).all()
    
    return render_template(
        'smtp_profiles.html',
        title='SMTP Profiles',
        profiles=profiles,
        form=form
    )


@bp.route('/settings/smtp/<int:profile_id>/delete', methods=['POST'])
def delete_smtp_profile(profile_id):
    """Delete an SMTP profile."""
    profile = SMTPServer.query.get_or_404(profile_id)
    if profile.user_id != current_user.id:
        return redirect(url_for('main.smtp_profiles'))
    
    try:
        db.session.delete(profile)
        db.session.commit()
        
        log_activity(f"Deleted SMTP profile: '{profile.profile_name}'", "WARNING")
        flash('SMTP profile deleted.', 'success')
    except Exception as e:
        db.session.rollback()
        log_activity(f"Error deleting SMTP profile: {e}", "ERROR")
        flash(f'Error deleting profile: {e}', 'danger')
        
    return redirect(url_for('main.smtp_profiles'))


@bp.route('/settings/suppression', methods=['GET', 'POST'])
def suppression_list():
    """Manage the suppression list."""
    form = SuppressionForm()
    
    if form.validate_on_submit():
        email = form.email.data.strip().lower()
        if not Suppression.query.filter_by(email=email).first():
            suppression = Suppression(
                email=email,
                reason=form.reason.data,
                source='manual',
                user_id=current_user.id
            )
            db.session.add(suppression)
            db.session.commit()
            flash('Email added to suppression list.', 'success')
        else:
            flash('Email is already on the suppression list.', 'info')
        return redirect(url_for('main.suppression_list'))
        
    page = request.args.get('page', 1, type=int)
    pagination = Suppression.query.filter_by(user_id=current_user.id).order_by(
        Suppression.created_at.desc()
    ).paginate(page=page, per_page=20, error_out=False)
    
    return render_template(
        'suppression.html',
        title='Suppression List',
        form=form,
        pagination=pagination
    )


@bp.route('/settings/suppression/<int:suppressed_id>/delete', methods=['POST'])
def delete_suppressed_email(suppressed_id):
    """Remove an email from the suppression list."""
    item = Suppression.query.get_or_404(suppressed_id)
    if item.user_id != current_user.id:
        return redirect(url_for('main.suppression_list'))
    
    db.session.delete(item)
    db.session.commit()
    flash('Email removed from suppression list.', 'success')
    
    return redirect(url_for('main.suppression_list'))


@bp.route('/settings/general', methods=['GET', 'POST'])
def general_settings():
    """Manage global application settings."""
    settings = GlobalSettings.query.first()
    if not settings:
        settings = GlobalSettings()
        db.session.add(settings)
        db.session.commit()
    
    form = GlobalSettingsForm(obj=settings)
    
    if form.validate_on_submit():
        try:
            form.populate_obj(settings)
            
            # Encrypt OpenAI key if provided
            if form.openai_api_key.data:
                # You'd need an encryption method here
                pass
            
            # Handle file upload
            if form.template_pdf.data:
                if form.remove_pdf.data == '1':
                    # Logic to remove existing file if any
                    pass
                filename = secure_filename(form.template_pdf.data.filename)
                filepath = os.path.join(current_app.config['UPLOAD_FOLDER'], 'global', filename)
                os.makedirs(os.path.dirname(filepath), exist_ok=True)
                form.template_pdf.data.save(filepath)
                settings.template_pdf_path = filepath
            
            db.session.commit()
            flash('Global settings updated!', 'success')
            return redirect(url_for('main.general_settings'))
        
        except Exception as e:
            db.session.rollback()
            flash(f'Error saving settings: {e}', 'danger')
            
    # For GET request, ensure form is pre-populated
    form.warmup_schedule.data = settings.warmup_schedule or '[]'
    
    return render_template(
        'settings_general.html',
        title='Global Settings',
        form=form,
        settings=settings,
        smtp_count=SMTPServer.query.count(),
        suppression_count=Suppression.query.count()
    )


# ==========================================
#   ANALYTICS & DELIVERABILITY ROUTES
# ==========================================

@bp.route('/analytics')
def analytics_dashboard():
    """Main analytics dashboard."""
    days = request.args.get('days', 30, type=int)
    start_date = datetime.utcnow() - timedelta(days=days)
    
    # Summary
    campaigns = Campaign.query.filter(
        Campaign.user_id == current_user.id,
        Campaign.started_at >= start_date,
        Campaign.status.in_(['Completed', 'Sending', 'Stopped'])
    ).all()
    
    summary = {
        'total_sent': sum(c.get_analytics().get('sent', 0) for c in campaigns),
        'total_opens': sum(c.get_analytics().get('opened', 0) for c in campaigns),
        'total_clicks': sum(c.get_analytics().get('clicked', 0) for c in campaigns),
        'total_failed': sum(c.failed_count or 0 for c in campaigns),
        'total_bounced': sum(c.get_analytics().get('bounced', 0) for c in campaigns),
        'total_unsubscribed': sum(c.get_analytics().get('unsubscribed', 0) for c in campaigns),
    }
    summary['avg_open_rate'] = round((summary['total_opens'] / summary['total_sent'] * 100), 1) if summary['total_sent'] > 0 else 0
    summary['avg_click_rate'] = round((summary['total_clicks'] / summary['total_sent'] * 100), 1) if summary['total_sent'] > 0 else 0
    
    # Daily Chart Data
    daily_stats = DailyStats.query.filter(
        DailyStats.user_id == current_user.id,
        DailyStats.date >= start_date.date()
    ).order_by(DailyStats.date).all()
    
    daily_data = {
        'chart_labels': [d.date.strftime('%b %d') for d in daily_stats],
        'chart_data': [d.emails_sent for d in daily_stats]
    }
    
    # Hourly Chart Data
    hourly_stats = HourlyStats.query.filter_by(user_id=current_user.id).order_by(HourlyStats.hour_of_day).all()
    hourly_data = {
        'chart_labels': [f"{h.hour_of_day}:00" for h in hourly_stats],
        'chart_data': [h.total_opens for h in hourly_stats]
    }
    
    return render_template(
        'analytics.html',
        title='Analytics Dashboard',
        days=days,
        summary=summary,
        daily_data=daily_data,
        hourly_data=hourly_data
    )


@bp.route('/deliverability', methods=['GET', 'POST'])
def deliverability_tools():
    """Tools for checking deliverability metrics."""
    results = None
    helper = DeliverabilityHelper()
    
    if request.method == 'POST':
        target = request.form.get('domain_ip')
        if not target:
            flash('Please enter a domain or IP address.', 'warning')
            return redirect(url_for('main.deliverability_tools'))
        
        results = {'target': target}
        
        if 'check_auth' in request.form:
            results['type'] = 'auth'
            results['auth'] = helper.check_domain_authentication(target)
        
        if 'check_blacklist' in request.form:
            results['type'] = 'blacklist'
            status, listed_on = helper.check_blacklist(target)
            results['blacklist'] = status
    
    return render_template('deliverability.html', title='Deliverability Tools', results=results)


# ==========================================
#   TRACKING & WEBHOOK ROUTES
# ==========================================

@bp.route('/track/o/<token>')
@login.unauthorized_handler
def track_open(token):
    """Track an email open event."""
    # This route would use a timed serializer to decode the token securely
    # For now, it's a placeholder
    pixel_data = base64.b64decode(b'R0lGODlhAQABAIAAAP///wAAACH5BAEAAAAALAAAAAABAAEAAAICRAEAOw==')
    response = make_response(pixel_data)
    response.headers['Content-Type'] = 'image/gif'
    return response

@bp.route('/track/c/<token>')
@login.unauthorized_handler
def track_click(token):
    """Track a click event and redirect."""
    # Placeholder
    return redirect(request.args.get('url', url_for('main.index')))

@bp.route('/unsubscribe/<token>')
@login.unauthorized_handler
def unsubscribe(token):
    """Handle unsubscribe requests."""
    # Placeholder
    flash("You have been unsubscribed.", "info")
    return redirect(url_for('main.index'))

# ==========================================
#   AJAX & API HELPER ROUTES
# ==========================================

@bp.route('/api/campaign/<int:campaign_id>/status')
def api_campaign_status(campaign_id):
    """AJAX endpoint to get campaign status for real-time updates."""
    campaign = Campaign.query.get_or_404(campaign_id)
    if campaign.user_id != current_user.id:
        return jsonify({'error': 'Permission denied'}), 403
    
    analytics = campaign.get_analytics()
    
    return jsonify({
        'status': campaign.status,
        'sent': analytics.get('sent', 0),
        'failed': analytics.get('failed', 0),
        'total': analytics.get('total', 0)
    })


@bp.route('/api/smtp/test', methods=['POST'])
def test_smtp_connection():
    """AJAX endpoint to test SMTP credentials."""
    data = request.json
    profile_id = data.get('profile_id')
    
    if not profile_id:
        return jsonify({'success': False, 'message': 'Profile ID is required.'}), 400
    
    profile = SMTPServer.query.get(profile_id)
    if not profile or profile.user_id != current_user.id:
        return jsonify({'success': False, 'message': 'Profile not found.'}), 404
    
    config = profile.to_dict()
    if not config.get('password'):
        return jsonify({'success': False, 'message': 'Password not set for this profile.'}), 400
    
    handler = SMTPHandler(config)
    success, message = handler.test_connection()
    
    return jsonify({'success': success, 'message': message})


@bp.route('/api/ai/rewrite', methods=['POST'])
def ai_rewrite():
    """AJAX endpoint for AI content rewrite."""
    content = request.json.get('content')
    if not content:
        return jsonify({'success': False, 'result': 'No content provided.'})
    
    ai = AIHandler()
    success, result = ai.rewrite_content(content)
    
    return jsonify({'success': success, 'result': result})


@bp.route('/api/ai/subject', methods=['POST'])
def ai_subject():
    """AJAX endpoint for AI subject line generation."""
    content = request.json.get('content')
    if not content:
        return jsonify({'success': False, 'result': 'No content provided.'})
        
    ai = AIHandler()
    success, result = ai.generate_subjects(content)
    
    return jsonify({'success': success, 'result': result})


@bp.route('/api/logs', methods=['GET'])
def api_get_logs():
    """AJAX endpoint to get recent system logs."""
    return jsonify(get_logs())
