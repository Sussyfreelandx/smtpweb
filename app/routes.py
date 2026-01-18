from flask import render_template, flash, redirect, url_for, request, jsonify, Blueprint, current_app
from flask_login import login_user, logout_user, current_user, login_required
from app import db
from app.models import User, Campaign, Recipient, SMTPServer, Suppression
from app.core_logic.smtp_handler import SMTPHandler
from app.core_logic.deliverability import DeliverabilityHelper
import csv
import io
import json
import re

bp = Blueprint('main', __name__)

# --- Helper for Email Validation ---
def is_valid_email(email):
    return re.match(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', email)

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
        flash("Permission denied", "danger")
        return redirect(url_for('main.index'))
        
    page = request.args.get('page', 1, type=int)
    recipients = campaign.recipients.paginate(page=page, per_page=50, error_out=False)
    
    return render_template('campaign.html', title=campaign.name, campaign=campaign, recipients=recipients)

@bp.route('/campaign/new', methods=['GET', 'POST'])
@login_required
def new_campaign():
    # Load available profiles for the dropdown
    profiles = SMTPServer.query.filter_by(user_id=current_user.id).all()
    
    if request.method == 'POST':
        # Create Campaign with advanced settings
        campaign = Campaign(
            name=request.form['campaign_name'],
            subject=request.form['subject'],
            body=request.form['body_html'],
            user_id=current_user.id,
            smtp_profile_id=request.form.get('smtp_profile_id'),
            throttle_amount=int(request.form.get('throttle_amount', 20)),
            throttle_delay=int(request.form.get('throttle_delay', 60)),
            parallel_workers=int(request.form.get('parallel_workers', 1)),
            status='Draft'
        )
        db.session.add(campaign)
        db.session.flush() # Get ID
        
        # Load Recipients
        file = request.files.get('recipients_file')
        pasted_emails = request.form.get('pasted_emails', '').strip()
        
        added_count = 0
        
        # 1. Process File
        if file and file.filename != '':
            try:
                stream = io.StringIO(file.stream.read().decode("UTF-8", errors='ignore'), newline=None)
                csv_reader = csv.DictReader(stream)
                # Normalize headers to lowercase
                csv_reader.fieldnames = [h.lower() for h in csv_reader.fieldnames]
                
                if 'email' not in csv_reader.fieldnames:
                    flash("CSV must contain an 'email' column.", "danger")
                else:
                    for row in csv_reader:
                        email = row.get('email', '').strip()
                        if email and is_valid_email(email):
                            # Store extra data as JSON string for personalization
                            # Remove email from data to save space
                            row_data = {k: v for k, v in row.items() if k != 'email'}
                            
                            # Check suppression
                            if not Suppression.query.filter_by(email=email).first():
                                r = Recipient(email=email, data=json.dumps(row_data), campaign_id=campaign.id)
                                db.session.add(r)
                                added_count += 1
            except Exception as e:
                flash(f'Error processing CSV: {str(e)}', 'danger')

        # 2. Process Pasted Emails (Simple list, no extra data)
        if pasted_emails:
            raw_emails = re.findall(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', pasted_emails)
            for email in set(raw_emails):
                if not Suppression.query.filter_by(email=email).first():
                    r = Recipient(email=email, data='{}', campaign_id=campaign.id)
                    db.session.add(r)
                    added_count += 1

        db.session.commit()
        flash(f'Campaign created with {added_count} recipients!', 'success')
        return redirect(url_for('main.view_campaign', campaign_id=campaign.id))
        
    return render_template('create_campaign.html', title='New Campaign', profiles=profiles)

# --- Action Routes ---

@bp.route('/campaign/<int:campaign_id>/control/<action>')
@login_required
def campaign_control(campaign_id, action):
    campaign = Campaign.query.get_or_404(campaign_id)
    if campaign.author != current_user: return redirect(url_for('main.index'))

    from app.tasks import send_campaign_task

    if action == 'start':
        if campaign.status not in ['Sending', 'Completed']:
            send_campaign_task.delay(campaign.id)
            campaign.status = 'Sending'
            flash('Campaign started.', 'success')
            
    elif action == 'pause':
        campaign.status = 'Paused'
        flash('Campaign paused (workers will stop after current batch).', 'warning')
        
    elif action == 'stop':
        campaign.status = 'Stopped'
        flash('Campaign stopped.', 'danger')
        
    elif action == 'retry':
        # Reset Failed to Queued
        failed_recipients = campaign.recipients.filter_by(status='Failed').all()
        count = 0
        for r in failed_recipients:
            r.status = 'Queued'
            r.status_message = None
            count += 1
        db.session.commit()
        flash(f'{count} failed recipients requeued. Click Start to resume.', 'info')
        return redirect(url_for('main.view_campaign', campaign_id=campaign_id))

    db.session.commit()
    return redirect(url_for('main.view_campaign', campaign_id=campaign_id))

@bp.route('/campaign/<int:campaign_id>/clear_list')
@login_required
def clear_recipient_list(campaign_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    if campaign.author != current_user: return redirect(url_for('main.index'))
    
    # Efficient bulk delete
    Recipient.query.filter_by(campaign_id=campaign.id).delete()
    db.session.commit()
    
    flash('Recipient list cleared.', 'info')
    return redirect(url_for('main.view_campaign', campaign_id=campaign_id))

# --- API / AJAX Routes ---

@bp.route('/api/test_smtp', methods=['POST'])
@login_required
def test_smtp_connection():
    """Tests the SMTP connection for a specific profile ID."""
    data = request.get_json()
    profile_id = data.get('profile_id')
    
    profile = SMTPServer.query.get(profile_id)
    if not profile or profile.user_id != current_user.id:
        return jsonify({'success': False, 'message': 'Invalid Profile'})

    try:
        # Use SMTPHandler to test
        import smtplib
        server = None
        handler = SMTPHandler(profile.to_dict()) # Initializes basic config
        
        # Manual connection test logic matching desktop script
        context = handler._create_secure_ssl_context()
        if handler.use_ssl or handler.smtp_port == 465:
            server = smtplib.SMTP_SSL(handler.smtp_server, handler.smtp_port, context=context, timeout=10)
        else:
            server = smtplib.SMTP(handler.smtp_server, handler.smtp_port, timeout=10)
        
        server.ehlo()
        if not handler.use_ssl and handler.use_tls:
            server.starttls(context=context)
            server.ehlo()
            
        server.login(handler.username, handler.password)
        server.quit()
        
        return jsonify({'success': True, 'message': f'Connected to {profile.server} successfully!'})
        
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

@bp.route('/api/validate_list', methods=['POST'])
@login_required
def validate_email_list():
    """Checks MX records for a list of emails (simulated for web response)."""
    data = request.get_json()
    emails = data.get('emails', [])
    
    helper = DeliverabilityHelper()
    results = {'valid': 0, 'invalid': 0, 'details': []}
    
    for email in emails:
        domain = email.split('@')[1]
        mx_status = helper.check_mx_record(domain)
        status = 'Valid' if mx_status == 'Valid' else 'Invalid'
        
        if status == 'Valid': results['valid'] += 1
        else: results['invalid'] += 1
        
        results['details'].append({'email': email, 'status': status, 'reason': mx_status})
        
    return jsonify(results)

# --- Standard Auth Routes (Login/Register/Logout) ---
# ... (Keep existing login/register/logout routes from your previous file) ...
@bp.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated: return redirect(url_for('main.index'))
    if request.method == 'POST':
        user = User.query.filter_by(username=request.form.get('username')).first()
        if user and user.check_password(request.form.get('password')):
            login_user(user, remember=True)
            return redirect(url_for('main.index'))
        flash('Invalid credentials', 'danger')
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

@bp.route('/smtp_profiles', methods=['GET', 'POST'])
@login_required
def smtp_profiles():
    if request.method == 'POST':
        profile_id = request.form.get('profile_id')
        if profile_id:
            profile = SMTPServer.query.get(profile_id)
            if profile.user_id != current_user.id: return redirect(url_for('main.index'))
        else:
            profile = SMTPServer(user_id=current_user.id)
            
        profile.profile_name = request.form['name']
        profile.server = request.form['server']
        profile.port = int(request.form['port'])
        profile.username = request.form['username']
        if request.form.get('password'):
            profile.set_password(request.form['password'])
        profile.sender_name = request.form['sender_name']
        profile.sender_email = request.form['sender_email']
        profile.use_tls = 'use_tls' in request.form
        profile.use_ssl = 'use_ssl' in request.form
        
        db.session.add(profile)
        db.session.commit()
        flash('Profile saved.', 'success')
        return redirect(url_for('main.smtp_profiles'))
        
    profiles = SMTPServer.query.filter_by(user_id=current_user.id).all()
    return render_template('smtp_profiles.html', title='SMTP Profiles', profiles=profiles)

@bp.route('/smtp_profile/delete/<int:profile_id>', methods=['POST'])
@login_required
def delete_smtp_profile(profile_id):
    profile = SMTPServer.query.get_or_404(profile_id)
    if profile.user_id == current_user.id:
        db.session.delete(profile)
        db.session.commit()
        flash('Profile deleted.', 'info')
    return redirect(url_for('main.smtp_profiles'))

@bp.route('/suppression', methods=['GET', 'POST'])
@login_required
def suppression_list():
    from app.forms import SuppressionForm # Assuming you have a form class or handle manually
    # For simplicity, using manual request.form here based on previous structure
    if request.method == 'POST':
        email = request.form.get('email')
        reason = request.form.get('reason')
        if email:
            s = Suppression(email=email, reason=reason)
            try:
                db.session.add(s)
                db.session.commit()
                flash(f'{email} suppressed.', 'success')
            except:
                db.session.rollback()
                flash('Email already suppressed.', 'warning')
    
    page = request.args.get('page', 1, type=int)
    pagination = Suppression.query.paginate(page=page, per_page=20)
    return render_template('suppression.html', pagination=pagination)

@bp.route('/suppression/delete/<int:suppressed_id>', methods=['POST'])
@login_required
def delete_suppressed_email(suppressed_id):
    s = Suppression.query.get_or_404(suppressed_id)
    db.session.delete(s)
    db.session.commit()
    flash('Removed from suppression list.', 'info')
    return redirect(url_for('main.suppression_list'))
