from flask import render_template, flash, redirect, url_for, request, current_app, jsonify
from flask_login import current_user, login_user, logout_user, login_required
from app import db
from app.main import bp
from app.models import (
    User, Campaign, Recipient, SMTPServer, Suppression,
    GlobalSettings, DailyStats, APIKey
)
from app.forms import (
    LoginForm, RegistrationForm, NewCampaignForm, SMTPServerForm, 
    SuppressionForm, GlobalSettingsForm
)
from app.utils import (
    parse_csv_file, parse_txt_file, allowed_file, validate_email_list, 
    sanitize_filename, export_to_csv, log_activity, get_logs
)
from datetime import datetime, timedelta, date
import os
import json
from sqlalchemy import func

# ==================== AUTHENTICATION ROUTES ====================

@bp.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('main.index'))
    
    form = LoginForm()
    if form.validate_on_submit():
        user = User.query.filter_by(username=form.username.data).first()
        if user is None or not user.check_password(form.password.data):
            flash('Invalid username or password', 'error')
            return redirect(url_for('main.login'))
        
        login_user(user, remember=form.remember_me.data)
        user.last_login = datetime.utcnow()
        db.session.commit()
        
        next_page = request.args.get('next')
        if not next_page or not next_page.startswith('/'):
            next_page = url_for('main.index')
        return redirect(next_page)
    
    return render_template('login.html', title='Sign In', form=form)

@bp.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('main.index'))
    
    form = RegistrationForm()
    if form.validate_on_submit():
        user = User(username=form.username.data, email=form.email.data)
        user.set_password(form.password.data)
        db.session.add(user)
        db.session.commit()
        
        flash('Congratulations, you are now a registered user!', 'success')
        return redirect(url_for('main.login'))
    
    return render_template('register.html', title='Register', form=form)

@bp.route('/logout')
def logout():
    logout_user()
    flash('You have been logged out.', 'info')
    return redirect(url_for('main.login'))

# ==================== DASHBOARD & MAIN ====================

@bp.route('/')
@bp.route('/dashboard')
@login_required
def index():
    # Fetch recent campaigns
    recent_campaigns = Campaign.query.filter_by(user_id=current_user.id).order_by(
        Campaign.created_at.desc()
    ).limit(5).all()
    
    # Fetch campaigns query object for filters in template
    campaigns = Campaign.query.filter_by(user_id=current_user.id)
    
    # Stats for today
    today = date.today()
    daily_stats = DailyStats.query.filter_by(user_id=current_user.id, date=today).first()
    
    today_sent = daily_stats.emails_sent if daily_stats else 0
    today_opened = daily_stats.emails_opened if daily_stats else 0
    
    # Counts
    smtp_count = SMTPServer.query.filter_by(user_id=current_user.id).count()
    
    return render_template('dashboard.html', 
                           title='Dashboard',
                           recent_campaigns=recent_campaigns,
                           campaigns=campaigns,
                           today_sent=today_sent,
                           today_opened=today_opened,
                           smtp_count=smtp_count)

# ==================== CAMPAIGN MANAGEMENT ====================

@bp.route('/campaign/new', methods=['GET', 'POST'])
@login_required
def new_campaign():
    form = NewCampaignForm()
    
    # Populate SMTP profiles
    profiles = SMTPServer.query.filter_by(user_id=current_user.id, is_active=True).all()
    form.smtp_profile_id.choices = [(p.id, f"{p.profile_name} ({p.sender_email or p.username})") for p in profiles]
    
    # Set default settings from global
    if request.method == 'GET':
        settings = GlobalSettings.query.first()
        if settings:
            form.burner_domain.data = settings.burner_domain
            form.lure_path.data = settings.lure_path
            form.throttle_amount.data = settings.default_throttle_amount
            form.throttle_delay.data = settings.default_throttle_delay
    
    if form.validate_on_submit():
        try:
            # Create Campaign
            campaign = Campaign(
                name=form.campaign_name.data,
                subject=form.subject.data,
                body_html=form.body_html.data,
                user_id=current_user.id,
                smtp_profile_id=form.smtp_profile_id.data,
                ab_testing_enabled=form.ab_testing_enabled.data,
                subject_b=form.subject_b.data if form.ab_testing_enabled.data else None,
                body_b=form.body_b.data if form.ab_testing_enabled.data else None,
                ab_split_ratio=form.ab_split_ratio.data,
                burner_domain=form.burner_domain.data,
                lure_path=form.lure_path.data,
                throttle_amount=form.throttle_amount.data,
                throttle_delay=form.throttle_delay.data,
                tracking_enabled=form.tracking_enabled.data,
                smtp_rotation_enabled=form.smtp_rotation_enabled.data,
                warmup_mode=form.warmup_mode.data,
                status='Draft'
            )
            
            # Handle Scheduling
            if form.scheduled_date.data and form.scheduled_time.data:
                dt = datetime.combine(form.scheduled_date.data, form.scheduled_time.data)
                campaign.scheduled_at = dt
                campaign.status = 'Scheduled'
            
            # Handle Attachments
            if form.attachments.data:
                attachment_paths = []
                for file in form.attachments.data:
                    if file.filename:
                        filename = sanitize_filename(file.filename)
                        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
                        safe_filename = f"{timestamp}_{filename}"
                        
                        upload_dir = current_app.config['UPLOAD_FOLDER']
                        if not os.path.exists(upload_dir):
                            os.makedirs(upload_dir)
                            
                        filepath = os.path.join(upload_dir, safe_filename)
                        file.save(filepath)
                        attachment_paths.append(filepath)
                
                campaign.set_attachments(attachment_paths)
            
            db.session.add(campaign)
            db.session.commit()
            
            # Process Recipients File
            if form.recipients_file.data:
                filename = form.recipients_file.data.filename
                added = 0
                errors = []
                
                if filename.lower().endswith('.csv'):
                    added, errors = parse_csv_file(form.recipients_file.data, campaign.id)
                elif filename.lower().endswith('.txt'):
                    added, errors = parse_txt_file(form.recipients_file.data, campaign.id)
                
                campaign.total_recipients = added
                db.session.commit()
                
                if errors:
                    flash(f'Campaign created with {added} recipients. Some warnings: {"; ".join(errors[:3])}...', 'warning')
                else:
                    flash(f'Campaign "{campaign.name}" created successfully with {added} recipients.', 'success')
            
            return redirect(url_for('main.view_campaign', campaign_id=campaign.id))
            
        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f"Campaign creation error: {e}")
            flash(f'Error creating campaign: {str(e)}', 'error')
    
    return render_template('create_campaign.html', 
                           title='New Campaign', 
                           form=form,
                           smtp_profiles=profiles,
                           default_burner=form.burner_domain.data,
                           default_lure=form.lure_path.data,
                           default_throttle_amount=form.throttle_amount.data,
                           default_throttle_delay=form.throttle_delay.data)

@bp.route('/campaign/<int:campaign_id>')
@login_required
def view_campaign(campaign_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    if campaign.user_id != current_user.id:
        flash('Access denied.', 'error')
        return redirect(url_for('main.index'))
    
    page = request.args.get('page', 1, type=int)
    recipients = campaign.recipients.paginate(page=page, per_page=20, error_out=False)
    
    # Calculate A/B stats if enabled
    ab_stats = None
    if campaign.ab_testing_enabled:
        ab_stats = {
            'a_sent': campaign.recipients.filter_by(ab_version='A', status='Sent').count(),
            'b_sent': campaign.recipients.filter_by(ab_version='B', status='Sent').count(),
            'a_opened': campaign.recipients.filter(Recipient.ab_version=='A', Recipient.opened_at.isnot(None)).count(),
            'b_opened': campaign.recipients.filter(Recipient.ab_version=='B', Recipient.opened_at.isnot(None)).count(),
            'a_clicked': campaign.recipients.filter(Recipient.ab_version=='A', Recipient.clicked_at.isnot(None)).count(),
            'b_clicked': campaign.recipients.filter(Recipient.ab_version=='B', Recipient.clicked_at.isnot(None)).count()
        }
        
        # Avoid division by zero
        ab_stats['a_open_rate'] = round(ab_stats['a_opened'] / ab_stats['a_sent'] * 100, 1) if ab_stats['a_sent'] > 0 else 0
        ab_stats['b_open_rate'] = round(ab_stats['b_opened'] / ab_stats['b_sent'] * 100, 1) if ab_stats['b_sent'] > 0 else 0
        ab_stats['a_click_rate'] = round(ab_stats['a_clicked'] / ab_stats['a_sent'] * 100, 1) if ab_stats['a_sent'] > 0 else 0
        ab_stats['b_click_rate'] = round(ab_stats['b_clicked'] / ab_stats['b_sent'] * 100, 1) if ab_stats['b_sent'] > 0 else 0

    return render_template('campaign.html', 
                           title=campaign.name,
                           campaign=campaign,
                           recipients=recipients,
                           analytics=campaign.get_analytics(),
                           ab_stats=ab_stats)

@bp.route('/campaign/<int:campaign_id>/control/<action>')
@login_required
def campaign_control(campaign_id, action):
    campaign = Campaign.query.get_or_404(campaign_id)
    if campaign.user_id != current_user.id:
        return jsonify({'error': 'Access denied'}), 403
    
    if action == 'start':
        if campaign.recipients.filter_by(status='Queued').count() == 0 and campaign.status != 'Paused':
             flash('No queued recipients to send to.', 'warning')
        else:
            campaign.status = 'Sending'
            if not campaign.started_at:
                campaign.started_at = datetime.utcnow()
            db.session.commit()
            
            # Trigger Celery Task
            from app.tasks import send_campaign_task
            send_campaign_task.delay(campaign.id)
            
            flash('Campaign started successfully!', 'success')
            
    elif action == 'pause':
        if campaign.status == 'Sending':
            campaign.status = 'Paused'
            db.session.commit()
            flash('Campaign paused.', 'info')
            
    elif action == 'stop':
        campaign.status = 'Stopped'
        campaign.completed_at = datetime.utcnow()
        db.session.commit()
        flash('Campaign stopped.', 'warning')
        
    elif action == 'retry':
        failed = campaign.recipients.filter_by(status='Failed').all()
        count = 0
        for r in failed:
            r.status = 'Queued'
            r.status_message = None
            count += 1
        db.session.commit()
        flash(f'Re-queued {count} failed recipients.', 'success')
        
    return redirect(url_for('main.view_campaign', campaign_id=campaign.id))

@bp.route('/campaign/<int:campaign_id>/delete', methods=['POST'])
@login_required
def delete_campaign(campaign_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    if campaign.user_id != current_user.id:
        flash('Access denied.', 'error')
        return redirect(url_for('main.index'))
    
    if campaign.status == 'Sending':
        flash('Cannot delete a running campaign. Stop it first.', 'error')
        return redirect(url_for('main.view_campaign', campaign_id=campaign.id))
        
    db.session.delete(campaign)
    db.session.commit()
    flash('Campaign deleted.', 'success')
    return redirect(url_for('main.index'))

@bp.route('/campaign/<int:campaign_id>/export')
@login_required
def export_campaign_report(campaign_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    if campaign.user_id != current_user.id:
        return "Access denied", 403
    
    # Export logic to CSV
    recipients = campaign.recipients.all()
    data = []
    for r in recipients:
        data.append([
            r.email,
            r.status,
            r.sent_at,
            r.opened_at,
            r.clicked_at,
            r.status_message
        ])
    
    csv_content = export_to_csv(data, headers=['Email', 'Status', 'Sent At', 'Opened At', 'Clicked At', 'Message'])
    
    from flask import Response
    return Response(
        csv_content,
        mimetype="text/csv",
        headers={"Content-disposition": f"attachment; filename=campaign_{campaign_id}_report.csv"}
    )

# ==================== ANALYTICS ====================

@bp.route('/analytics')
@login_required
def analytics_dashboard():
    days = request.args.get('days', 30, type=int)
    start_date = datetime.utcnow() - timedelta(days=days)
    
    # Aggregate stats
    total_sent = 0
    total_opens = 0
    total_clicks = 0
    total_failed = 0
    
    campaigns = Campaign.query.filter(
        Campaign.user_id == current_user.id,
        Campaign.created_at >= start_date
    ).all()
    
    for c in campaigns:
        analytics = c.get_analytics()
        total_sent += analytics['sent']
        total_opens += analytics['opened']
        total_clicks += analytics['clicked']
        total_failed += analytics['failed']
        
    summary = {
        'total_sent': total_sent,
        'total_opens': total_opens,
        'total_clicks': total_clicks,
        'total_failed': total_failed,
        'avg_open_rate': round(total_opens / total_sent * 100, 1) if total_sent > 0 else 0,
        'avg_click_rate': round(total_clicks / total_sent * 100, 1) if total_sent > 0 else 0
    }
    
    # Prepare chart data (dummy data for structure)
    daily_data = {
        'chart_labels': [(start_date + timedelta(days=i)).strftime('%Y-%m-%d') for i in range(days)],
        'chart_data': [0] * days # You'd fill this with actual query data
    }
    
    hourly_data = {
        'chart_labels': [f"{i}:00" for i in range(24)],
        'chart_data': [0] * 24
    }
    
    return render_template('analytics.html', 
                           title='Analytics',
                           days=days,
                           summary=summary,
                           daily_data=daily_data,
                           hourly_data=hourly_data)

# ==================== SMTP PROFILES ====================

@bp.route('/smtp-profiles', methods=['GET', 'POST'])
@login_required
def smtp_profiles():
    profiles = SMTPServer.query.filter_by(user_id=current_user.id).order_by(SMTPServer.priority).all()
    
    if request.method == 'POST':
        try:
            profile_id = request.form.get('profile_id')
            if profile_id:
                # Update existing
                profile = SMTPServer.query.get(profile_id)
                if not profile or profile.user_id != current_user.id:
                    flash('Profile not found', 'error')
                    return redirect(url_for('main.smtp_profiles'))
            else:
                # Create new
                profile = SMTPServer(user_id=current_user.id)
                db.session.add(profile)
            
            profile.profile_name = request.form.get('name')
            profile.server = request.form.get('server')
            profile.port = int(request.form.get('port'))
            profile.username = request.form.get('username')
            
            pwd = request.form.get('password')
            if pwd:
                profile.set_password(pwd)
                
            profile.sender_name = request.form.get('sender_name')
            profile.sender_email = request.form.get('sender_email')
            profile.use_tls = 'use_tls' in request.form
            profile.use_ssl = 'use_ssl' in request.form
            profile.is_active = 'is_active' in request.form
            profile.daily_limit = int(request.form.get('daily_limit', 500))
            profile.priority = int(request.form.get('priority', 1))
            
            # IMAP
            profile.imap_server = request.form.get('imap_server')
            profile.imap_port = int(request.form.get('imap_port', 993))
            profile.imap_username = request.form.get('imap_username')
            
            imap_pwd = request.form.get('imap_password')
            if imap_pwd:
                profile.set_imap_password(imap_pwd)
                
            db.session.commit()
            flash('SMTP Profile saved successfully.', 'success')
            
        except Exception as e:
            db.session.rollback()
            flash(f'Error saving profile: {str(e)}', 'error')
            
        return redirect(url_for('main.smtp_profiles'))
        
    return render_template('smtp_profiles.html', title='SMTP Profiles', profiles=profiles)

@bp.route('/smtp-profiles/<int:profile_id>/delete', methods=['POST'])
@login_required
def delete_smtp_profile(profile_id):
    profile = SMTPServer.query.get_or_404(profile_id)
    if profile.user_id != current_user.id:
        flash('Access denied', 'error')
        return redirect(url_for('main.smtp_profiles'))
        
    db.session.delete(profile)
    db.session.commit()
    flash('SMTP Profile deleted.', 'success')
    return redirect(url_for('main.smtp_profiles'))

@bp.route('/smtp-profiles/test', methods=['POST'])
@login_required
def test_smtp_connection():
    data = request.get_json()
    profile_id = data.get('profile_id')
    
    profile = SMTPServer.query.get(profile_id)
    if not profile or profile.user_id != current_user.id:
        return jsonify({'success': False, 'message': 'Profile not found'})
        
    # Use the core logic SMTP handler for testing
    from app.core_logic.smtp_handler import SMTPHandler
    try:
        handler = SMTPHandler(profile.to_dict())
        success, message = handler.test_connection()
        handler.disconnect()
        return jsonify({'success': success, 'message': message})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

# ==================== DELIVERABILITY TOOLS ====================

@bp.route('/tools/deliverability')
@login_required
def deliverability_tools():
    return render_template('deliverability.html', title='Deliverability Tools')

@bp.route('/tools/spam-check', methods=['POST'])
@login_required
def spam_check():
    data = request.get_json()
    subject = data.get('subject', '')
    body = data.get('body', '')
    
    from app.core_logic.deliverability import DeliverabilityHelper
    helper = DeliverabilityHelper()
    result = helper.basic_spam_check(subject, body)
    
    return jsonify({'success': True, 'result': result})

# ==================== SETTINGS ====================

@bp.route('/settings', methods=['GET', 'POST'])
@login_required
def general_settings():
    settings = GlobalSettings.query.first()
    if not settings:
        settings = GlobalSettings()
        db.session.add(settings)
        db.session.commit()
        
    if request.method == 'POST':
        # Simple form handling for settings
        settings.burner_domain = request.form.get('burner_domain')
        settings.lure_path = request.form.get('lure_path')
        settings.default_throttle_amount = int(request.form.get('default_throttle_amount', 20))
        settings.default_throttle_delay = int(request.form.get('default_throttle_delay', 60))
        settings.warmup_schedule = request.form.get('warmup_schedule')
        
        db.session.commit()
        flash('Settings saved.', 'success')
        return redirect(url_for('main.general_settings'))
        
    return render_template('settings_general.html', title='Global Settings', settings=settings)

@bp.route('/api-keys')
@login_required
def api_keys():
    keys = APIKey.query.filter_by(user_id=current_user.id).all()
    return render_template('api_keys.html', title='API Keys', api_keys=keys)

@bp.route('/api-keys/create', methods=['POST'])
@login_required
def create_api_key():
    name = request.form.get('name')
    scopes = request.form.getlist('scopes')
    
    new_key_str = APIKey.generate_key()
    api_key = APIKey(
        name=name,
        user_id=current_user.id,
        is_active=True
    )
    api_key.set_key(new_key_str)
    api_key.set_scopes(scopes)
    
    db.session.add(api_key)
    db.session.commit()
    
    return render_template('api_keys.html', 
                           title='API Keys', 
                           api_keys=APIKey.query.filter_by(user_id=current_user.id).all(),
                           new_api_key=new_key_str)

@bp.route('/api-keys/<int:key_id>/revoke', methods=['POST'])
@login_required
def revoke_api_key(key_id):
    key = APIKey.query.get_or_404(key_id)
    if key.user_id != current_user.id:
        return "Access denied", 403
        
    db.session.delete(key)
    db.session.commit()
    flash('API Key revoked.', 'success')
    return redirect(url_for('main.api_keys'))

# ==================== SUPPRESSION LIST ====================

@bp.route('/suppression', methods=['GET', 'POST'])
@login_required
def suppression_list():
    form = SuppressionForm()
    if form.validate_on_submit():
        if not Suppression.query.filter_by(email=form.email.data).first():
            s = Suppression(
                email=form.email.data,
                reason=form.reason.data,
                source='Manual',
                user_id=current_user.id
            )
            db.session.add(s)
            db.session.commit()
            flash('Email added to suppression list.', 'success')
        else:
            flash('Email already suppressed.', 'warning')
        return redirect(url_for('main.suppression_list'))
        
    page = request.args.get('page', 1, type=int)
    pagination = Suppression.query.order_by(Suppression.created_at.desc()).paginate(page=page, per_page=50)
    
    return render_template('suppression.html', title='Suppression List', form=form, pagination=pagination)

@bp.route('/suppression/import', methods=['POST'])
@login_required
def import_suppression_list():
    if 'file' not in request.files:
        flash('No file uploaded', 'error')
        return redirect(url_for('main.suppression_list'))
        
    file = request.files['file']
    if file.filename == '':
        flash('No file selected', 'error')
        return redirect(url_for('main.suppression_list'))
        
    if file and allowed_file(file.filename, {'csv', 'txt'}):
        try:
            content = file.stream.read().decode("utf-8", errors='ignore')
            lines = content.splitlines()
            count = 0
            for line in lines:
                # Basic csv/txt parsing for emails
                emails = re.findall(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', line)
                for email in emails:
                    if not Suppression.query.filter_by(email=email).first():
                        db.session.add(Suppression(email=email, reason='Import', source='CSV/TXT', user_id=current_user.id))
                        count += 1
            db.session.commit()
            flash(f'Imported {count} emails to suppression list.', 'success')
        except Exception as e:
            flash(f'Error importing: {str(e)}', 'error')
            
    return redirect(url_for('main.suppression_list'))

@bp.route('/suppression/<int:suppressed_id>/delete', methods=['POST'])
@login_required
def delete_suppressed_email(suppressed_id):
    s = Suppression.query.get_or_404(suppressed_id)
    # Check permission (suppression list is usually global or team-based, here assumed generic for simplicity or matching user)
    # If suppression is user-specific:
    if s.user_id and s.user_id != current_user.id:
         return "Access denied", 403
         
    db.session.delete(s)
    db.session.commit()
    flash('Email removed from suppression list.', 'success')
    return redirect(url_for('main.suppression_list'))

@bp.route('/suppression/export')
@login_required
def export_suppression_list():
    suppressed = Suppression.query.all()
    data = [[s.email, s.reason, s.created_at] for s in suppressed]
    csv_content = export_to_csv(data, headers=['Email', 'Reason', 'Date'])
    
    from flask import Response
    return Response(
        csv_content, 
        mimetype="text/csv",
        headers={"Content-disposition": "attachment; filename=suppression_list.csv"}
    )

# ==================== API HELPERS FOR FRONTEND ====================

@bp.route('/api/logs')
@login_required
def api_get_logs():
    return jsonify(get_logs())

@bp.route('/api/campaign/<int:campaign_id>/status')
@login_required
def api_campaign_status(campaign_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    if campaign.user_id != current_user.id:
        return jsonify({'error': 'Denied'}), 403
        
    return jsonify({
        'status': campaign.status,
        'sent': campaign.sent_count,
        'failed': campaign.failed_count,
        'total': campaign.total_recipients
    })

@bp.route('/api/campaign/<int:campaign_id>/recipient/add', methods=['POST'])
@login_required
def add_recipient_manual(campaign_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    if campaign.user_id != current_user.id:
        return jsonify({'success': False, 'message': 'Denied'})
        
    email = request.form.get('email')
    if not email:
        return jsonify({'success': False, 'message': 'Email required'})
        
    if Recipient.query.filter_by(campaign_id=campaign.id, email=email).first():
        return jsonify({'success': False, 'message': 'Email already exists in campaign'})
        
    is_suppressed = Suppression.query.filter_by(email=email).first()
    
    r = Recipient(
        email=email, 
        campaign_id=campaign.id,
        status='Suppressed' if is_suppressed else 'Queued',
        status_message='Suppressed' if is_suppressed else None
    )
    db.session.add(r)
    campaign.total_recipients += 1
    db.session.commit()
    
    return jsonify({'success': True})

@bp.route('/api/campaign/<int:campaign_id>/validate')
@login_required
def validate_list(campaign_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    if campaign.user_id != current_user.id:
        flash('Access denied', 'error')
        return redirect(url_for('main.index'))
        
    # Logic to validate existing recipients (simplified)
    recipients = campaign.recipients.filter_by(status='Queued').all()
    count = 0
    for r in recipients:
        if not is_valid_email(r.email):
            r.status = 'Invalid'
            r.status_message = 'Failed format validation'
            count += 1
    db.session.commit()
    flash(f'Validation complete. Marked {count} invalid emails.', 'success')
    return redirect(url_for('main.view_campaign', campaign_id=campaign.id))

@bp.route('/api/campaign/<int:campaign_id>/clear')
@login_required
def clear_recipient_list(campaign_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    if campaign.user_id != current_user.id:
        flash('Access denied', 'error')
        return redirect(url_for('main.index'))
        
    if campaign.status == 'Sending':
        flash('Cannot clear list while sending.', 'error')
        return redirect(url_for('main.view_campaign', campaign_id=campaign.id))
        
    try:
        # Bulk delete
        Recipient.query.filter_by(campaign_id=campaign.id).delete()
        campaign.total_recipients = 0
        campaign.sent_count = 0
        campaign.failed_count = 0
        db.session.commit()
        flash('All recipients cleared.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Error clearing list: {e}', 'error')
        
    return redirect(url_for('main.view_campaign', campaign_id=campaign.id))

# AI Helper Routes (Placeholders/Wrappers)
@bp.route('/api/ai/rewrite', methods=['POST'])
@login_required
def ai_rewrite():
    data = request.get_json()
    content = data.get('content')
    if not content: return jsonify({'success': False, 'result': 'No content'})
    
    from app.core_logic.ai_handler import AIHandler
    ai = AIHandler()
    success, result = ai.rewrite_content(content)
    return jsonify({'success': success, 'result': result})

@bp.route('/api/ai/subject', methods=['POST'])
@login_required
def ai_subject():
    data = request.get_json()
    content = data.get('content')
    if not content: return jsonify({'success': False, 'result': 'No content'})
    
    from app.core_logic.ai_handler import AIHandler
    ai = AIHandler()
    success, result = ai.generate_subjects(content)
    return jsonify({'success': success, 'result': result})

@bp.route('/api/tools/css-inline', methods=['POST'])
@login_required
def css_inline():
    data = request.get_json()
    content = data.get('content')
    
    try:
        import css_inline
        inliner = css_inline.CSSInliner()
        result = inliner.inline(content)
        return jsonify({'success': True, 'result': result})
    except ImportError:
        return jsonify({'success': False, 'result': 'CSS Inline library not installed'})
    except Exception as e:
        return jsonify({'success': False, 'result': str(e)})

@bp.route('/api/tools/link-check', methods=['POST'])
@login_required
def link_check():
    data = request.get_json()
    content = data.get('content')
    
    from app.core_logic.deliverability import DeliverabilityHelper
    helper = DeliverabilityHelper()
    results = helper.check_link_health(content)
    
    return jsonify({'success': True, 'results': results})

@bp.route('/api/tools/deliverability-ajax', methods=['POST'])
@login_required
def deliverability_tools_ajax():
    data = request.get_json()
    subject = data.get('subject')
    body = data.get('body')
    
    from app.core_logic.deliverability import DeliverabilityHelper
    helper = DeliverabilityHelper()
    success, result = helper.analyze_spam_ai(subject, body)
    
    return jsonify({'success': success, 'result': result})
