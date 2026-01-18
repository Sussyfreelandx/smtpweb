from flask import render_template, flash, redirect, url_for, request, jsonify, Blueprint, current_app
from flask_login import login_user, logout_user, current_user, login_required
from app import db
from app.models import User, Campaign, Recipient, SMTPServer, Attachment, Suppression
from app.core_logic.smtp_handler import SMTPHandler
from app.core_logic.deliverability import DeliverabilityHelper
from werkzeug.utils import secure_filename
import csv
import io
import os
import json
import re

bp = Blueprint('main', __name__)

def allowed_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in {'csv', 'txt', 'html', 'htm', 'pdf', 'png', 'jpg', 'jpeg', 'zip', 'doc', 'docx'}

# --- Dashboard & Auth ---

@bp.route('/')
@bp.route('/index')
@login_required
def index():
    campaigns = Campaign.query.filter_by(user_id=current_user.id).order_by(Campaign.id.desc())
    return render_template('dashboard.html', title='Dashboard', campaigns=campaigns)

@bp.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('main.index'))
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
    if current_user.is_authenticated:
        return redirect(url_for('main.index'))
    if request.method == 'POST':
        user = User(username=request.form['username'], email=request.form['email'])
        user.set_password(request.form['password'])
        db.session.add(user)
        db.session.commit()
        flash('Registered!', 'success')
        return redirect(url_for('main.login'))
    return render_template('register.html', title='Register')

# --- Campaign Management ---

@bp.route('/campaign/new', methods=['GET', 'POST'])
@login_required
def new_campaign():
    smtp_profiles = SMTPServer.query.filter_by(user_id=current_user.id).all()
    
    if request.method == 'POST':
        try:
            # 1. Create Campaign
            campaign = Campaign(
                name=request.form['campaign_name'],
                subject=request.form['subject'],
                body=request.form['body_html'],
                smtp_profile_id=request.form['smtp_profile_id'],
                user_id=current_user.id,
                parallel_workers=int(request.form.get('parallel_workers', 1)),
                throttle_amount=int(request.form.get('throttle_amount', 0)),
                throttle_interval=int(request.form.get('throttle_interval', 0))
            )
            db.session.add(campaign)
            db.session.flush() # Get ID

            # 2. Handle Attachments
            uploaded_files = request.files.getlist('attachments')
            upload_dir = os.path.join(current_app.root_path, 'static', 'uploads', str(campaign.id))
            os.makedirs(upload_dir, exist_ok=True)
            
            for file in uploaded_files:
                if file and file.filename and allowed_file(file.filename):
                    filename = secure_filename(file.filename)
                    file_path = os.path.join(upload_dir, filename)
                    file.save(file_path)
                    attachment = Attachment(filename=filename, file_path=file_path, campaign_id=campaign.id)
                    db.session.add(attachment)

            # 3. Handle Recipients CSV
            csv_file = request.files.get('recipients_file')
            if csv_file:
                stream = io.StringIO(csv_file.stream.read().decode("UTF-8", errors='ignore'), newline=None)
                csv_reader = csv.DictReader(stream)
                
                # Check for autograb headers needed in CSV? No, we store raw JSON.
                # Just look for 'email'
                if not csv_reader.fieldnames or 'email' not in [h.lower() for h in csv_reader.fieldnames]:
                     flash("CSV must contain an 'email' header.", "danger")
                     return redirect(url_for('main.new_campaign'))

                email_col = next(h for h in csv_reader.fieldnames if h.lower() == 'email')
                
                count = 0
                for row in csv_reader:
                    email_val = row[email_col].strip()
                    if email_val:
                        # Check Suppression
                        if Suppression.query.filter_by(email=email_val).first():
                            continue
                            
                        # Store extra data for autograb (firstname, etc)
                        data_json = json.dumps(row)
                        recipient = Recipient(email=email_val, campaign_id=campaign.id, data=data_json)
                        db.session.add(recipient)
                        count += 1
                
                flash(f"Loaded {count} recipients.", "info")

            db.session.commit()
            flash('Campaign created successfully!', 'success')
            return redirect(url_for('main.view_campaign', campaign_id=campaign.id))
            
        except Exception as e:
            db.session.rollback()
            flash(f'Error creating campaign: {str(e)}', 'danger')

    return render_template('create_campaign.html', title='New Campaign', smtp_profiles=smtp_profiles)

@bp.route('/campaign/<int:campaign_id>')
@login_required
def view_campaign(campaign_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    if campaign.author != current_user:
        flash("Permission denied.", "danger")
        return redirect(url_for('main.index'))
    
    page = request.args.get('page', 1, type=int)
    recipients = campaign.recipients.paginate(page=page, per_page=50)
    
    return render_template('campaign.html', title=campaign.name, campaign=campaign, recipients=recipients)

# --- Campaign Control Actions (Start/Pause/Stop) ---

@bp.route('/campaign/<int:campaign_id>/action/<action>')
@login_required
def campaign_action(campaign_id, action):
    campaign = Campaign.query.get_or_404(campaign_id)
    if campaign.author != current_user:
        return jsonify({'error': 'Denied'}), 403

    if action == 'start':
        if campaign.status in ['Draft', 'Paused', 'Stopped', 'Completed']:
            from app.tasks import send_campaign_task
            send_campaign_task.delay(campaign.id)
            campaign.status = 'Running'
            flash("Campaign started.", "success")
            
    elif action == 'pause':
        if campaign.status == 'Running':
            campaign.status = 'Paused'
            flash("Campaign paused. Workers will finish current items then pause.", "warning")
            
    elif action == 'resume':
        if campaign.status == 'Paused':
            campaign.status = 'Running'
            flash("Campaign resumed.", "success")
            
    elif action == 'stop':
        if campaign.status in ['Running', 'Paused']:
            campaign.status = 'Stopped'
            flash("Campaign stopped. This cannot be resumed automatically.", "danger")

    db.session.commit()
    return redirect(url_for('main.view_campaign', campaign_id=campaign_id))

# --- Recipient Management Actions ---

@bp.route('/campaign/<int:campaign_id>/recipient/add', methods=['POST'])
@login_required
def add_recipient(campaign_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    email = request.form.get('email')
    
    if email:
        # Basic validation
        if not re.match(r"[^@]+@[^@]+\.[^@]+", email):
             flash("Invalid email format.", "warning")
        elif Suppression.query.filter_by(email=email).first():
             flash("Email is in suppression list.", "warning")
        else:
            rec = Recipient(email=email, campaign_id=campaign.id, status='Queued', data='{}')
            db.session.add(rec)
            db.session.commit()
            flash("Recipient added.", "success")
            
    return redirect(url_for('main.view_campaign', campaign_id=campaign_id))

@bp.route('/campaign/<int:campaign_id>/recipients/clear', methods=['POST'])
@login_required
def clear_recipients(campaign_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    # Only delete if not running
    if campaign.status == 'Running':
        flash("Cannot clear list while campaign is running.", "danger")
    else:
        num = Recipient.query.filter_by(campaign_id=campaign.id).delete()
        db.session.commit()
        flash(f"Cleared {num} recipients.", "info")
    return redirect(url_for('main.view_campaign', campaign_id=campaign_id))

@bp.route('/campaign/<int:campaign_id>/recipients/paste', methods=['POST'])
@login_required
def paste_recipients(campaign_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    text = request.form.get('pasted_text', '')
    
    # Extract emails
    emails = re.findall(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', text)
    count = 0
    for email in set(emails):
        if not Suppression.query.filter_by(email=email).first():
            # Check duplicates in campaign
            if not Recipient.query.filter_by(campaign_id=campaign.id, email=email).first():
                db.session.add(Recipient(email=email, campaign_id=campaign.id, data='{}'))
                count += 1
    db.session.commit()
    flash(f"Added {count} emails from paste.", "success")
    return redirect(url_for('main.view_campaign', campaign_id=campaign_id))

@bp.route('/campaign/<int:campaign_id>/recipients/retry_failed', methods=['POST'])
@login_required
def retry_failed(campaign_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    failed = Recipient.query.filter_by(campaign_id=campaign.id, status='Failed').all()
    count = 0
    for r in failed:
        r.status = 'Queued'
        r.status_message = None
        count += 1
    db.session.commit()
    flash(f"Re-queued {count} failed recipients.", "info")
    return redirect(url_for('main.view_campaign', campaign_id=campaign_id))

@bp.route('/campaign/<int:campaign_id>/recipients/validate', methods=['POST'])
@login_required
def validate_list(campaign_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    recipients = campaign.recipients.filter_by(status='Queued').all()
    
    dh = DeliverabilityHelper()
    valid_count = 0
    invalid_count = 0
    
    for r in recipients:
        domain = r.email.split('@')[1]
        mx_status = dh.check_mx_record(domain)
        if mx_status != "Valid":
            r.status = f"Invalid ({mx_status})"
            invalid_count += 1
        else:
            valid_count += 1
            
    db.session.commit()
    flash(f"Validation complete: {valid_count} Valid, {invalid_count} Invalid/Flagged.", "info")
    return redirect(url_for('main.view_campaign', campaign_id=campaign_id))

@bp.route('/campaign/<int:campaign_id>/remove_selected', methods=['POST'])
@login_required
def remove_selected(campaign_id):
    ids = request.form.getlist('recipient_ids')
    if ids:
        Recipient.query.filter(Recipient.id.in_(ids), Recipient.campaign_id == campaign_id).delete(synchronize_session=False)
        db.session.commit()
        flash(f"Removed {len(ids)} recipients.", "info")
    return redirect(url_for('main.view_campaign', campaign_id=campaign_id))

# --- SMTP Management & Testing ---

@bp.route('/smtp_profiles', methods=['GET', 'POST'])
@login_required
def smtp_profiles():
    if request.method == 'POST':
        profile_id = request.form.get('profile_id')
        
        if profile_id: # Edit
            profile = SMTPServer.query.get(profile_id)
            if profile.user_id != current_user.id: abort(403)
        else: # New
            profile = SMTPServer(user_id=current_user.id)
            
        profile.profile_name = request.form['name']
        profile.server = request.form['server']
        profile.port = int(request.form['port'])
        profile.username = request.form['username']
        profile.sender_name = request.form['sender_name']
        profile.sender_email = request.form['sender_email']
        profile.use_tls = 'use_tls' in request.form
        profile.use_ssl = 'use_ssl' in request.form
        
        if request.form.get('password'):
            profile.set_password(request.form['password'])
            
        db.session.add(profile)
        db.session.commit()
        flash('Profile saved.', 'success')
        return redirect(url_for('main.smtp_profiles'))
        
    profiles = SMTPServer.query.filter_by(user_id=current_user.id).all()
    return render_template('smtp_profiles.html', profiles=profiles)

@bp.route('/smtp/test', methods=['POST'])
@login_required
def test_smtp_connection():
    data = request.get_json()
    profile_id = data.get('profile_id')
    profile = SMTPServer.query.get_or_404(profile_id)
    
    if profile.user_id != current_user.id:
        return jsonify({'message': 'Unauthorized'}), 403
        
    handler = SMTPHandler(profile.to_dict())
    success, msg = handler.test_connection()
    
    return jsonify({'message': msg, 'success': success})

# --- Editor Helpers ---

@bp.route('/template/load', methods=['POST'])
@login_required
def load_template_example():
    # Returns the Jinja2 template content from the desktop app
    template = """<!DOCTYPE html>
<html>
<head>
    <style> .button { background-color: #4CAF50; color: white; padding: 10px 20px; text-decoration: none; } </style>
</head>
<body>
<div style="font-family: sans-serif;">
    <h1>{{ subject_line }}</h1>
    <p>{{ greetings }},</p>
    <p>This is a message from {{ company }}.</p>
    
    <p>Regards,<br>{{ sender_name }}</p>
    <hr>
    <p style="font-size:12px; color:#888;">To unsubscribe, <a href="{{ unsubscribe_link }}">click here</a>.</p>
</div>
</body>
</html>"""
    return jsonify({'content': template})
