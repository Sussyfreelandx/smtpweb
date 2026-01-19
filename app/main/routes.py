from flask import render_template, flash, redirect, url_for, request, jsonify, Blueprint, Response, current_app
from flask_login import login_user, logout_user, current_user, login_required
from app import db
from app.models import User, Campaign, Recipient, SMTPServer, Suppression, GlobalSettings
from app.core_logic.deliverability import DeliverabilityHelper
from app.core_logic.ai_handler import AIHandler
from app.utils import log_activity, get_logs
from flask_wtf import FlaskForm
from wtforms import StringField, SubmitField
from wtforms.validators import DataRequired
from werkzeug.utils import secure_filename
import csv
import io
import json
import os
import time

bp = Blueprint('main', __name__)

class DeliverabilityForm(FlaskForm):
    domain_ip = StringField('Domain or IP', validators=[DataRequired()])
    check_auth = SubmitField('Check Authentication')
    check_blacklist = SubmitField('Check Blacklist')

class SuppressionForm(FlaskForm):
    email = StringField('Email Address', validators=[DataRequired()])
    reason = StringField('Reason', default="Manual")
    submit = SubmitField('Add to Suppression List')

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
        flash("You do not have permission.", "danger")
        return redirect(url_for('main.index'))
    page = request.args.get('page', 1, type=int)
    recipients = campaign.recipients.order_by(Recipient.id.asc()).paginate(page=page, per_page=50, error_out=False)
    return render_template('campaign.html', title=campaign.name, campaign=campaign, recipients=recipients)

@bp.route('/campaign/new', methods=['GET', 'POST'])
@login_required
def new_campaign():
    smtp_profiles = SMTPServer.query.filter_by(user_id=current_user.id).all()
    global_settings = GlobalSettings.query.first()
    default_burner = global_settings.burner_domain if global_settings else ""
    default_lure = global_settings.lure_path if global_settings else ""
    
    if request.method == 'POST':
        try:
            campaign = Campaign(
                name=request.form['campaign_name'],
                subject=request.form['subject'],
                body=request.form['body_html'],
                ab_testing_enabled='ab_testing_enabled' in request.form,
                subject_b=request.form.get('subject_b'),
                body_b=request.form.get('body_b'),
                ab_split_ratio=int(request.form.get('ab_split_ratio', 50)),
                burner_domain=request.form.get('burner_domain') or default_burner,
                lure_path=request.form.get('lure_path') or default_lure,
                smtp_profile_id=request.form.get('smtp_profile_id'),
                throttle_amount=int(request.form.get('throttle_amount', 20)),
                throttle_delay=int(request.form.get('throttle_delay', 60)),
                parallel_workers=int(request.form.get('parallel_workers', 10)),
                user_id=current_user.id
            )
            db.session.add(campaign)
            db.session.flush()
            
            file = request.files.get('recipients_file')
            if file:
                # Use utf-8-sig to handle Excel BOM
                stream = io.StringIO(file.stream.read().decode("utf-8-sig"), newline=None)
                csv_reader = csv.DictReader(stream)
                
                count = 0
                for row in csv_reader:
                    # --- SMART CSV NORMALIZATION START ---
                    # This ensures 'Autograb' works even if the CSV has weird headers like "F. Name" or "Company Name"
                    clean_row = {}
                    for k, v in row.items():
                        if not k: continue # Skip empty headers
                        # Clean key: remove spaces, underscores, lowercase
                        key = k.lower().strip().replace(' ', '').replace('_', '').replace('.', '')
                        
                        # Map common variations to standard keys needed for Autograb
                        if key in ['firstname', 'fname', 'first', 'name', 'givenname']: 
                            clean_row['firstname'] = v
                        elif key in ['lastname', 'lname', 'last', 'surname']: 
                            clean_row['lastname'] = v
                        elif key in ['company', 'companyname', 'business', 'org', 'organization']: 
                            clean_row['company'] = v
                        elif key == 'email':
                            clean_row['email'] = v
                        else: 
                            clean_row[k] = v # Keep other specific columns as-is
                    # --- SMART CSV NORMALIZATION END ---

                    if 'email' in clean_row and clean_row['email']:
                        email = clean_row['email'].strip().lower()
                        is_suppressed = Suppression.query.filter_by(email=email).first()
                        
                        recipient = Recipient(
                            email=email, 
                            campaign_id=campaign.id,
                            # Store the NORMALIZED data so personalization engine can find 'firstname' easily
                            data=json.dumps(clean_row),
                            status='Suppressed' if is_suppressed else 'Queued',
                            status_message='Suppressed by global list' if is_suppressed else None
                        )
                        db.session.add(recipient)
                        count += 1
                
                log_activity(f"Campaign '{campaign.name}' created with {count} recipients.", "SUCCESS")
                flash(f"Loaded {count} recipients.", "info")
            
            db.session.commit()
            return redirect(url_for('main.view_campaign', campaign_id=campaign.id))
        except Exception as e:
            db.session.rollback()
            log_activity(f"Error creating campaign: {str(e)}", "ERROR")
            flash(f"Error creating campaign: {str(e)}", "danger")
        
    return render_template('create_campaign.html', title='New Campaign', smtp_profiles=smtp_profiles,
                           default_burner=default_burner, default_lure=default_lure)

@bp.route('/campaign/<int:campaign_id>/add_recipient', methods=['POST'])
@login_required
def add_recipient_manual(campaign_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    if campaign.author != current_user: return jsonify({'success': False, 'message': 'Unauthorized'}), 403
    
    email = request.form.get('email', '').strip().lower()
    if not email: return jsonify({'success': False, 'message': 'Email required'})
    
    if Recipient.query.filter_by(campaign_id=campaign.id, email=email).first():
        return jsonify({'success': False, 'message': 'Email already in list'})
        
    is_suppressed = Suppression.query.filter_by(email=email).first()
    # For manual add, we create a basic JSON with just the email
    data_payload = {'email': email, 'firstname': '', 'company': ''} 
    
    recipient = Recipient(
        email=email, campaign_id=campaign.id,
        data=json.dumps(data_payload), 
        status='Suppressed' if is_suppressed else 'Queued',
        status_message='Suppressed' if is_suppressed else None
    )
    db.session.add(recipient)
    db.session.commit()
    return jsonify({'success': True, 'message': 'Recipient added'})

@bp.route('/campaign/<int:campaign_id>/control/<action>')
@login_required
def campaign_control(campaign_id, action):
    campaign = Campaign.query.get_or_404(campaign_id)
    if campaign.author != current_user: return redirect(url_for('main.index'))

    try:
        if action == 'start':
            campaign.status = 'Sending'
            db.session.commit()
            from app.tasks import send_campaign_task
            send_campaign_task.delay(campaign_id)
            log_activity(f"Resumed campaign: {campaign.name}", "SUCCESS")
            flash('Campaign started successfully.', 'success')
        elif action == 'pause':
            campaign.status = 'Paused'
            db.session.commit()
            log_activity(f"Paused campaign: {campaign.name}", "WARNING")
        elif action == 'stop':
            campaign.status = 'Stopped'
            db.session.commit()
            log_activity(f"Stopped campaign: {campaign.name}", "ERROR")
        elif action == 'retry':
            failed = campaign.recipients.filter_by(status='Failed').all()
            for r in failed:
                r.status = 'Queued'
                r.status_message = None
            db.session.commit()
            flash(f'Queued {len(failed)} failed recipients for retry.', 'info')
    except Exception as e:
        log_activity(f"Control Error: {e}", "ERROR")
        flash(f"Error: {e}", "danger")
        
    return redirect(url_for('main.view_campaign', campaign_id=campaign.id))

@bp.route('/campaign/<int:campaign_id>/validate_list')
@login_required
def validate_list(campaign_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    recipients = campaign.recipients.filter_by(status='Queued').limit(100).all() 
    helper = DeliverabilityHelper()
    count = 0
    for r in recipients:
        try:
            domain = r.email.split('@')[1]
            mx_status = helper.check_mx_record(domain) if hasattr(helper, 'check_mx_record') else "Skipped"
            if mx_status != "Valid" and mx_status != "Skipped":
                r.status = 'Invalid'
                r.status_message = f"MX Check: {mx_status}"
            count += 1
        except: pass
    db.session.commit()
    flash(f"Validated {count} emails (limited batch).", "info")
    return redirect(url_for('main.view_campaign', campaign_id=campaign.id))

@bp.route('/campaign/<int:campaign_id>/clear_list')
@login_required
def clear_recipient_list(campaign_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    if campaign.author != current_user: return redirect(url_for('main.index'))
    Recipient.query.filter_by(campaign_id=campaign.id).delete()
    db.session.commit()
    flash("Recipient list cleared.", "success")
    return redirect(url_for('main.view_campaign', campaign_id=campaign.id))

@bp.route('/campaign/<int:campaign_id>/export')
@login_required
def export_campaign_report(campaign_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    if campaign.author != current_user: return redirect(url_for('main.index'))
    recipients = campaign.recipients.all()
    def generate():
        data = io.StringIO()
        w = csv.writer(data)
        w.writerow(('Email', 'Status', 'Sent At', 'Opened At', 'Clicked At', 'Error Message'))
        yield data.getvalue()
        data.seek(0); data.truncate(0)
        for r in recipients:
            w.writerow((r.email, r.status, r.sent_at, r.opened_at, r.clicked_at, r.status_message))
            yield data.getvalue()
            data.seek(0); data.truncate(0)
    response = Response(generate(), mimetype='text/csv')
    response.headers.set("Content-Disposition", "attachment", filename=f"report_{campaign.id}.csv")
    return response

@bp.route('/settings/smtp', methods=['GET', 'POST'])
@login_required
def smtp_profiles():
    if request.method == 'POST':
        try:
            profile_id = request.form.get('profile_id')
            if profile_id:
                profile = SMTPServer.query.get(profile_id)
                if not profile or profile.user_id != current_user.id: return redirect(url_for('main.index'))
            else:
                profile = SMTPServer(user_id=current_user.id)
            profile.profile_name = request.form.get('name')
            profile.server = request.form.get('server')
            profile.port = int(request.form.get('port'))
            profile.username = request.form.get('username')
            profile.sender_name = request.form.get('sender_name')
            profile.sender_email = request.form.get('sender_email')
            profile.use_tls = 'use_tls' in request.form
            profile.use_ssl = 'use_ssl' in request.form
            password = request.form.get('password')
            if password and password.strip(): profile.set_password(password)
            db.session.add(profile)
            db.session.commit()
            flash('SMTP Profile Saved.', 'success')
        except Exception as e:
            flash(f"Error saving profile: {e}", "danger")
        return redirect(url_for('main.smtp_profiles'))
    profiles = SMTPServer.query.filter_by(user_id=current_user.id).all()
    return render_template('smtp_profiles.html', title='SMTP Profiles', profiles=profiles)

@bp.route('/settings/smtp/test', methods=['POST'])
@login_required
def test_smtp_connection():
    try:
        data = request.get_json()
        profile = SMTPServer.query.get_or_404(data.get('profile_id'))
        if profile.user_id != current_user.id: return jsonify({'message': 'Unauthorized'}), 403
        
        from app.core_logic.smtp_handler import SMTPHandler
        config = profile.to_dict()
        if not config.get('password'):
            return jsonify({'message': '❌ Failed: Password missing or decryption error.'}), 400
            
        handler = SMTPHandler(config)
        success, msg = handler.test_connection()
        return jsonify({'message': f'✅ Success: {msg}' if success else f'❌ Failed: {msg}'}), 200 if success else 400
    except Exception as e:
        return jsonify({'message': f'Error: {e}'}), 500

@bp.route('/settings/smtp/delete/<int:profile_id>', methods=['POST'])
@login_required
def delete_smtp_profile(profile_id):
    profile = SMTPServer.query.get_or_404(profile_id)
    if profile.user_id != current_user.id: return redirect(url_for('main.smtp_profiles'))
    db.session.delete(profile)
    db.session.commit()
    flash('Profile deleted.', 'success')
    return redirect(url_for('main.smtp_profiles'))

@bp.route('/settings/suppression', methods=['GET', 'POST'])
@login_required
def suppression_list():
    form = SuppressionForm()
    if form.validate_on_submit():
        if not Suppression.query.filter_by(email=form.email.data.lower()).first():
            db.session.add(Suppression(email=form.email.data.lower(), reason=form.reason.data))
            db.session.commit()
            flash(f'{form.email.data} added.', 'success')
        else:
            flash('Already suppressed.', 'warning')
        return redirect(url_for('main.suppression_list'))
    page = request.args.get('page', 1, type=int)
    pagination = Suppression.query.order_by(Suppression.timestamp.desc()).paginate(page=page, per_page=50)
    return render_template('suppression.html', title='Suppression List', form=form, pagination=pagination)

@bp.route('/settings/suppression/delete/<int:suppressed_id>', methods=['POST'])
@login_required
def delete_suppressed_email(suppressed_id):
    db.session.delete(Suppression.query.get_or_404(suppressed_id))
    db.session.commit()
    flash('Removed.', 'success')
    return redirect(url_for('main.suppression_list'))

@bp.route('/settings/general', methods=['GET', 'POST'])
@login_required
def general_settings():
    settings = GlobalSettings.query.first() or GlobalSettings()
    if not settings.id: db.session.add(settings); db.session.commit()
    
    if request.method == 'POST':
        settings.burner_domain = request.form.get('burner_domain')
        settings.lure_path = request.form.get('lure_path')
        pdf = request.files.get('template_pdf')
        if pdf and pdf.filename:
            filename = secure_filename(pdf.filename)
            upload_folder = os.path.join(current_app.root_path, 'static', 'uploads')
            os.makedirs(upload_folder, exist_ok=True)
            path = os.path.join(upload_folder, filename)
            pdf.save(path)
            settings.template_pdf_path = path
        db.session.commit()
        flash("Settings updated.", "success")
        return redirect(url_for('main.general_settings'))
    return render_template('settings_general.html', settings=settings)

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
    success, result = DeliverabilityHelper().analyze_spam_ai(data.get('subject'), data.get('body'), provider_type=data.get('provider', 'openai'))
    return jsonify({'success': success, 'result': result})

@bp.route('/tools/ai_rewrite', methods=['POST'])
@login_required
def ai_rewrite():
    try:
        data = request.get_json()
        content = data.get('content')
        if not content: return jsonify({'success': False, 'result': 'No content'})
        handler = AIHandler()
        prompt = f"Rewrite to be persuasive. Preserve HTML/Jinja2:\n\n{content}"
        success, result = handler.generate(prompt)
        return jsonify({'success': success, 'result': result})
    except Exception as e:
        return jsonify({'success': False, 'result': str(e)})

@bp.route('/tools/ai_subject', methods=['POST'])
@login_required
def ai_subject():
    try:
        data = request.get_json()
        content = data.get('content')
        if not content: return jsonify({'success': False, 'result': 'No content'})
        handler = AIHandler()
        prompt = f"Generate 3 short email subjects:\n\n{content}"
        success, result = handler.generate(prompt)
        return jsonify({'success': success, 'result': result})
    except Exception as e:
        return jsonify({'success': False, 'result': str(e)})

@bp.route('/api/logs')
@login_required
def api_get_logs():
    return jsonify(get_logs())

# Auth Routes
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
        if User.query.filter_by(username=request.form['username']).first():
            flash('Username taken', 'danger')
        else:
            user = User(username=request.form['username'], email=request.form['email'])
            user.set_password(request.form['password'])
            db.session.add(user)
            db.session.commit()
            flash('Registered!', 'success')
            return redirect(url_for('main.login'))
    return render_template('register.html', title='Register')
