import os
import io
import csv
import json
from werkzeug.utils import secure_filename
from flask import render_template, flash, redirect, url_for, request, jsonify, Blueprint, current_app
from flask_login import login_user, logout_user, current_user, login_required
from flask_wtf import FlaskForm
from wtforms import StringField, SubmitField
from wtforms.validators import DataRequired
from app import db
from app.models import User, Campaign, Recipient, SMTPServer, Suppression, ActivityLog
from app.core_logic.deliverability import DeliverabilityHelper

# Define the blueprint
bp = Blueprint('main', __name__)

# --- Helper Forms ---
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

@bp.route('/campaign/<int:campaign_id>/logs_json')
@login_required
def get_campaign_logs(campaign_id):
    """API Endpoint for Live Activity Log."""
    campaign = Campaign.query.get_or_404(campaign_id)
    if campaign.author != current_user: return jsonify([])

    # Fetch last 50 logs, ordered by newest first
    logs = campaign.logs.order_by(ActivityLog.timestamp.desc()).limit(50).all()
    # Reverse to show oldest to newest in the UI
    return jsonify([log.to_dict() for log in reversed(logs)])

@bp.route('/campaign/new', methods=['GET', 'POST'])
@login_required
def new_campaign():
    smtp_profiles = SMTPServer.query.filter_by(user_id=current_user.id).all()
    
    if request.method == 'POST':
        ab_enabled = 'ab_testing_enabled' in request.form
        rotation_enabled = 'smtp_rotation_enabled' in request.form
        
        try:
            workers = int(request.form.get('parallel_workers', 10))
            throttle_amt = int(request.form.get('throttle_amount', 20))
            throttle_del = int(request.form.get('throttle_delay', 1))
        except ValueError:
            workers, throttle_amt, throttle_del = 10, 20, 1

        # Handle PDF Upload
        pdf_path = None
        pdf_file = request.files.get('template_pdf_file')
        if pdf_file and pdf_file.filename:
            filename = secure_filename(pdf_file.filename)
            upload_folder = os.path.join(current_app.root_path, 'static', 'uploads')
            os.makedirs(upload_folder, exist_ok=True)
            pdf_path = os.path.join('static', 'uploads', filename)
            pdf_file.save(os.path.join(current_app.root_path, pdf_path))

        campaign = Campaign(
            name=request.form['campaign_name'],
            subject=request.form['subject'],
            body=request.form['body_html'],
            ab_testing_enabled=ab_enabled,
            subject_b=request.form.get('subject_b'),
            body_b=request.form.get('body_b'),
            ab_split_ratio=int(request.form.get('ab_split_ratio', 50)),
            
            # Secure Redirector Config
            burner_domain=request.form.get('burner_domain'),
            lure_path=request.form.get('lure_path'),
            template_pdf=pdf_path,
            
            smtp_profile_id=request.form.get('smtp_profile_id'),
            parallel_workers=workers,
            smtp_rotation_enabled=rotation_enabled,
            throttle_amount=throttle_amt,
            throttle_delay=throttle_del,
            throttle_unit=request.form.get('throttle_unit', 'Minutes'),
            user_id=current_user.id
        )
        db.session.add(campaign)
        db.session.flush()
        
        # Recipients CSV
        file = request.files.get('recipients_file')
        if file:
            try:
                stream = io.StringIO(file.stream.read().decode("UTF-8"), newline=None)
                csv_reader = csv.DictReader(stream)
                csv_reader.fieldnames = [f.lower() for f in csv_reader.fieldnames]
                
                count = 0
                for row in csv_reader:
                    if 'email' in row:
                        is_suppressed = Suppression.query.filter_by(email=row['email'].lower()).first()
                        recipient = Recipient(
                            email=row['email'], 
                            campaign_id=campaign.id,
                            data=json.dumps(row),
                            status='Suppressed' if is_suppressed else 'Queued',
                            status_message='Suppressed by global list' if is_suppressed else None
                        )
                        db.session.add(recipient)
                        count += 1
                flash(f'Imported {count} recipients.', 'info')
                
                # Initial Log Entry
                init_log = ActivityLog(campaign_id=campaign.id, message=f"Campaign created. Loaded {count} recipients.", log_type="info")
                db.session.add(init_log)
                
            except Exception as e:
                flash(f'Error reading CSV: {e}', 'danger')
        
        db.session.commit()
        flash('Campaign created successfully!', 'success')
        return redirect(url_for('main.view_campaign', campaign_id=campaign.id))
        
    return render_template('create_campaign.html', title='New Campaign', smtp_profiles=smtp_profiles)

@bp.route('/campaign/<int:campaign_id>/control/<action>')
@login_required
def campaign_control(campaign_id, action):
    campaign = Campaign.query.get_or_404(campaign_id)
    if campaign.author != current_user:
        return redirect(url_for('main.index'))

    log_entry = None

    if action == 'start':
        if campaign.status in ['Draft', 'Paused', 'Stopped', 'Completed', 'Failed']:
            from app.tasks import send_campaign_task
            send_campaign_task.delay(campaign.id)
            flash('Campaign started.', 'success')
            log_entry = ActivityLog(campaign_id=campaign.id, message="Command: START executed.", log_type="success")
    
    elif action == 'pause':
        if campaign.status == 'Sending':
            campaign.status = 'Paused'
            db.session.commit()
            flash('Campaign paused.', 'warning')
            log_entry = ActivityLog(campaign_id=campaign.id, message="Command: PAUSE executed. Waiting for workers...", log_type="warning")
            
    elif action == 'stop':
        campaign.status = 'Stopped'
        db.session.commit()
        flash('Campaign stopping...', 'danger')
        log_entry = ActivityLog(campaign_id=campaign.id, message="Command: STOP executed.", log_type="error")

    elif action == 'retry':
        failed = campaign.recipients.filter_by(status='Failed').all()
        for r in failed:
            r.status = 'Queued'
            r.status_message = None
        db.session.commit()
        flash(f'Queued {len(failed)} failed recipients for retry.', 'info')
        log_entry = ActivityLog(campaign_id=campaign.id, message=f"Command: RETRY executed. {len(failed)} requeued.", log_type="info")

    if log_entry:
        db.session.add(log_entry)
        db.session.commit()

    return redirect(url_for('main.view_campaign', campaign_id=campaign.id))

@bp.route('/campaign/<int:campaign_id>/clear_list')
@login_required
def clear_recipient_list(campaign_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    if campaign.author != current_user: return redirect(url_for('main.index'))
    
    Recipient.query.filter_by(campaign_id=campaign.id).delete()
    log = ActivityLog(campaign_id=campaign.id, message="Recipient list cleared by user.", log_type="warning")
    db.session.add(log)
    db.session.commit()
    flash('Recipient list cleared.', 'warning')
    return redirect(url_for('main.view_campaign', campaign_id=campaign.id))

# --- Other Settings Routes (SMTP, Suppression, Tools) remain mostly same but integrated imports ---

@bp.route('/settings/smtp', methods=['GET', 'POST'])
@login_required
def smtp_profiles():
    if request.method == 'POST':
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
    if profile.user_id != current_user.id: return jsonify({'message': 'Unauthorized'}), 403
    from app.core_logic.smtp_handler import SMTPHandler
    handler = SMTPHandler(profile.to_dict())
    try:
        success, msg = handler.test_connection()
        return jsonify({'message': f'Test Result: {msg}'})
    except Exception as e:
        return jsonify({'message': f'Error: {str(e)}'})

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

@bp.route('/tools/validate_list', methods=['POST'])
@login_required
def validate_list_ajax():
    return jsonify({'success': True, 'message': 'Validation logic initiated (Feature pending full backend implementation).'})

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
