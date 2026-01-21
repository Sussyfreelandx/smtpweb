from flask import (
    render_template, flash, redirect, url_for, request, current_app, jsonify,
    send_from_directory
)
from flask_login import current_user, login_user, logout_user, login_required
from werkzeug.urls import url_parse
from werkzeug.utils import secure_filename
from datetime import datetime
import os
import json
import base64

from app import db, csrf
from app.main import bp
from app.main.forms import LoginForm, RegistrationForm, SMTPProfileForm, SuppressionForm
from app.models import (
    User, Campaign, Recipient, SMTPServer, Suppression, GlobalSettings
)
from app.utils import log_activity, parse_csv_file, is_valid_email
from app.core_logic.deliverability import DeliverabilityHelper
from app.core_logic.ai_handler import AIHandler

# CRITICAL: Import the tasks module to trigger Celery tasks
from app import tasks


@bp.route('/')
@bp.route('/index')
@login_required
def index():
    """Main dashboard view."""
    # Query campaigns and order by creation date
    all_campaigns = Campaign.query.filter_by(user_id=current_user.id).order_by(Campaign.created_at.desc())
    recent_campaigns = all_campaigns.limit(20).all()
    smtp_count = SMTPServer.query.filter_by(user_id=current_user.id).count()

    # Pass the full query object and the recent list to the template
    return render_template(
        'dashboard.html',
        title='Dashboard',
        campaigns=all_campaigns,
        recent_campaigns=recent_campaigns,
        smtp_count=smtp_count
    )


# ==============================================================================
# AUTHENTICATION ROUTES
# ==============================================================================

@bp.route('/login', methods=['GET', 'POST'])
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
        user.last_login = datetime.utcnow()
        db.session.commit()
        
        next_page = request.args.get('next')
        if not next_page or url_parse(next_page).netloc != '':
            next_page = url_for('main.index')
        
        log_activity(f"User {user.username} logged in successfully", "SUCCESS")
        return redirect(next_page)

    return render_template('login.html', title='Sign In', form=form)


@bp.route('/logout')
def logout():
    """User logout route."""
    log_activity(f"User {current_user.username} logged out", "INFO")
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


# ==============================================================================
# CAMPAIGN MANAGEMENT ROUTES
# ==============================================================================

@bp.route('/campaign/new', methods=['GET', 'POST'])
@login_required
def new_campaign():
    """Create a new campaign."""
    if request.method == 'POST':
        try:
            # --- 1. Basic Campaign Info ---
            name = request.form.get('campaign_name')
            subject = request.form.get('subject')
            body_html = request.form.get('body_html')
            
            if not name or not subject or not body_html:
                flash('Campaign Name, Subject, and Body are required.', 'danger')
                return redirect(request.url)

            campaign = Campaign(
                name=name,
                subject=subject,
                body_html=body_html,
                user_id=current_user.id,
                status='Draft'
            )

            # --- 2. Recipients CSV ---
            recipients_file = request.files.get('recipients_file')
            if not recipients_file or recipients_file.filename == '':
                flash('Recipients CSV file is required.', 'danger')
                return redirect(request.url)

            # --- 3. SMTP & Sending Config ---
            campaign.smtp_profile_id = request.form.get('smtp_profile_id')
            if not campaign.smtp_profile_id:
                flash('SMTP Profile is required.', 'danger')
                return redirect(request.url)
            
            campaign.throttle_amount = int(request.form.get('throttle_amount', 20))
            campaign.throttle_delay = int(request.form.get('throttle_delay', 60))
            campaign.tracking_enabled = 'tracking_enabled' in request.form
            campaign.smtp_rotation_enabled = 'smtp_rotation_enabled' in request.form
            campaign.warmup_mode = 'warmup_mode' in request.form

            # --- 4. A/B Testing ---
            if 'ab_testing_enabled' in request.form:
                campaign.ab_testing_enabled = True
                campaign.subject_b = request.form.get('subject_b')
                campaign.body_b = request.form.get('body_b')
                campaign.ab_split_ratio = int(request.form.get('ab_split_ratio', 50))
            
            # --- 5. Scheduling ---
            scheduled_date = request.form.get('scheduled_date')
            scheduled_time = request.form.get('scheduled_time')
            if scheduled_date and scheduled_time:
                campaign.scheduled_at = datetime.strptime(f"{scheduled_date} {scheduled_time}", '%Y-%m-%d %H:%M')
                campaign.status = 'Scheduled'
            
            # --- 6. Save Campaign First (to get an ID) ---
            db.session.add(campaign)
            db.session.commit()
            
            # --- 7. Process CSV and Add Recipients ---
            added_count, errors = parse_csv_file(recipients_file, campaign.id)
            if added_count == 0 and errors:
                flash(f"Failed to add recipients: {errors[0]}", "danger")
                db.session.delete(campaign)
                db.session.commit()
                return redirect(request.url)
            
            campaign.total_recipients = added_count
            db.session.commit()
            
            flash(f"Campaign '{campaign.name}' created with {added_count} recipients.", "success")
            log_activity(f"Campaign '{campaign.name}' created", "SUCCESS")
            return redirect(url_for('main.view_campaign', campaign_id=campaign.id))

        except Exception as e:
            db.session.rollback()
            flash(f"Error creating campaign: {e}", "danger")
            log_activity(f"Campaign creation failed: {e}", "ERROR")

    # For GET request
    smtp_profiles = SMTPServer.query.filter_by(user_id=current_user.id).all()
    settings = GlobalSettings.query.first()
    return render_template(
        'create_campaign.html', 
        title='New Campaign', 
        smtp_profiles=smtp_profiles,
        default_throttle_amount=settings.default_throttle_amount if settings else 20,
        default_throttle_delay=settings.default_throttle_delay if settings else 60,
        default_burner=settings.burner_domain if settings else '',
        default_lure=settings.lure_path if settings else ''
    )


@bp.route('/campaign/<int:campaign_id>')
@login_required
def view_campaign(campaign_id):
    """View campaign details and stats."""
    campaign = Campaign.query.get_or_404(campaign_id)
    if campaign.user_id != current_user.id:
        return redirect(url_for('main.index'))

    page = request.args.get('page', 1, type=int)
    recipients = campaign.recipients.order_by(Recipient.id).paginate(page=page, per_page=50)
    
    # Calculate stats
    analytics = campaign.get_analytics()
    analytics['queued'] = campaign.recipients.filter_by(status='Queued').count()

    ab_stats = None
    if campaign.ab_testing_enabled:
        ab_stats = {
            'a_sent': campaign.recipients.filter_by(ab_version='A').count(),
            'b_sent': campaign.recipients.filter_by(ab_version='B').count(),
            'a_opened': campaign.recipients.filter_by(ab_version='A').filter(Recipient.opened_at.isnot(None)).count(),
            'b_opened': campaign.recipients.filter_by(ab_version='B').filter(Recipient.opened_at.isnot(None)).count(),
            'a_clicked': campaign.recipients.filter_by(ab_version='A').filter(Recipient.clicked_at.isnot(None)).count(),
            'b_clicked': campaign.recipients.filter_by(ab_version='B').filter(Recipient.clicked_at.isnot(None)).count(),
        }
        ab_stats['a_open_rate'] = round(ab_stats['a_opened'] / ab_stats['a_sent'] * 100, 2) if ab_stats['a_sent'] > 0 else 0
        ab_stats['b_open_rate'] = round(ab_stats['b_opened'] / ab_stats['b_sent'] * 100, 2) if ab_stats['b_sent'] > 0 else 0
        ab_stats['a_click_rate'] = round(ab_stats['a_clicked'] / ab_stats['a_sent'] * 100, 2) if ab_stats['a_sent'] > 0 else 0
        ab_stats['b_click_rate'] = round(ab_stats['b_clicked'] / ab_stats['b_sent'] * 100, 2) if ab_stats['b_sent'] > 0 else 0

    return render_template(
        'campaign.html', 
        campaign=campaign, 
        recipients=recipients, 
        analytics=analytics, 
        ab_stats=ab_stats
    )


@bp.route('/campaign/<int:campaign_id>/control/<action>')
@login_required
def campaign_control(campaign_id, action):
    """Handle campaign actions: start, pause, stop, retry."""
    campaign = Campaign.query.get_or_404(campaign_id)
    if campaign.user_id != current_user.id:
        return redirect(url_for('main.index'))

    try:
        if action == 'start':
            if campaign.status in ['Draft', 'Paused', 'Stopped', 'Failed']:
                if campaign.recipients.filter_by(status='Queued').count() == 0:
                    flash('No queued recipients to send to.', 'warning')
                    return redirect(url_for('main.view_campaign', campaign_id=campaign_id))
                
                campaign.status = 'Sending'
                campaign.started_at = datetime.utcnow()
                db.session.commit()
                # Trigger Celery task
                tasks.send_campaign_task.delay(campaign.id)
                flash('Campaign sending started!', 'success')
                log_activity(f"Campaign '{campaign.name}' started", "SUCCESS")
            else:
                flash('Campaign cannot be started.', 'warning')

        elif action == 'pause':
            if campaign.status == 'Sending':
                campaign.status = 'Paused'
                db.session.commit()
                flash('Campaign paused.', 'info')
                log_activity(f"Campaign '{campaign.name}' paused", "INFO")
            else:
                flash('Campaign is not sending.', 'warning')

        elif action == 'stop':
            if campaign.status in ['Sending', 'Paused']:
                campaign.status = 'Stopped'
                campaign.completed_at = datetime.utcnow()
                db.session.commit()
                flash('Campaign stopped.', 'danger')
                log_activity(f"Campaign '{campaign.name}' stopped", "WARNING")
            else:
                flash('Campaign is not active.', 'warning')
        
        elif action == 'retry':
            failed_recipients = Recipient.query.filter_by(campaign_id=campaign.id, status='Failed').all()
            if not failed_recipients:
                flash('No failed recipients to retry.', 'info')
            else:
                for r in failed_recipients:
                    r.status = 'Queued'
                    r.attempts = 0 # Reset attempts
                db.session.commit()
                flash(f'Re-queued {len(failed_recipients)} failed recipients.', 'success')
                log_activity(f"Retrying {len(failed_recipients)} recipients for campaign '{campaign.name}'", "INFO")
                # If campaign is not already sending, start it
                if campaign.status != 'Sending':
                    campaign.status = 'Sending'
                    db.session.commit()
                    tasks.send_campaign_task.delay(campaign.id)

    except Exception as e:
        db.session.rollback()
        flash(f'Campaign control error ({action}): {e}', 'danger')
        log_activity(f"Campaign control error ({action}) on '{campaign.name}': {e}", "ERROR")

    return redirect(url_for('main.view_campaign', campaign_id=campaign_id))


@bp.route('/campaign/<int:campaign_id>/delete', methods=['POST'])
@login_required
def delete_campaign(campaign_id):
    """Delete a campaign and its recipients."""
    campaign = Campaign.query.get_or_404(campaign_id)
    if campaign.user_id != current_user.id:
        return redirect(url_for('main.index'))

    if campaign.status == 'Sending':
        flash("Cannot delete a campaign that is currently sending.", "danger")
        return redirect(url_for('main.view_campaign', campaign_id=campaign.id))

    try:
        # Cascade delete will handle recipients
        db.session.delete(campaign)
        db.session.commit()
        flash(f"Campaign '{campaign.name}' has been deleted.", "success")
        log_activity(f"Campaign '{campaign.name}' deleted", "WARNING")
    except Exception as e:
        db.session.rollback()
        flash(f"Error deleting campaign: {e}", "danger")
        log_activity(f"Failed to delete campaign '{campaign.name}': {e}", "ERROR")

    return redirect(url_for('main.index'))


@bp.route('/campaign/<int:campaign_id>/export', methods=['GET'])
@login_required
def export_campaign_report(campaign_id):
    """Export campaign recipient data to CSV."""
    # This would ideally be a background task for large campaigns
    # For now, synchronous generation
    campaign = Campaign.query.get_or_404(campaign_id)
    if campaign.user_id != current_user.id:
        return redirect(url_for('main.index'))
    
    # Placeholder for CSV generation logic
    # from app.utils import export_to_csv
    # data = [...]
    # return Response(export_to_csv(data), mimetype='text/csv', ...)
    flash("Export functionality is not yet implemented.", "info")
    return redirect(url_for('main.view_campaign', campaign_id=campaign_id))


# ==============================================================================
# RECIPIENT MANAGEMENT ROUTES
# ==============================================================================

@bp.route('/campaign/<int:campaign_id>/recipients/add', methods=['POST'])
@login_required
@csrf.exempt # Using custom token in JS for fetch
def add_recipient_manual(campaign_id):
    """Add a single recipient manually via AJAX."""
    campaign = Campaign.query.get_or_404(campaign_id)
    if campaign.user_id != current_user.id:
        return jsonify({'success': False, 'message': 'Permission denied'}), 403

    email = request.form.get('email', '').strip().lower()
    if not is_valid_email(email):
        return jsonify({'success': False, 'message': 'Invalid email address.'})

    if Recipient.query.filter_by(campaign_id=campaign_id, email=email).first():
        return jsonify({'success': False, 'message': 'Recipient already exists in this campaign.'})

    is_suppressed = Suppression.query.filter_by(email=email).first()
    
    recipient = Recipient(
        email=email,
        campaign_id=campaign_id,
        data=json.dumps({'email': email}),
        status='Suppressed' if is_suppressed else 'Queued'
    )
    db.session.add(recipient)
    campaign.total_recipients += 1
    db.session.commit()
    
    return jsonify({'success': True, 'message': 'Recipient added.'})


@bp.route('/campaign/<int:campaign_id>/recipients/clear', methods=['GET'])
@login_required
def clear_recipient_list(campaign_id):
    """Clear all recipients from a campaign."""
    campaign = Campaign.query.get_or_404(campaign_id)
    if campaign.user_id != current_user.id:
        return redirect(url_for('main.index'))

    if campaign.status == 'Sending':
        flash('Cannot clear recipients while campaign is sending.', 'danger')
        return redirect(url_for('main.view_campaign', campaign_id=campaign_id))
    
    num_deleted = campaign.recipients.delete()
    campaign.total_recipients = 0
    db.session.commit()
    
    flash(f'Cleared {num_deleted} recipients from the list.', 'success')
    return redirect(url_for('main.view_campaign', campaign_id=campaign_id))


@bp.route('/campaign/<int:campaign_id>/recipients/validate', methods=['GET'])
@login_required
def validate_list(campaign_id):
    """Validate recipient list for a campaign."""
    # Placeholder for a background validation task
    flash("List validation is not yet implemented.", "info")
    return redirect(url_for('main.view_campaign', campaign_id=campaign_id))


# ==============================================================================
# SMTP & SETTINGS ROUTES
# ==============================================================================

@bp.route('/settings/smtp', methods=['GET', 'POST'])
@login_required
def smtp_profiles():
    """Manage SMTP profiles."""
    form = SMTPProfileForm()
    if form.validate_on_submit():
        try:
            profile_id = request.form.get('profile_id')
            if profile_id: # Editing existing
                profile = SMTPServer.query.get(profile_id)
                if profile.user_id != current_user.id:
                    flash('Permission denied.', 'danger')
                    return redirect(url_for('main.smtp_profiles'))
                flash('Profile updated successfully!', 'success')
                log_activity(f"SMTP Profile '{form.name.data}' updated", "SUCCESS")
            else: # Creating new
                profile = SMTPServer(user_id=current_user.id)
                db.session.add(profile)
                flash('Profile added successfully!', 'success')
                log_activity(f"SMTP Profile '{form.name.data}' created", "SUCCESS")

            # Populate profile from form
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
            profile.imap_server = form.imap_server.data
            profile.imap_port = form.imap_port.data
            profile.imap_username = form.imap_username.data

            if form.password.data:
                profile.set_password(form.password.data)
            if form.imap_password.data:
                profile.set_imap_password(form.imap_password.data)
            
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            flash(f'Error saving profile: {e}', 'danger')
            log_activity(f"Error saving SMTP profile: {e}", "ERROR")

        return redirect(url_for('main.smtp_profiles'))
    
    profiles = SMTPServer.query.filter_by(user_id=current_user.id).order_by(SMTPServer.priority).all()
    return render_template('smtp_profiles.html', title="SMTP Profiles", profiles=profiles, form=form)


@bp.route('/settings/smtp/delete/<int:profile_id>', methods=['POST'])
@login_required
def delete_smtp_profile(profile_id):
    """Delete an SMTP profile."""
    profile = SMTPServer.query.get_or_404(profile_id)
    if profile.user_id != current_user.id:
        flash('Permission denied.', 'danger')
    else:
        try:
            db.session.delete(profile)
            db.session.commit()
            flash('SMTP Profile deleted.', 'success')
            log_activity(f"SMTP Profile '{profile.profile_name}' deleted", "WARNING")
        except Exception as e:
            db.session.rollback()
            flash(f'Error deleting profile: {e}. It might be in use.', 'danger')

    return redirect(url_for('main.smtp_profiles'))


@bp.route('/settings/smtp/test', methods=['POST'])
@login_required
@csrf.exempt
def test_smtp_connection():
    """Test SMTP connection via AJAX."""
    profile_id = request.json.get('profile_id')
    if not profile_id:
        return jsonify({'success': False, 'message': 'Profile ID required.'})

    profile = SMTPServer.query.get(profile_id)
    if not profile or profile.user_id != current_user.id:
        return jsonify({'success': False, 'message': 'Profile not found.'})

    # Use the test connection task
    # This avoids blocking the web server process
    task = tasks.test_smtp_connection_task.delay(profile.id)
    # The frontend will have to poll for the result, or use websockets.
    # For now, let's just return a message that the test has started.
    # A more advanced implementation would return a task ID.
    
    # Quick synchronous test for simplicity (can block, not ideal for production)
    from app.core_logic.smtp_handler import SMTPHandler
    handler = SMTPHandler(profile.to_dict())
    success, message = handler.test_connection()
    
    return jsonify({'success': success, 'message': message})


@bp.route('/settings/general', methods=['GET', 'POST'])
@login_required
def general_settings():
    """Manage global application settings."""
    settings = GlobalSettings.query.first()
    if not settings:
        settings = GlobalSettings()
        db.session.add(settings)
        db.session.commit()

    if request.method == 'POST':
        try:
            settings.burner_domain = request.form.get('burner_domain')
            settings.lure_path = request.form.get('lure_path')
            # ... and so on for all other settings
            # AI settings
            if request.form.get('openai_api_key'):
                # In a real app, encrypt this key
                settings.openai_api_key_encrypted = request.form.get('openai_api_key')
            settings.local_ai_url = request.form.get('local_ai_url')
            
            # Throttling
            settings.default_throttle_amount = int(request.form.get('default_throttle_amount', 20))
            settings.default_throttle_delay = int(request.form.get('default_throttle_delay', 60))
            
            # Warmup
            settings.warmup_schedule = request.form.get('warmup_schedule')

            db.session.commit()
            flash('Global settings updated.', 'success')
        except Exception as e:
            db.session.rollback()
            flash(f'Error updating settings: {e}', 'danger')

        return redirect(url_for('main.general_settings'))

    return render_template(
        'settings_general.html', 
        title="Global Settings", 
        settings=settings,
        smtp_count=SMTPServer.query.filter_by(user_id=current_user.id).count(),
        suppression_count=Suppression.query.count() # Global for now
    )


# ==============================================================================
# SUPPRESSION LIST ROUTES
# ==============================================================================

@bp.route('/suppression', methods=['GET', 'POST'])
@login_required
def suppression_list():
    """Manage suppression list."""
    form = SuppressionForm()
    if form.validate_on_submit():
        email = form.email.data.lower()
        if not Suppression.query.filter_by(email=email).first():
            supp = Suppression(email=email, reason=form.reason.data, source='manual', user_id=current_user.id)
            db.session.add(supp)
            db.session.commit()
            flash(f'Email {email} added to suppression list.', 'success')
        else:
            flash('Email already on suppression list.', 'info')
        return redirect(url_for('main.suppression_list'))

    page = request.args.get('page', 1, type=int)
    pagination = Suppression.query.order_by(Suppression.created_at.desc()).paginate(page=page, per_page=50)
    
    return render_template('suppression.html', title="Suppression List", pagination=pagination, form=form)


@bp.route('/suppression/delete/<int:suppressed_id>', methods=['POST'])
@login_required
def delete_suppressed_email(suppressed_id):
    """Remove email from suppression list."""
    supp = Suppression.query.get_or_404(suppressed_id)
    # Add ownership check if suppression is user-specific
    db.session.delete(supp)
    db.session.commit()
    flash(f"Email {supp.email} removed from suppression list.", "success")
    return redirect(url_for('main.suppression_list'))


@bp.route('/suppression/import', methods=['POST'])
@login_required
def import_suppression_list():
    """Import a CSV of emails to suppress."""
    if 'file' not in request.files:
        flash('No file part', 'danger')
        return redirect(url_for('main.suppression_list'))
    
    file = request.files['file']
    if file.filename == '':
        flash('No selected file', 'danger')
        return redirect(url_for('main.suppression_list'))

    # Placeholder for CSV import logic
    flash("Import functionality not yet implemented.", "info")
    return redirect(url_for('main.suppression_list'))


@bp.route('/suppression/export')
@login_required
def export_suppression_list():
    """Export suppression list to CSV."""
    flash("Export functionality not yet implemented.", "info")
    return redirect(url_for('main.suppression_list'))


# ==============================================================================
# TRACKING & WEBHOOK ROUTES (Publicly Accessible)
# ==============================================================================

@bp.route('/track/open/<token>')
def track_open(token):
    """Track email opens."""
    # This needs a robust token validation system (e.g., itsdangerous)
    # For now, a simplified placeholder
    try:
        # Simplified: decode token to get recipient ID
        # In real app: s = Serializer(secret); data = s.loads(token)
        recipient_id = int(base64.urlsafe_b64decode(token).decode())
        recipient = Recipient.query.get(recipient_id)
        if recipient:
            recipient.opened_at = datetime.utcnow()
            recipient.open_count = (recipient.open_count or 0) + 1
            if recipient.status == 'Sent':
                recipient.status = 'Opened'
            db.session.commit()
    except Exception:
        pass # Fail silently
    
    # Return 1x1 transparent pixel
    pixel_data = base64.b64decode(b'R0lGODlhAQABAIAAAP///wAAACH5BAEAAAAALAAAAAABAAEAAAICRAEAOw==')
    response = make_response(pixel_data)
    response.headers['Content-Type'] = 'image/gif'
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    return response


@bp.route('/track/click/<token>')
def track_click(token):
    """Track link clicks."""
    # Simplified token validation
    try:
        data = json.loads(base64.urlsafe_b64decode(token).decode())
        recipient_id = data.get('rid')
        redirect_url = data.get('url', url_for('main.index'))
        
        recipient = Recipient.query.get(recipient_id)
        if recipient:
            recipient.clicked_at = datetime.utcnow()
            recipient.click_count = (recipient.click_count or 0) + 1
            if recipient.status in ['Sent', 'Opened']:
                recipient.status = 'Clicked'
            db.session.commit()
        
        return redirect(redirect_url)
    except Exception:
        return redirect(url_for('main.index'))


@bp.route('/unsubscribe/<token>')
def unsubscribe(token):
    """Handle user unsubscribe requests."""
    try:
        # Simplified token validation
        recipient_id = int(base64.urlsafe_b64decode(token).decode())
        recipient = Recipient.query.get(recipient_id)
        if recipient:
            recipient.status = 'Unsubscribed'
            recipient.unsubscribed_at = datetime.utcnow()
            
            if not Suppression.query.filter_by(email=recipient.email).first():
                supp = Suppression(email=recipient.email, reason='Unsubscribed', source='click')
                db.session.add(supp)
            
            db.session.commit()
            
            return render_template('message.html', 
                                   message_title="Unsubscribed",
                                   message_body=f"You have been successfully unsubscribed from future emails.",
                                   is_unsubscribe_page=True)
    except Exception:
        pass
    
    return render_template('message.html', 
                           message_title="Error", 
                           message_body="Invalid unsubscribe link.",
                           message_type="error")


# ==============================================================================
# ANALYTICS & TOOLS
# ==============================================================================

@bp.route('/analytics')
@login_required
def analytics_dashboard():
    """Show overall analytics dashboard."""
    # Placeholder for analytics logic
    summary = {
        'total_sent': 1000,
        'total_opens': 200,
        'total_clicks': 50,
        'total_failed': 10,
        'avg_open_rate': 20.0,
        'avg_click_rate': 5.0,
    }
    daily_data = {
        'chart_labels': ['Day 1', 'Day 2', 'Day 3'],
        'chart_data': [100, 150, 120]
    }
    hourly_data = {
        'chart_labels': ['9am', '10am', '11am'],
        'chart_data': [50, 80, 60]
    }
    return render_template(
        'analytics.html', 
        title="Analytics", 
        summary=summary, 
        daily_data=daily_data, 
        hourly_data=hourly_data, 
        days=30
    )


@bp.route('/tools/deliverability', methods=['GET', 'POST'])
@login_required
def deliverability_tools():
    """Tools for checking deliverability (DNS, blacklists, etc.)."""
    results = None
    if request.method == 'POST':
        target = request.form.get('domain_ip')
        helper = DeliverabilityHelper()
        if 'check_auth' in request.form:
            results = {
                'type': 'auth',
                'target': target,
                'auth': helper.check_domain_authentication(target)
            }
        elif 'check_blacklist' in request.form:
            status, listed_on = helper.check_blacklist(target)
            results = {
                'type': 'blacklist',
                'target': target,
                'blacklist': status
            }

    return render_template('deliverability.html', title="Deliverability Tools", results=results)


# ==============================================================================
# AJAX / API HELPER ROUTES
# ==============================================================================

@bp.route('/api/campaign/<int:campaign_id>/status')
@login_required
def api_campaign_status(campaign_id):
    """AJAX endpoint to get campaign status for dashboard refresh."""
    campaign = Campaign.query.get_or_404(campaign_id)
    if campaign.user_id != current_user.id:
        return jsonify({'error': 'Not Found'}), 404
        
    stats = campaign.get_analytics()
    return jsonify({
        'id': campaign.id,
        'status': campaign.status,
        'sent': stats.get('sent', 0),
        'failed': stats.get('failed', 0),
        'total': stats.get('total', 0)
    })


@bp.route('/api/ai/rewrite', methods=['POST'])
@login_required
@csrf.exempt
def ai_rewrite():
    """AJAX endpoint for AI content rewriting."""
    content = request.json.get('content')
    if not content:
        return jsonify({'success': False, 'result': 'No content provided.'})
    
    ai = AIHandler()
    success, result = ai.rewrite_content(content)
    return jsonify({'success': success, 'result': result})


@bp.route('/api/ai/subject', methods=['POST'])
@login_required
@csrf.exempt
def ai_subject():
    """AJAX endpoint for AI subject line generation."""
    content = request.json.get('content')
    if not content:
        return jsonify({'success': False, 'result': 'No content provided.'})
    
    ai = AIHandler()
    success, result = ai.generate_subjects(content)
    return jsonify({'success': success, 'result': result})


@bp.route('/api/css-inline', methods=['POST'])
@login_required
@csrf.exempt
def css_inline():
    """AJAX endpoint for inlining CSS."""
    content = request.json.get('content')
    if not content:
        return jsonify({'success': False, 'result': 'No content provided.'})

    try:
        from app.core_logic.personalization import PersonalizationEngine
        p_engine = PersonalizationEngine(None, None)
        inlined_html = p_engine._inline_css(content)
        return jsonify({'success': True, 'result': inlined_html})
    except Exception as e:
        return jsonify({'success': False, 'result': str(e)})


@bp.route('/api/spam-check', methods=['POST'])
@login_required
@csrf.exempt
def spam_check():
    """AJAX endpoint for basic spam check."""
    subject = request.json.get('subject', '')
    body = request.json.get('body', '')
    
    helper = DeliverabilityHelper()
    result = helper.basic_spam_check(subject, body)
    
    return jsonify({'success': True, 'result': result})


@bp.route('/api/link-check', methods=['POST'])
@login_required
@csrf.exempt
def link_check():
    """AJAX endpoint for checking links in HTML."""
    content = request.json.get('content', '')
    if not content:
        return jsonify({'success': False, 'error': 'No content provided'})
        
    helper = DeliverabilityHelper()
    results = helper.check_link_health(content)
    
    return jsonify({'success': True, 'results': results})
