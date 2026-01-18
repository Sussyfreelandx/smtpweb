from flask import render_template, flash, redirect, url_for, request, jsonify
from flask_login import login_user, logout_user, current_user, login_required
from app import db
from app.main import bp
from app.models import User, Campaign, Recipient, SmtpProfile, SuppressedEmail
from app.tasks import send_campaign_task, test_smtp_task
import csv
import io
import json

# --- Main Dashboard & Campaign Routes ---

@bp.route('/')
@bp.route('/index')
@login_required
def index():
    """Dashboard page showing all campaigns for the logged-in user."""
    campaigns = Campaign.query.filter_by(user_id=current_user.id).order_by(Campaign.created_at.desc())
    return render_template('dashboard.html', title='Dashboard', campaigns=campaigns)

@bp.route('/campaign/<int:campaign_id>')
@login_required
def view_campaign(campaign_id):
    """Page to view a specific campaign and its recipients."""
    campaign = Campaign.query.get_or_404(campaign_id)
    if campaign.author != current_user:
        flash("You do not have permission to view this campaign.", "danger")
        return redirect(url_for('main.index'))
    recipients = campaign.recipients.order_by(Recipient.id.asc()).all()
    return render_template('campaign.html', title=campaign.name, campaign=campaign, recipients=recipients)

@bp.route('/campaign/new', methods=['GET', 'POST'])
@login_required
def new_campaign():
    """Page to create a new campaign with A/B testing and SMTP profile selection."""
    smtp_profiles = SmtpProfile.query.filter_by(owner=current_user).all()
    if not smtp_profiles:
        flash("You must create at least one SMTP Profile before creating a campaign.", "warning")
        return redirect(url_for('main.smtp_profiles'))

    if request.method == 'POST':
        is_ab_test = 'is_ab_test' in request.form
        
        # Check for suppressed emails in the uploaded list
        file = request.files.get('recipients_file')
        if not file:
            flash('Recipients file is required.', 'danger')
            return redirect(request.url)
            
        suppressed_emails_query = SuppressedEmail.query.with_entities(SuppressedEmail.email).all()
        suppressed_list = {item.email for item in suppressed_emails_query}
        
        recipients_to_add = []
        try:
            stream = io.StringIO(file.stream.read().decode("UTF8"), newline=None)
            csv_reader = csv.DictReader(stream)
            if 'email' not in csv_reader.fieldnames:
                raise ValueError("CSV must contain an 'email' column header.")

            for row in csv_reader:
                email = row.get('email', '').strip().lower()
                if email and email not in suppressed_list:
                    recipients_to_add.append({'email': email, 'data': json.dumps(row)})
        except Exception as e:
            flash(f"Error processing CSV file: {e}", 'danger')
            return redirect(request.url)

        if not recipients_to_add:
            flash('No valid, non-suppressed recipients found in the uploaded file.', 'warning')
            return redirect(request.url)

        # Create Campaign
        campaign = Campaign(
            name=request.form['campaign_name'],
            subject_a=request.form['subject_a'],
            body_html_a=request.form['body_html_a'],
            is_ab_test=is_ab_test,
            subject_b=request.form.get('subject_b') if is_ab_test else None,
            body_html_b=request.form.get('body_html_b') if is_ab_test else None,
            ab_split_ratio=int(request.form.get('ab_split_ratio', 50)),
            smtp_profile_ids=','.join(request.form.getlist('smtp_profiles')),
            author=current_user
        )
        db.session.add(campaign)

        # Add recipients to the campaign
        for rec_data in recipients_to_add:
            recipient = Recipient(email=rec_data['email'], data=rec_data['data'], campaign=campaign)
            db.session.add(recipient)
        
        db.session.commit()
        flash(f'Your campaign has been created with {len(recipients_to_add)} recipients!')
        return redirect(url_for('main.view_campaign', campaign_id=campaign.id))

    return render_template('create_campaign.html', title='New Campaign', smtp_profiles=smtp_profiles)

@bp.route('/campaign/<int:campaign_id>/send')
@login_required
def send_campaign(campaign_id):
    """Triggers the background task to send the campaign."""
    campaign = Campaign.query.get_or_404(campaign_id)
    if campaign.author != current_user:
        flash("You do not have permission to send this campaign.", "danger")
        return redirect(url_for('main.index'))
        
    # This is non-blocking. It starts the background task and returns immediately.
    send_campaign_task.delay(campaign_id)
    flash('Your campaign is being sent in the background! Statuses will update automatically.')
    return redirect(url_for('main.view_campaign', campaign_id=campaign_id))


# --- SMTP Profile Management Routes (NEW) ---

@bp.route('/settings/smtp', methods=['GET', 'POST'])
@login_required
def smtp_profiles():
    """Page to manage SMTP profiles."""
    if request.method == 'POST':
        profile_id = request.form.get('profile_id')
        
        # Check if username is unique for this user
        existing = SmtpProfile.query.filter(SmtpProfile.user_id == current_user.id, SmtpProfile.username == request.form['username']).first()
        if existing and str(existing.id) != profile_id:
            flash('An SMTP profile with this username already exists.', 'danger')
            return redirect(url_for('main.smtp_profiles'))

        if profile_id: # Update existing
            profile = SmtpProfile.query.get_or_404(profile_id)
            if profile.owner != current_user:
                flash('Not authorized.', 'danger')
                return redirect(url_for('main.smtp_profiles'))
            flash_msg = 'SMTP Profile updated successfully!'
        else: # Create new
            profile = SmtpProfile(owner=current_user)
            db.session.add(profile)
            flash_msg = 'SMTP Profile created successfully!'

        profile.name = request.form['name']
        profile.server = request.form['server']
        profile.port = int(request.form['port'])
        profile.username = request.form['username']
        # Only update password if a new one is provided
        if request.form.get('password'):
            profile.password = request.form['password'] # TODO: Encrypt this
        profile.sender_name = request.form['sender_name']
        profile.sender_email = request.form['sender_email']
        profile.use_tls = 'use_tls' in request.form
        profile.use_ssl = 'use_ssl' in request.form
        
        db.session.commit()
        flash(flash_msg, 'success')
        return redirect(url_for('main.smtp_profiles'))

    profiles = SmtpProfile.query.filter_by(owner=current_user).all()
    return render_template('smtp_profiles.html', title="SMTP Profiles", profiles=profiles)

@bp.route('/settings/smtp/delete/<int:profile_id>', methods=['POST'])
@login_required
def delete_smtp_profile(profile_id):
    profile = SmtpProfile.query.get_or_404(profile_id)
    if profile.owner != current_user:
        flash('Not authorized.', 'danger')
    else:
        db.session.delete(profile)
        db.session.commit()
        flash('SMTP Profile deleted.', 'success')
    return redirect(url_for('main.smtp_profiles'))

@bp.route('/settings/smtp/test', methods=['POST'])
@login_required
def test_smtp_connection():
    """AJAX endpoint to test SMTP connection."""
    data = request.json
    task = test_smtp_task.delay(data)
    return jsonify({"task_id": task.id}), 202

@bp.route('/task_status/<task_id>')
@login_required
def task_status(task_id):
    """AJAX endpoint to check the status of a background task."""
    task = test_smtp_task.AsyncResult(task_id)
    if task.state == 'PENDING':
        response = {'state': task.state, 'status': 'Pending...'}
    elif task.state != 'FAILURE':
        response = {'state': task.state, 'status': task.info.get('status', ''), 'result': task.info.get('result', '')}
    else:
        response = {'state': task.state, 'status': str(task.info)}
    return jsonify(response)

# --- Suppression List Management (NEW) ---

@bp.route('/suppression', methods=['GET', 'POST'])
@login_required
def suppression_list():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        reason = request.form.get('reason', 'Manual Add')
        if email:
            existing = SuppressedEmail.query.filter_by(email=email).first()
            if not existing:
                suppressed = SuppressedEmail(email=email, reason=reason)
                db.session.add(suppressed)
                db.session.commit()
                flash(f'{email} added to the suppression list.', 'success')
            else:
                flash(f'{email} is already on the suppression list.', 'info')
        return redirect(url_for('main.suppression_list'))
    
    suppressed_emails = SuppressedEmail.query.order_by(SuppressedEmail.created_at.desc()).all()
    return render_template('suppression.html', title="Suppression List", suppressed_emails=suppressed_emails)

@bp.route('/suppression/delete/<int:suppressed_id>', methods=['POST'])
@login_required
def delete_suppressed_email(suppressed_id):
    email_to_remove = SuppressedEmail.query.get_or_404(suppressed_id)
    db.session.delete(email_to_remove)
    db.session.commit()
    flash(f'{email_to_remove.email} has been removed from the suppression list.', 'success')
    return redirect(url_for('main.suppression_list'))


# --- Authentication Routes ---

@bp.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('main.index'))
    if request.method == 'POST':
        user = User.query.filter_by(username=request.form['username']).first()
        if user is None or not user.check_password(request.form['password']):
            flash('Invalid username or password', 'danger')
            return redirect(url_for('main.login'))
        login_user(user, remember=True)
        return redirect(url_for('main.index'))
    return render_template('login.html', title='Sign In')

@bp.route('/logout')
def logout():
    logout_user()
    return redirect(url_for('main.login'))

@bp.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('main.index'))
    if request.method == 'POST':
        # Check if username or email already exists
        if User.query.filter_by(username=request.form['username']).first():
            flash('Username already taken. Please choose a different one.', 'warning')
            return redirect(url_for('main.register'))
        if User.query.filter_by(email=request.form['email']).first():
            flash('Email address already registered.', 'warning')
            return redirect(url_for('main.register'))
            
        user = User(username=request.form['username'], email=request.form['email'])
        user.set_password(request.form['password'])
        db.session.add(user)
        db.session.commit()
        flash('Congratulations, you are now a registered user!', 'success')
        return redirect(url_for('main.login'))
    return render_template('register.html', title='Register')
