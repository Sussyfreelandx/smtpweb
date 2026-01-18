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
from datetime import datetime

# Define the blueprint
bp = Blueprint('main', __name__)

# --- Helper Functions ---
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
        flash("You do not have permission to view this campaign.", "danger")
        return redirect(url_for('main.index'))
    
    page = request.args.get('page', 1, type=int)
    recipients = campaign.recipients.paginate(page=page, per_page=50)
    return render_template('campaign.html', title=campaign.name, campaign=campaign, recipients=recipients)

@bp.route('/campaign/new', methods=['GET', 'POST'])
@login_required
def new_campaign():
    smtp_profiles = SMTPServer.query.filter_by(user_id=current_user.id).all()
    
    if request.method == 'POST':
        # Get SMTP profile
        profile_id = request.form.get('smtp_profile_id')
        if not profile_id:
            # Create a temporary one if not selected
            profile = SMTPServer(
                profile_name=f"Temp-{datetime.now().timestamp()}",
                server=request.form['smtp_server'],
                port=int(request.form['smtp_port']),
                username=request.form['smtp_username'],
                sender_name=request.form['smtp_sender_name'],
                sender_email=request.form['smtp_sender_email'],
                user_id=current_user.id
            )
            profile.set_password(request.form['smtp_password'])
            db.session.add(profile)
            db.session.flush()
            profile_id = profile.id
        
        campaign = Campaign(
            name=request.form['campaign_name'],
            subject=request.form['subject'],
            body=request.form['body_html'],
            smtp_profile_id=profile_id,
            user_id=current_user.id,
            throttle_amount=int(request.form.get('throttle_amount', 0)),
            throttle_delay=int(request.form.get('throttle_delay', 0))
        )
        db.session.add(campaign)
        db.session.flush()
        
        # Handle File Upload
        file = request.files.get('recipients_file')
        if file:
            try:
                stream = io.StringIO(file.stream.read().decode("UTF-8", errors='ignore'), newline=None)
                csv_reader = csv.reader(stream)
                headers = [h.lower().strip() for h in next(csv_reader)]
                
                if 'email' not in headers:
                    flash('CSV must contain an "email" column.', 'danger')
                else:
                    email_idx = headers.index('email')
                    count = 0
                    for row in csv_reader:
                        if not row: continue
                        email = row[email_idx].strip()
                        
                        # Check suppression
                        if Suppression.query.filter_by(email=email).first():
                            continue
                            
                        if is_valid_email(email):
                            # Store extra data for personalization
                            data_dict = {headers[i]: row[i] for i in range(len(row)) if i < len(headers)}
                            
                            recipient = Recipient(
                                email=email, 
                                campaign_id=campaign.id,
                                data=json.dumps(data_dict)
                            )
                            db.session.add(recipient)
                            count += 1
                    flash(f'Imported {count} recipients.', 'success')
            except Exception as e:
                flash(f'Error reading CSV: {e}', 'danger')
        
        db.session.commit()
        return redirect(url_for('main.view_campaign', campaign_id=campaign.id))
        
    return render_template('create_campaign.html', title='New Campaign', smtp_profiles=smtp_profiles)

@bp.route('/campaign/<int:campaign_id>/send')
@login_required
def send_campaign(campaign_id):
    from app.tasks import send_campaign_task
    
    campaign = Campaign.query.get_or_404(campaign_id)
    campaign.status = 'Sending'
    db.session.commit()
    
    send_campaign_task.delay(campaign_id)
    flash('Campaign sending started in background.', 'success')
    return redirect(url_for('main.view_campaign', campaign_id=campaign_id))

@bp.route('/campaign/<int:campaign_id>/stop')
@login_required
def stop_campaign(campaign_id):
    # In a real Redis/Celery setup, revoking tasks is complex.
    # We will set status to 'Paused' which tasks check before sending.
    campaign = Campaign.query.get_or_404(campaign_id)
    campaign.status = 'Paused'
    db.session.commit()
    flash('Campaign stopped. Pending emails will not be sent.', 'warning')
    return redirect(url_for('main.view_campaign', campaign_id=campaign_id))

# --- API Endpoints for Dashboard/Editor Buttons ---

@bp.route('/api/load_template', methods=['POST'])
@login_required
def api_load_template():
    # Simple example template for now
    template = """<!DOCTYPE html>
<html>
<body>
    <p>Hi {{ firstname }},</p>
    <p>This is an email for {{ company }}.</p>
    <p><a href="{{ unsubscribe_link }}">Unsubscribe</a></p>
</body>
</html>"""
    return jsonify({'content': template})

@bp.route('/api/validate_list', methods=['POST'])
@login_required
def api_validate_list():
    data = request.json
    emails = data.get('emails', [])
    valid = []
    invalid = []
    
    helper = DeliverabilityHelper()
    
    for email in emails:
        if is_valid_email(email):
            domain = email.split('@')[1]
            if helper.check_mx_record(domain) == "Valid":
                valid.append(email)
            else:
                invalid.append(email)
        else:
            invalid.append(email)
            
    return jsonify({'valid': valid, 'invalid': invalid})

@bp.route('/api/test_smtp', methods=['POST'])
@login_required
def api_test_smtp():
    data = request.json
    config = {
        'server': data.get('server'),
        'port': data.get('port'),
        'username': data.get('username'),
        'password': data.get('password'),
        'use_tls': True, # Assume true for simplicity or add checkbox
        'sender_email': data.get('username')
    }
    
    handler = SMTPHandler(config)
    success, msg = handler.test_connection()
    return jsonify({'success': success, 'message': msg})

# --- Standard Auth Routes ---

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
        if User.query.filter_by(username=request.form['username']).first():
            flash('Username already exists', 'danger')
        else:
            user = User(username=request.form['username'], email=request.form['email'])
            user.set_password(request.form['password'])
            db.session.add(user)
            db.session.commit()
            flash('Registered!', 'success')
            return redirect(url_for('main.login'))
    return render_template('register.html', title='Register')

# --- Smtp Profiles Management ---
@bp.route('/smtp_profiles', methods=['GET', 'POST'])
@login_required
def smtp_profiles():
    if request.method == 'POST':
        profile_id = request.form.get('profile_id')
        if profile_id:
            profile = SMTPServer.query.get(profile_id)
        else:
            profile = SMTPServer(user_id=current_user.id)
            db.session.add(profile)
        
        profile.profile_name = request.form['name']
        profile.server = request.form['server']
        profile.port = int(request.form['port'])
        profile.username = request.form['username']
        profile.sender_name = request.form['sender_name']
        profile.sender_email = request.form['sender_email']
        profile.use_tls = 'use_tls' in request.form
        profile.use_ssl = 'use_ssl' in request.form
        profile.parallel_workers = int(request.form.get('parallel_workers', 1))
        
        if request.form.get('password'):
            profile.set_password(request.form['password'])
            
        try:
            db.session.commit()
            flash('Profile saved.', 'success')
        except Exception as e:
            db.session.rollback()
            flash('Error saving profile (name must be unique).', 'danger')
            
    profiles = SMTPServer.query.filter_by(user_id=current_user.id).all()
    return render_template('smtp_profiles.html', title='SMTP Profiles', profiles=profiles)

@bp.route('/smtp_profiles/delete/<int:profile_id>', methods=['POST'])
@login_required
def delete_smtp_profile(profile_id):
    profile = SMTPServer.query.get_or_404(profile_id)
    if profile.user_id == current_user.id:
        db.session.delete(profile)
        db.session.commit()
        flash('Profile deleted.', 'success')
    return redirect(url_for('main.smtp_profiles'))

@bp.route('/smtp_profiles/test_connection', methods=['POST'])
@login_required
def test_smtp_connection():
    data = request.get_json()
    profile = SMTPServer.query.get(data.get('profile_id'))
    if not profile or profile.user_id != current_user.id:
        return jsonify({'message': 'Profile not found'}), 404
        
    handler = SMTPHandler(profile.to_dict())
    success, msg = handler.test_connection()
    return jsonify({'message': 'Connection Successful' if success else f'Failed: {msg}'})

# --- Suppression List ---
@bp.route('/suppression', methods=['GET', 'POST'])
@login_required
def suppression_list():
    if request.method == 'POST':
        email = request.form.get('email')
        if email:
            s = Suppression(email=email, reason=request.form.get('reason', 'Manual'))
            try:
                db.session.add(s)
                db.session.commit()
                flash(f'{email} suppressed.', 'success')
            except:
                db.session.rollback()
                flash('Email already suppressed.', 'warning')
                
    page = request.args.get('page', 1, type=int)
    pagination = Suppression.query.order_by(Suppression.timestamp.desc()).paginate(page=page, per_page=20)
    
    # We need a simple form class or manual HTML form handling for the template
    # Passing a dummy form object to match template expectation if using wtforms, 
    # but here I'll assume manual HTML form in template or basic object
    class SimpleForm:
        pass
    form = SimpleForm()
    # Mocking fields for template compatibility if it uses render_field
    # In a real app, use WTForms properly.
    
    return render_template('suppression.html', title='Suppression List', pagination=pagination, form=form)

@bp.route('/suppression/delete/<int:suppressed_id>', methods=['POST'])
@login_required
def delete_suppressed_email(suppressed_id):
    s = Suppression.query.get_or_404(suppressed_id)
    db.session.delete(s)
    db.session.commit()
    flash('Removed from suppression list.', 'success')
    return redirect(url_for('main.suppression_list'))
