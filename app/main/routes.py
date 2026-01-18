from flask import render_template, flash, redirect, url_for, request, jsonify, Blueprint
from flask_login import login_user, logout_user, current_user, login_required
from app import db
from app.models import User, Campaign, Recipient, SMTPServer, Suppression
from app.core_logic.deliverability import DeliverabilityHelper
from flask_wtf import FlaskForm
from wtforms import StringField, SubmitField
from wtforms.validators import DataRequired
import csv
import io
import json

# Define the blueprint
bp = Blueprint('main', __name__)

# --- Simple Forms for these views ---
class DeliverabilityForm(FlaskForm):
    domain_ip = StringField('Domain or IP', validators=[DataRequired()])
    check_auth = SubmitField('Check Authentication')
    check_blacklist = SubmitField('Check Blacklist')

class SuppressionForm(FlaskForm):
    email = StringField('Email Address', validators=[DataRequired()])
    reason = StringField('Reason', default="Manual")
    submit = SubmitField('Add to Suppression List')

# --- Routes ---

@bp.route('/')
@bp.route('/index')
@login_required
def index():
    campaigns = Campaign.query.filter_by(user_id=current_user.id).order_by(Campaign.timestamp.desc())
    return render_template('dashboard.html', title='Dashboard', campaigns=campaigns)

@bp.route('/campaign/<int:campaign_id>')
@login_required
def view_campaign(campaign_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    if campaign.author != current_user:
        flash("You do not have permission to view this campaign.", "danger")
        return redirect(url_for('main.index'))
        
    page = request.args.get('page', 1, type=int)
    recipients = campaign.recipients.order_by(Recipient.id.asc()).paginate(page=page, per_page=20, error_out=False)
    
    return render_template('campaign.html', title=campaign.name, campaign=campaign, recipients=recipients)

@bp.route('/campaign/new', methods=['GET', 'POST'])
@login_required
def new_campaign():
    # Fetch user's SMTP profiles to populate the dropdown
    smtp_profiles = SMTPServer.query.filter_by(user_id=current_user.id).all()
    
    if request.method == 'POST':
        # --- Handle A/B Testing Fields ---
        ab_enabled = 'ab_testing_enabled' in request.form
        
        campaign = Campaign(
            name=request.form['campaign_name'],
            subject=request.form['subject'],
            body=request.form['body_html'],
            ab_testing_enabled=ab_enabled,
            subject_b=request.form.get('subject_b'),
            body_b=request.form.get('body_b'),
            ab_split_ratio=int(request.form.get('ab_split_ratio', 50)),
            burner_domain=request.form.get('burner_domain'),
            lure_path=request.form.get('lure_path'),
            smtp_profile_id=request.form.get('smtp_profile_id'),
            user_id=current_user.id
        )
        db.session.add(campaign)
        db.session.flush()
        
        file = request.files.get('recipients_file')
        if file:
            try:
                stream = io.StringIO(file.stream.read().decode("UTF-8"), newline=None)
                csv_reader = csv.DictReader(stream)
                
                # Normalize headers to lowercase for easier lookup
                csv_reader.fieldnames = [f.lower() for f in csv_reader.fieldnames]
                
                for row in csv_reader:
                    if 'email' in row:
                        # Check suppression list
                        is_suppressed = Suppression.query.filter_by(email=row['email'].lower()).first()
                        
                        recipient = Recipient(
                            email=row['email'], 
                            campaign_id=campaign.id,
                            # Store entire row as JSON for autograb
                            data=json.dumps(row),
                            status='Suppressed' if is_suppressed else 'Queued',
                            status_message='Suppressed by global list' if is_suppressed else None
                        )
                        db.session.add(recipient)
            except Exception as e:
                flash(f'Error reading CSV: {e}', 'danger')
        
        db.session.commit()
        flash('Campaign created!', 'success')
        return redirect(url_for('main.view_campaign', campaign_id=campaign.id))
        
    return render_template('create_campaign.html', title='New Campaign', smtp_profiles=smtp_profiles)

@bp.route('/campaign/<int:campaign_id>/send')
@login_required
def send_campaign(campaign_id):
    from app.tasks import send_campaign_task
    send_campaign_task.delay(campaign_id)
    flash('Campaign sending started in background.', 'success')
    return redirect(url_for('main.view_campaign', campaign_id=campaign_id))

# --- SMTP Profile Management ---
@bp.route('/settings/smtp', methods=['GET', 'POST'])
@login_required
def smtp_profiles():
    if request.method == 'POST':
        # Add or Edit Logic
        profile_id = request.form.get('profile_id')
        if profile_id:
            profile = SMTPServer.query.get(profile_id)
            if profile.user_id != current_user.id:
                return redirect(url_for('main.index'))
        else:
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
        flash('SMTP Profile Saved.', 'success')
        return redirect(url_for('main.smtp_profiles'))

    profiles = SMTPServer.query.filter_by(user_id=current_user.id).all()
    return render_template('smtp_profiles.html', title='SMTP Profiles', profiles=profiles)

@bp.route('/settings/smtp/delete/<int:profile_id>', methods=['POST'])
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

@bp.route('/settings/smtp/test', methods=['POST'])
@login_required
def test_smtp_connection():
    data = request.get_json()
    profile_id = data.get('profile_id')
    profile = SMTPServer.query.get_or_404(profile_id)
    
    if profile.user_id != current_user.id:
        return jsonify({'message': 'Unauthorized'}), 403

    from app.core_logic.smtp_handler import SMTPHandler
    handler = SMTPHandler(profile.to_dict())
    
    # We create a dummy test sync method in handler or just try connecting
    try:
        success, msg = handler.send_email_sync(
            to_email=profile.sender_email, 
            subject="Paris Sender Test", 
            html_content="<p>Connection successful!</p>"
        )
        return jsonify({'message': f'Test Result: {msg}'})
    except Exception as e:
        return jsonify({'message': f'Error: {str(e)}'})

# --- Suppression List Management ---
@bp.route('/settings/suppression', methods=['GET', 'POST'])
@login_required
def suppression_list():
    form = SuppressionForm()
    if form.validate_on_submit():
        if not Suppression.query.filter_by(email=form.email.data.lower()).first():
            s = Suppression(email=form.email.data.lower(), reason=form.reason.data)
            db.session.add(s)
            db.session.commit()
            flash(f'{form.email.data} added to suppression list.', 'success')
        else:
            flash(f'{form.email.data} is already suppressed.', 'warning')
        return redirect(url_for('main.suppression_list'))

    page = request.args.get('page', 1, type=int)
    pagination = Suppression.query.order_by(Suppression.timestamp.desc()).paginate(page=page, per_page=50)
    return render_template('suppression.html', title='Suppression List', form=form, pagination=pagination)

@bp.route('/settings/suppression/delete/<int:suppressed_id>', methods=['POST'])
@login_required
def delete_suppressed_email(suppressed_id):
    item = Suppression.query.get_or_404(suppressed_id)
    db.session.delete(item)
    db.session.commit()
    flash('Email removed from suppression list.', 'success')
    return redirect(url_for('main.suppression_list'))

# --- Deliverability Tools & Analytics ---
@bp.route('/tools/deliverability', methods=['GET', 'POST'])
@login_required
def deliverability_tools():
    form = DeliverabilityForm()
    results = None
    helper = DeliverabilityHelper()

    if form.validate_on_submit():
        target = form.domain_ip.data
        if form.check_auth.data:
            results = {'type': 'auth', 'target': target, 'auth': helper.check_domain_authentication(target)}
        elif form.check_blacklist.data:
            results = {'type': 'blacklist', 'target': target, 'blacklist': helper.check_blacklist(target)}

    return render_template('deliverability.html', title='Deliverability Tools', form=form, results=results)

@bp.route('/tools/ajax_analyze', methods=['POST'])
@login_required
def deliverability_tools_ajax():
    data = request.get_json()
    subject = data.get('subject')
    body = data.get('body')
    provider = data.get('provider', 'openai')
    
    helper = DeliverabilityHelper()
    success, result = helper.analyze_spam_ai(subject, body, provider_type=provider)
    
    return jsonify({'success': success, 'result': result})

# --- Auth Routes (Existing) ---
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
