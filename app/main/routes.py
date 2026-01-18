from flask import render_template, flash, redirect, url_for, request, jsonify, Blueprint, current_app
from flask_login import login_user, logout_user, current_user, login_required
from app import db
from app.models import User, Campaign, Recipient, SMTPServer, Suppression
from app.core_logic.smtp_handler import SMTPHandler
from app.core_logic.deliverability import DeliverabilityHelper
from werkzeug.utils import secure_filename
import csv
import io
import json
import re
import time
from datetime import datetime

# Define the blueprint
bp = Blueprint('main', __name__)

# --- Helper Functions ---
def is_valid_email(email):
    # Basic regex validation + check against suppression list
    if not re.match(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', email):
        return False
    # Check suppression list
    if Suppression.query.filter_by(email=email).first():
        return False
    return True

# --- Routes ---

@bp.route('/')
@bp.route('/index')
@login_required
def index():
    campaigns = Campaign.query.filter_by(user_id=current_user.id).order_by(Campaign.id.desc())
    return render_template('dashboard.html', title='Dashboard', campaigns=campaigns)

@bp.route('/campaign/<int:campaign_id>')
@login_required
def view_campaign(campaign_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    if campaign.author != current_user:
        flash("You do not have permission to view this campaign.", "danger")
        return redirect(url_for('main.index'))
    
    page = request.args.get('page', 1, type=int)
    recipients = campaign.recipients.order_by(Recipient.id.asc()).paginate(page=page, per_page=50, error_out=False)
    
    return render_template('campaign.html', title=campaign.name, campaign=campaign, recipients=recipients)

@bp.route('/campaign/new', methods=['GET', 'POST'])
@login_required
def new_campaign():
    smtp_profiles = SMTPServer.query.filter_by(user_id=current_user.id).all()
    
    if request.method == 'POST':
        # Create Campaign
        campaign = Campaign(
            name=request.form['campaign_name'],
            subject=request.form['subject'],
            body=request.form['body_html'],
            smtp_profile_id=request.form.get('smtp_profile_id'),
            user_id=current_user.id,
            # Save throttling settings
            throttle_count=int(request.form.get('throttle_amount', 20)),
            throttle_delay=int(request.form.get('throttle_delay', 1)),
            parallel_workers=int(request.form.get('smtp_max_workers', 1))
        )
        db.session.add(campaign)
        db.session.flush()
        
        # Handle File Upload (CSV)
        file = request.files.get('recipients_file')
        recipient_count = 0
        if file:
            try:
                stream = io.StringIO(file.stream.read().decode("UTF-8"), newline=None)
                csv_reader = csv.DictReader(stream)
                for row in csv_reader:
                    # Normalize keys to lowercase
                    row = {k.lower(): v for k, v in row.items()}
                    if 'email' in row:
                        email = row['email'].strip().lower()
                        if is_valid_email(email):
                            # Store full row data for autograb personalization
                            recipient = Recipient(
                                email=email, 
                                campaign_id=campaign.id,
                                data=json.dumps(row) 
                            )
                            db.session.add(recipient)
                            recipient_count += 1
            except Exception as e:
                flash(f'Error reading CSV: {e}', 'danger')
        
        db.session.commit()
        flash(f'Campaign created with {recipient_count} valid recipients!', 'success')
        return redirect(url_for('main.view_campaign', campaign_id=campaign.id))
        
    return render_template('create_campaign.html', title='New Campaign', smtp_profiles=smtp_profiles)

@bp.route('/campaign/<int:campaign_id>/action', methods=['POST'])
@login_required
def campaign_action(campaign_id):
    """Handles Start, Pause, Stop actions."""
    campaign = Campaign.query.get_or_404(campaign_id)
    action = request.form.get('action')
    
    if action == 'start':
        from app.tasks import send_campaign_task
        campaign.status = 'Running'
        db.session.commit()
        # Trigger Celery Task
        send_campaign_task.delay(campaign_id)
        flash('Campaign started! Emails are queuing...', 'success')
        
    elif action == 'pause':
        campaign.status = 'Paused'
        db.session.commit()
        flash('Campaign paused. Pending emails will hold.', 'warning')
        
    elif action == 'stop':
        campaign.status = 'Stopped'
        db.session.commit()
        # Logic to clear queued tasks would go here (requires Celery control)
        flash('Campaign stopped.', 'danger')
        
    return redirect(url_for('main.view_campaign', campaign_id=campaign_id))

@bp.route('/campaign/<int:campaign_id>/recipients/add', methods=['POST'])
@login_required
def add_recipients_paste(campaign_id):
    """Handles pasting emails directly."""
    campaign = Campaign.query.get_or_404(campaign_id)
    raw_text = request.form.get('pasted_emails', '')
    
    # Extract emails using regex
    emails = re.findall(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b', raw_text)
    count = 0
    for email in set(emails): # Deduplicate
        email = email.lower()
        if is_valid_email(email):
            # Check if already exists in campaign
            exists = Recipient.query.filter_by(campaign_id=campaign.id, email=email).first()
            if not exists:
                rec = Recipient(email=email, campaign_id=campaign.id, data=json.dumps({'email': email}))
                db.session.add(rec)
                count += 1
    
    db.session.commit()
    flash(f'Added {count} new recipients from paste.', 'success')
    return redirect(url_for('main.view_campaign', campaign_id=campaign_id))

@bp.route('/campaign/<int:campaign_id>/recipients/clear', methods=['POST'])
@login_required
def clear_recipients(campaign_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    # Only delete unsent
    deleted = Recipient.query.filter_by(campaign_id=campaign_id, status='Queued').delete()
    db.session.commit()
    flash(f'Cleared {deleted} queued recipients.', 'info')
    return redirect(url_for('main.view_campaign', campaign_id=campaign_id))

@bp.route('/campaign/<int:campaign_id>/recipients/retry', methods=['POST'])
@login_required
def retry_failed(campaign_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    failed_recipients = Recipient.query.filter_by(campaign_id=campaign_id, status='Failed').all()
    count = 0
    for r in failed_recipients:
        r.status = 'Queued'
        r.status_message = None
        count += 1
    db.session.commit()
    
    if count > 0 and campaign.status == 'Running':
        # Re-trigger sending if campaign is active
        from app.tasks import send_campaign_task
        send_campaign_task.delay(campaign_id)
        
    flash(f'Reset {count} failed recipients to Queued status.', 'info')
    return redirect(url_for('main.view_campaign', campaign_id=campaign_id))

@bp.route('/campaign/<int:campaign_id>/validate', methods=['POST'])
@login_required
def validate_list(campaign_id):
    """Runs MX check on queued recipients."""
    campaign = Campaign.query.get_or_404(campaign_id)
    recipients = Recipient.query.filter_by(campaign_id=campaign_id, status='Queued').all()
    
    helper = DeliverabilityHelper()
    valid_count = 0
    invalid_count = 0
    
    for r in recipients:
        domain = r.email.split('@')[1]
        mx_status = helper.check_mx_record(domain)
        if mx_status != "Valid":
            r.status = f'Invalid ({mx_status})'
            invalid_count += 1
        else:
            valid_count += 1
            
    db.session.commit()
    flash(f'Validation complete: {valid_count} Valid, {invalid_count} Invalid/Removed from queue.', 'info')
    return redirect(url_for('main.view_campaign', campaign_id=campaign_id))

@bp.route('/smtp_profiles', methods=['GET', 'POST'])
@login_required
def smtp_profiles():
    if request.method == 'POST':
        profile_id = request.form.get('profile_id')
        if profile_id:
            profile = SMTPServer.query.get(profile_id)
        else:
            profile = SMTPServer(user_id=current_user.id)
            
        profile.profile_name = request.form['name']
        profile.server = request.form['server']
        profile.port = int(request.form['port'])
        profile.username = request.form['username']
        if request.form['password']: # Only update if provided
            profile.set_password(request.form['password'])
        profile.sender_name = request.form['sender_name']
        profile.sender_email = request.form['sender_email']
        profile.use_tls = 'use_tls' in request.form
        profile.use_ssl = 'use_ssl' in request.form
        
        db.session.add(profile)
        db.session.commit()
        flash('SMTP Profile saved.', 'success')
        return redirect(url_for('main.smtp_profiles'))
        
    profiles = SMTPServer.query.filter_by(user_id=current_user.id).all()
    return render_template('smtp_profiles.html', title='SMTP Profiles', profiles=profiles)

@bp.route('/smtp_profiles/delete/<int:profile_id>', methods=['POST'])
@login_required
def delete_smtp_profile(profile_id):
    profile = SMTPServer.query.get_or_404(profile_id)
    if profile.user_id != current_user.id:
        flash("Unauthorized", "danger")
        return redirect(url_for('main.smtp_profiles'))
    db.session.delete(profile)
    db.session.commit()
    flash('Profile deleted.', 'success')
    return redirect(url_for('main.smtp_profiles'))

@bp.route('/test_smtp', methods=['POST'])
@login_required
def test_smtp_connection():
    data = request.get_json()
    profile_id = data.get('profile_id')
    profile = SMTPServer.query.get(profile_id)
    
    if not profile or profile.user_id != current_user.id:
        return jsonify({'message': 'Invalid profile'}), 403
        
    handler = SMTPHandler(profile.to_dict())
    success, msg = handler.test_connection()
    
    if success:
        return jsonify({'message': f'✅ Connection Successful to {profile.server}!'})
    else:
        return jsonify({'message': f'❌ Connection Failed: {msg}'}), 400

@bp.route('/suppression', methods=['GET', 'POST'])
@login_required
def suppression_list():
    form_data = request.form
    if request.method == 'POST' and form_data.get('email'):
        email = form_data.get('email').lower().strip()
        if not Suppression.query.filter_by(email=email).first():
            s = Suppression(email=email, reason=form_data.get('reason', 'Manual Add'))
            db.session.add(s)
            db.session.commit()
            flash('Email added to suppression list.', 'success')
            
    page = request.args.get('page', 1, type=int)
    pagination = Suppression.query.order_by(Suppression.timestamp.desc()).paginate(page=page, per_page=20)
    return render_template('suppression.html', title='Suppression List', pagination=pagination)

@bp.route('/suppression/delete/<int:suppressed_id>', methods=['POST'])
@login_required
def delete_suppressed_email(suppressed_id):
    item = Suppression.query.get_or_404(suppressed_id)
    db.session.delete(item)
    db.session.commit()
    flash('Removed from suppression list.', 'info')
    return redirect(url_for('main.suppression_list'))

# --- Auth Routes (unchanged logic) ---
@bp.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated: return redirect(url_for('main.index'))
    if request.method == 'POST':
        user = User.query.filter_by(username=request.form.get('username')).first()
        if user and user.check_password(request.form.get('password')):
            login_user(user, remember=True)
            return redirect(url_for('main.index'))
        flash('Invalid username or password', 'danger')
    return render_template('login.html', title='Sign In')

@bp.route('/logout')
def logout():
    logout_user()
    return redirect(url_for('main.index'))

@bp.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated: return redirect(url_for('main.index'))
    if request.method == 'POST':
        user = User(username=request.form['username'], email=request.form['email'])
        user.set_password(request.form['password'])
        db.session.add(user)
        db.session.commit()
        flash('Registered!', 'success')
        return redirect(url_for('main.login'))
    return render_template('register.html', title='Register')
