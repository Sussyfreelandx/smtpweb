from flask import (render_template, flash, redirect, url_for, request,
                   jsonify, current_app, Response)
from flask_login import login_user, logout_user, current_user, login_required
from app import db
from app.models import (User, Campaign, Recipient, SMTPServer,
                        Suppression, GlobalSettings)
from app.core_logic. deliverability import DeliverabilityHelper
from app.core_logic.ai_handler import AIHandler
from app. core_logic.smtp_handler import SMTPHandler
from app. core_logic.personalization import PersonalizationEngine
from app.utils import log_activity, get_logs
from app.main import bp
from flask_wtf import FlaskForm
from wtforms import StringField, SubmitField
from wtforms.validators import DataRequired
from werkzeug.utils import secure_filename
import csv
import io
import json
import os
import re
import threading
import time
from datetime import datetime


class DeliverabilityForm(FlaskForm):
    domain_ip = StringField('Domain or IP', validators=[DataRequired()])
    check_auth = SubmitField('Check Authentication')
    check_blacklist = SubmitField('Check Blacklist')


class SuppressionForm(FlaskForm):
    email = StringField('Email Address', validators=[DataRequired()])
    reason = StringField('Reason', default="Manual")
    submit = SubmitField('Add to Suppression List')


def is_valid_email(email):
    pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    return re.match(pattern, email) is not None


def html_to_plain_text(html):
    if not html:
        return ""
    text = re.sub(r'<(script|style).*?>.*?</\1>', '', html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'</(p|h[1-6]|li|div|tr)\s*>', '\n', text, flags=re. IGNORECASE)
    text = re. sub(r'<br\s*/? >', '\n', text, flags=re. IGNORECASE)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'&nbsp;', ' ', text)
    text = re.sub(r'&amp;', '&', text)
    text = re.sub(r'&lt;', '<', text)
    text = re.sub(r'&gt;', '>', text)
    text = re.sub(r'\s+', ' ', text)
    lines = [line.strip() for line in text.split('\n')]
    return '\n'.join(line for line in lines if line)


@bp.route('/')
@bp.route('/index')
@login_required
def index():
    campaigns = Campaign.query. filter_by(user_id=current_user.id).order_by(Campaign. timestamp.desc())
    return render_template('dashboard.html', title='Dashboard', campaigns=campaigns)


@bp.route('/campaign/<int:campaign_id>')
@login_required
def view_campaign(campaign_id):
    campaign = Campaign.query. get_or_404(campaign_id)
    if campaign.author != current_user: 
        flash("You do not have permission.", "danger")
        return redirect(url_for('main.index'))

    page = request.args. get('page', 1, type=int)
    recipients = campaign.recipients.order_by(Recipient. id.asc()).paginate(
        page=page, per_page=50, error_out=False
    )

    return render_template('campaign.html', title=campaign.name,
                          campaign=campaign, recipients=recipients)


@bp.route('/campaign/new', methods=['GET', 'POST'])
@login_required
def new_campaign():
    smtp_profiles = SMTPServer. query.filter_by(user_id=current_user.id).all()

    global_settings = GlobalSettings.query.first()
    default_burner = global_settings.burner_domain if global_settings else ""
    default_lure = global_settings.lure_path if global_settings else ""

    if request.method == 'POST':
        try:
            ab_enabled = 'ab_testing_enabled' in request. form

            body_html = request. form.get('body_html', '')
            body_plain = html_to_plain_text(body_html)

            campaign = Campaign(
                name=request.form['campaign_name'],
                subject=request.form['subject'],
                body_html=body_html,
                body_plain=body_plain,
                ab_testing_enabled=ab_enabled,
                subject_b=request.form. get('subject_b'),
                body_b=request.form.get('body_b'),
                ab_split_ratio=int(request.form.get('ab_split_ratio', 50)),
                burner_domain=request.form.get('burner_domain') or default_burner,
                lure_path=request. form.get('lure_path') or default_lure,
                smtp_profile_id=request.form.get('smtp_profile_id'),
                throttle_amount=int(request.form.get('throttle_amount', 20)),
                throttle_delay=int(request. form.get('throttle_delay', 60)),
                parallel_workers=int(request.form.get('parallel_workers', 10)),
                user_id=current_user.id
            )
            db.session.add(campaign)
            db.session.flush()

            file = request.files. get('recipients_file')
            if file and file.filename:
                stream = io.StringIO(file.stream.read().decode("UTF-8"), newline=None)
                csv_reader = csv. DictReader(stream)
                if csv_reader.fieldnames:
                    csv_reader.fieldnames = [f.lower().strip() for f in csv_reader.fieldnames]

                count = 0
                for row in csv_reader:
                    if 'email' in row and row['email']: 
                        email = row['email']. strip().lower()
                        if not is_valid_email(email):
                            continue
                        is_suppressed = Suppression.query. filter_by(email=email).first()
                        recipient = Recipient(
                            email=email,
                            campaign_id=campaign. id,
                            data=json.dumps(row),
                            status='Suppressed' if is_suppressed else 'Queued',
                            status_message='Suppressed by global list' if is_suppressed else None
                        )
                        db.session.add(recipient)
                        count += 1

                log_activity(f"Campaign '{campaign.name}' created with {count} recipients.", "SUCCESS")
                flash(f"Loaded {count} recipients.", "info")

            db.session. commit()
            flash('Campaign created! ', 'success')
            return redirect(url_for('main.view_campaign', campaign_id=campaign.id))

        except Exception as e:
            db.session.rollback()
            log_activity(f"Error creating campaign: {str(e)}", "ERROR")
            flash(f"Error creating campaign: {str(e)}", "danger")

    return render_template('create_campaign.html', title='New Campaign',
                          smtp_profiles=smtp_profiles,
                          default_burner=default_burner, default_lure=default_lure)


@bp.route('/campaign/<int: campaign_id>/add_recipient', methods=['POST'])
@login_required
def add_recipient_manual(campaign_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    if campaign.author != current_user: 
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403

    email = request.form. get('email', '').strip().lower()
    if not email: 
        return jsonify({'success': False, 'message': 'Email required'})

    if not is_valid_email(email):
        return jsonify({'success': False, 'message': 'Invalid email format'})

    exists = Recipient.query.filter_by(campaign_id=campaign. id, email=email).first()
    if exists:
        return jsonify({'success': False, 'message': 'Email already in list'})

    is_suppressed = Suppression.query. filter_by(email=email).first()

    recipient = Recipient(
        email=email,
        campaign_id=campaign. id,
        data=json.dumps({'email': email}),
        status='Suppressed' if is_suppressed else 'Queued',
        status_message='Suppressed by global list' if is_suppressed else None
    )
    db.session.add(recipient)
    db.session.commit()

    log_activity(f"Manually added {email} to campaign {campaign.name}", "INFO")
    return jsonify({'success': True, 'message':  'Recipient added'})


@bp.route('/campaign/<int:campaign_id>/control/<action>')
@login_required
def campaign_control(campaign_id, action):
    campaign = Campaign. query.get_or_404(campaign_id)
    if campaign.author != current_user:
        return redirect(url_for('main.index'))

    try:
        if action == 'start': 
            queued_count = campaign.recipients.filter_by(status='Queued').count()
            if queued_count == 0:
                flash('No queued recipients to send to.', 'warning')
                return redirect(url_for('main.view_campaign', campaign_id=campaign.id))

            if not campaign.smtp_profile:
                flash('No SMTP profile configured for this campaign.', 'danger')
                return redirect(url_for('main.view_campaign', campaign_id=campaign.id))

            smtp_config = campaign.smtp_profile. to_dict()
            if not smtp_config. get('password'):
                flash('SMTP password not configured.  Please update your SMTP profile.', 'danger')
                return redirect(url_for('main. view_campaign', campaign_id=campaign. id))

            campaign.status = 'Sending'
            db.session.commit()

            thread = threading.Thread(
                target=run_campaign_sending,
                args=(current_app._get_current_object(), campaign_id)
            )
            thread.daemon = True
            thread.start()

            log_activity(f"Started campaign:  {campaign.name}", "SUCCESS")
            flash('Campaign started successfully. ', 'success')

        elif action == 'pause':
            campaign.status = 'Paused'
            db.session. commit()
            log_activity(f"Paused campaign:  {campaign.name}", "WARNING")
            flash('Campaign paused.', 'warning')

        elif action == 'stop':
            campaign. status = 'Stopped'
            db.session.commit()
            log_activity(f"Stopped campaign: {campaign. name}", "ERROR")
            flash('Campaign stopped.', 'danger')

        elif action == 'retry':
            failed = campaign.recipients.filter_by(status='Failed').all()
            for r in failed:
                r.status = 'Queued'
                r.status_message = None
                r.attempts = 0
            db.session.commit()
            log_activity(f"Queued {len(failed)} failed recipients for retry.", "INFO")
            flash(f'Queued {len(failed)} failed recipients for retry. ', 'info')

    except Exception as e: 
        log_activity(f"Control Error ({action}): {str(e)}", "ERROR")
        flash(f"Error:  {str(e)}", "danger")
        if action == 'start':
            campaign.status = 'Draft'
            db. session.commit()

    return redirect(url_for('main. view_campaign', campaign_id=campaign. id))


def run_campaign_sending(app, campaign_id):
    with app.app_context():
        try:
            campaign = Campaign.query.get(campaign_id)
            if not campaign: 
                log_activity(f"Campaign {campaign_id} not found", "ERROR")
                return

            smtp_profile = campaign.smtp_profile
            if not smtp_profile:
                log_activity(f"No SMTP profile for campaign {campaign. name}", "ERROR")
                campaign.status = 'Failed'
                db. session.commit()
                return

            smtp_config = smtp_profile.to_dict()
            if not smtp_config.get('password'):
                log_activity(f"No password for SMTP profile {smtp_profile.profile_name}", "ERROR")
                campaign. status = 'Failed'
                db. session.commit()
                return

            smtp_handler = SMTPHandler(smtp_config)

            batch_size = campaign.throttle_amount or 20
            delay_seconds = campaign.throttle_delay or 60

            log_activity(f"Starting campaign:  {campaign.name}. Batch: {batch_size}, Delay: {delay_seconds}s", "INFO")

            while True:
                db.session.expire_all()
                campaign = Campaign.query.get(campaign_id)

                if not campaign or campaign.status != 'Sending': 
                    log_activity(f"Campaign {campaign_id} status changed.  Stopping.", "WARNING")
                    break

                recipients = campaign.recipients. filter_by(status='Queued').limit(batch_size).all()

                if not recipients:
                    campaign.status = 'Completed'
                    db.session.commit()
                    log_activity(f"Campaign {campaign. name} completed successfully.", "SUCCESS")
                    break

                log_activity(f"Processing batch of {len(recipients)} recipients...", "INFO")

                for recipient in recipients:
                    db.session.expire_all()
                    campaign = Campaign.query.get(campaign_id)

                    if not campaign or campaign.status != 'Sending': 
                        break

                    recipient = Recipient.query. get(recipient.id)
                    if not recipient or recipient.status != 'Queued': 
                        continue

                    try:
                        recipient.status = 'Sending'
                        recipient.attempts += 1
                        db.session.commit()

                        personalizer = PersonalizationEngine(campaign, recipient)
                        p_subject, p_body_html, p_body_plain = personalizer.personalize()

                        unsubscribe_token = recipient.get_tracking_token('unsubscribe')
                        unsubscribe_url = url_for('main.unsubscribe',
                                                  token=unsubscribe_token,
                                                  _external=True)

                        success, message = smtp_handler. send_email_sync(
                            to_email=recipient.email,
                            subject=p_subject,
                            html_content=p_body_html,
                            plain_content=p_body_plain,
                            unsubscribe_url=unsubscribe_url
                        )

                        if success:
                            recipient.status = 'Sent'
                            recipient.sent_at = datetime. utcnow()
                            recipient. status_message = "OK"
                            log_activity(f"Sent to {recipient.email}", "SUCCESS")
                        else:
                            recipient.status = 'Failed'
                            recipient.status_message = message[: 250] if message else "Unknown error"
                            log_activity(f"Failed to send to {recipient.email}:  {message}", "ERROR")

                        db.session.commit()

                    except Exception as e:
                        recipient.status = 'Failed'
                        recipient.status_message = str(e)[: 250]
                        db.session.commit()
                        log_activity(f"Exception sending to {recipient.email}: {e}", "ERROR")

                db.session.expire_all()
                campaign = Campaign.query.get(campaign_id)

                if campaign and campaign.status == 'Sending': 
                    remaining = campaign.recipients.filter_by(status='Queued').count()
                    if remaining > 0:
                        log_activity(f"Throttling:  waiting {delay_seconds}s.  {remaining} remaining.", "INFO")
                        time.sleep(delay_seconds)

        except Exception as e: 
            log_activity(f"Campaign sending error: {str(e)}", "ERROR")
            try:
                campaign = Campaign.query.get(campaign_id)
                if campaign: 
                    campaign. status = 'Failed'
                    db.session.commit()
            except Exception: 
                pass


@bp.route('/campaign/<int:campaign_id>/validate_list')
@login_required
def validate_list(campaign_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    if campaign.author != current_user: 
        return redirect(url_for('main.index'))

    recipients = campaign.recipients.filter_by(status='Queued').limit(100).all()
    helper = DeliverabilityHelper()
    count, valid, invalid = 0, 0, 0

    for r in recipients:
        try:
            domain = r.email. split('@')[1]
            mx_status = helper.check_mx_record(domain)
            if mx_status == "Valid":
                valid += 1
            else:
                r.status = 'Invalid'
                r.status_message = f"MX Check:  {mx_status}"
                invalid += 1
        except Exception:
            r. status = 'Invalid'
            r. status_message = "Invalid email format"
            invalid += 1
        count += 1

    db.session.commit()
    log_activity(f"Validated {count} recipients.  {valid} valid, {invalid} invalid.", "INFO")
    flash(f"Validated {count} emails. {valid} valid, {invalid} invalid.", "info")
    return redirect(url_for('main. view_campaign', campaign_id=campaign. id))


@bp.route('/campaign/<int:campaign_id>/clear_list')
@login_required
def clear_recipient_list(campaign_id):
    campaign = Campaign.query. get_or_404(campaign_id)
    if campaign. author != current_user:
        return redirect(url_for('main.index'))
    try:
        Recipient.query.filter_by(campaign_id=campaign.id).delete()
        db.session.commit()
        log_activity(f"Cleared recipient list for {campaign.name}", "WARNING")
        flash("Recipient list cleared.", "success")
    except Exception as e:
        flash(f"Error:  {e}", "danger")
    return redirect(url_for('main. view_campaign', campaign_id=campaign. id))


@bp.route('/campaign/<int:campaign_id>/export')
@login_required
def export_campaign_report(campaign_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    if campaign.author != current_user: 
        return redirect(url_for('main. index'))

    recipients = campaign.recipients. all()

    def generate():
        data = io.StringIO()
        w = csv.writer(data)
        w.writerow(('Email', 'Status', 'Sent At', 'Opened At', 'Clicked At', 'Attempts', 'Error Message'))
        yield data.getvalue()
        data.seek(0)
        data.truncate(0)
        for r in recipients: 
            w.writerow((r.email, r. status, r.sent_at, r.opened_at, r.clicked_at, r.attempts, r.status_message))
            yield data.getvalue()
            data.seek(0)
            data.truncate(0)

    response = Response(generate(), mimetype='text/csv')
    response.headers. set("Content-Disposition", "attachment", filename=f"report_{campaign.id}.csv")
    return response


@bp.route('/settings/smtp', methods=['GET', 'POST'])
@login_required
def smtp_profiles():
    if request.method == 'POST':
        try:
            profile_id = request.form. get('profile_id')
            if profile_id:
                profile = SMTPServer.query.get(profile_id)
                if not profile or profile.user_id != current_user.id:
                    flash("Profile not found.", "danger")
                    return redirect(url_for('main. smtp_profiles'))
            else:
                profile = SMTPServer(user_id=current_user.id)

            profile.profile_name = request. form.get('name')
            profile. server = request.form.get('server')
            profile.port = int(request.form. get('port', 587))
            profile. username = request.form. get('username')
            profile.sender_name = request.form. get('sender_name')
            profile. sender_email = request.form.get('sender_email')
            profile.use_tls = 'use_tls' in request.form
            profile.use_ssl = 'use_ssl' in request. form

            password = request.form. get('password')
            if password and password. strip():
                profile.set_password(password)

            db.session.add(profile)
            db.session.commit()
            log_activity(f"SMTP Profile saved: {profile.profile_name}", "SUCCESS")
            flash('SMTP Profile Saved. ', 'success')
        except Exception as e: 
            db.session.rollback()
            log_activity(f"Error saving SMTP profile: {e}", "ERROR")
            flash(f"Error saving profile: {str(e)}", "danger")

        return redirect(url_for('main.smtp_profiles'))

    profiles = SMTPServer. query.filter_by(user_id=current_user.id).all()
    return render_template('smtp_profiles.html', title='SMTP Profiles', profiles=profiles)


@bp.route('/settings/smtp/test', methods=['POST'])
@login_required
def test_smtp_connection():
    try:
        data = request.get_json()
        if not data: 
            return jsonify({'success': False, 'message': 'No data provided'}), 400

        profile_id = data. get('profile_id')
        if not profile_id: 
            return jsonify({'success': False, 'message': 'Profile ID required'}), 400

        profile = SMTPServer. query.get(profile_id)
        if not profile:
            return jsonify({'success': False, 'message': 'Profile not found'}), 404

        if profile.user_id != current_user.id:
            return jsonify({'success': False, 'message':  'Unauthorized'}), 403

        smtp_config = profile.to_dict()
        if not smtp_config.get('password'):
            return jsonify({'success':  False, 'message': 'Password not set for this profile'}), 400

        handler = SMTPHandler(smtp_config)
        success, msg = handler.test_connection()

        if success: 
            log_activity(f"SMTP Test successful for {profile.profile_name}", "SUCCESS")
            return jsonify({'success':  True, 'message': f'Success: {msg}'})
        else:
            log_activity(f"SMTP Test failed for {profile.profile_name}:  {msg}", "ERROR")
            return jsonify({'success': False, 'message':  f'Failed: {msg}'})

    except Exception as e:
        log_activity(f"SMTP Test error:  {str(e)}", "ERROR")
        return jsonify({'success': False, 'message': f'Error: {str(e)}'}), 500


@bp.route('/settings/smtp/delete/<int:profile_id>', methods=['POST'])
@login_required
def delete_smtp_profile(profile_id):
    profile = SMTPServer. query.get_or_404(profile_id)
    if profile.user_id != current_user.id:
        return redirect(url_for('main.smtp_profiles'))
    db.session.delete(profile)
    db.session.commit()
    flash('Profile deleted.', 'success')
    return redirect(url_for('main.smtp_profiles'))


@bp.route('/settings/suppression', methods=['GET', 'POST'])
@login_required
def suppression_list():
    form = SuppressionForm()
    if form.validate_on_submit():
        email = form.email. data. lower().strip()
        if not Suppression.query. filter_by(email=email).first():
            s = Suppression(email=email, reason=form.reason.data)
            db.session.add(s)
            db.session.commit()
            log_activity(f"Suppressed:  {email}", "WARNING")
            flash(f'{email} added. ', 'success')
        else:
            flash(f'{email} is already suppressed.', 'warning')
        return redirect(url_for('main.suppression_list'))
    page = request.args. get('page', 1, type=int)
    pagination = Suppression. query.order_by(Suppression. timestamp.desc()).paginate(page=page, per_page=50)
    return render_template('suppression. html', title='Suppression List', form=form, pagination=pagination)


@bp.route('/settings/suppression/delete/<int:suppressed_id>', methods=['POST'])
@login_required
def delete_suppressed_email(suppressed_id):
    item = Suppression. query.get_or_404(suppressed_id)
    db.session.delete(item)
    db.session.commit()
    flash('Removed from suppression list. ', 'success')
    return redirect(url_for('main.suppression_list'))


@bp.route('/settings/general', methods=['GET', 'POST'])
@login_required
def general_settings():
    settings = GlobalSettings.query.first()
    if not settings:
        settings = GlobalSettings()
        db.session.add(settings)
        db.session.commit()

    if request.method == 'POST': 
        settings.burner_domain = request.form. get('burner_domain')
        settings.lure_path = request. form.get('lure_path')

        pdf_file = request.files. get('template_pdf')
        if pdf_file and pdf_file.filename:
            filename = secure_filename(pdf_file.filename)
            upload_folder = os.path. join(current_app.root_path, 'static', 'uploads')
            os.makedirs(upload_folder, exist_ok=True)
            path = os.path. join(upload_folder, filename)
            pdf_file.save(path)
            settings.template_pdf_path = path
            log_activity(f"New PDF template uploaded: {filename}", "INFO")

        db.session.commit()
        log_activity("Global settings updated.", "SUCCESS")
        flash("Settings updated successfully.", "success")
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
            results = {'type': 'auth', 'target': target, 'auth':  helper.check_domain_authentication(target)}
        elif form.check_blacklist.data:
            results = {'type': 'blacklist', 'target':  target, 'blacklist': helper.check_blacklist(target)}
    return render_template('deliverability. html', title='Deliverability Tools', form=form, results=results)


@bp.route('/tools/ajax_analyze', methods=['POST'])
@login_required
def deliverability_tools_ajax():
    try:
        data = request.get_json()
        helper = DeliverabilityHelper()
        success, result = helper. analyze_spam_ai(
            data.get('subject'),
            data.get('body'),
            provider_type=data.get('provider', 'openai')
        )
        return jsonify({'success': success, 'result': result})
    except Exception as e:
        return jsonify({'success': False, 'result': str(e)})


@bp.route('/tools/ai_rewrite', methods=['POST'])
@login_required
def ai_rewrite():
    try:
        data = request.get_json()
        content = data.get('content')
        if not content:
            return jsonify({'success': False, 'result': 'No content'})
        handler = AIHandler()
        prompt = f"Rewrite the following email content to be more persuasive and clear.  Preserve HTML structure and placeholders like {{{{variable}}}}.\n\n{content}"
        success, result = handler. generate(prompt)
        return jsonify({'success':  success, 'result': result})
    except Exception as e: 
        return jsonify({'success': False, 'result': str(e)})


@bp.route('/tools/ai_subject', methods=['POST'])
@login_required
def ai_subject():
    try: 
        data = request. get_json()
        content = data. get('content')
        if not content: 
            return jsonify({'success': False, 'result': 'No content'})
        handler = AIHandler()
        prompt = f"Generate 3 short, catchy email subject lines.  Return only lines separated by newlines:\n\n{content}"
        success, result = handler.generate(prompt)
        return jsonify({'success': success, 'result': result})
    except Exception as e:
        return jsonify({'success': False, 'result':  str(e)})


@bp.route('/api/logs')
@login_required
def api_get_logs():
    return jsonify(get_logs())


@bp.route('/api/campaign/<int:campaign_id>/status')
@login_required
def api_campaign_status(campaign_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    if campaign.author != current_user:
        return jsonify({'error': 'Unauthorized'}), 403

    total = campaign.recipients.count()
    sent = campaign.recipients. filter_by(status='Sent').count()
    failed = campaign.recipients. filter_by(status='Failed').count()
    queued = campaign.recipients.filter_by(status='Queued').count()

    return jsonify({
        'status': campaign.status,
        'total': total,
        'sent':  sent,
        'failed': failed,
        'queued':  queued
    })


@bp.route('/unsubscribe/<token>')
def unsubscribe(token):
    try:
        from itsdangerous import URLSafeTimedSerializer as Serializer
        s = Serializer(current_app.config['SECRET_KEY'])
        data = s.loads(token, salt='track', max_age=86400 * 30)

        recipient_id = data. get('rid')
        if recipient_id:
            recipient = Recipient. query.get(recipient_id)
            if recipient: 
                recipient.status = 'Unsubscribed'
                db.session.commit()

                if not Suppression.query. filter_by(email=recipient.email).first():
                    suppression = Suppression(email=recipient. email, reason='Unsubscribed')
                    db. session.add(suppression)
                    db.session.commit()

                log_activity(f"Unsubscribed:  {recipient.email}", "INFO")

        return render_template('message.html',
                              message_title='Unsubscribed',
                              message_body='You have been successfully unsubscribed from our mailing list.')
    except Exception as e:
        log_activity(f"Unsubscribe error: {str(e)}", "ERROR")
        return render_template('message.html',
                              message_title='Error',
                              message_body='An error occurred processing your request.')


@bp.route('/track/open/<token>')
def track_open(token):
    try:
        from itsdangerous import URLSafeTimedSerializer as Serializer
        s = Serializer(current_app.config['SECRET_KEY'])
        data = s.loads(token, salt='track', max_age=86400 * 30)

        recipient_id = data.get('rid')
        if recipient_id: 
            recipient = Recipient.query.get(recipient_id)
            if recipient and not recipient.opened_at:
                recipient.opened_at = datetime.utcnow()
                if recipient.status not in ['Clicked', 'Unsubscribed']: 
                    recipient.status = 'Opened'
                db.session.commit()
    except Exception: 
        pass

    import base64
    pixel_data = base64.b64decode(b'R0lGODlhAQABAIAAAP///wAAACH5BAEAAAAALAAAAAABAAEAAAICRAEAOw==')
    response = Response(pixel_data, mimetype='image/gif')
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    return response


@bp. route('/track/click/<token>')
def track_click(token):
    redirect_url = request. args.get('url', '#')
    try:
        from itsdangerous import URLSafeTimedSerializer as Serializer
        s = Serializer(current_app.config['SECRET_KEY'])
        data = s.loads(token, salt='track', max_age=86400 * 30)

        if 'url' in data:
            redirect_url = data['url']

        recipient_id = data.get('rid')
        if recipient_id:
            recipient = Recipient. query.get(recipient_id)
            if recipient and not recipient.clicked_at:
                recipient.clicked_at = datetime.utcnow()
                if recipient.status != 'Unsubscribed': 
                    recipient.status = 'Clicked'
                db. session.commit()
    except Exception: 
        pass

    return redirect(redirect_url)


@bp.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('main.index'))
    if request.method == 'POST':
        user = User.query. filter_by(username=request.form. get('username')).first()
        if user and user.check_password(request. form.get('password')):
            login_user(user, remember=True)
            return redirect(url_for('main.index'))
        flash('Invalid credentials', 'danger')
    return render_template('login. html', title='Sign In')


@bp.route('/logout')
def logout():
    logout_user()
    return redirect(url_for('main.index'))


@bp.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('main.index'))
    if request. method == 'POST':
        username = request.form['username']
        email = request.form['email']

        if User.query.filter_by(username=username).first():
            flash('Username already exists', 'danger')
            return redirect(url_for('main.register'))

        if User.query.filter_by(email=email).first():
            flash('Email already registered', 'danger')
            return redirect(url_for('main.register'))

        user = User(username=username, email=email)
        user.set_password(request.form['password'])
        db.session.add(user)
        db.session.commit()
        flash('Registered! ', 'success')
        return redirect(url_for('main.login'))
    return render_template('register. html', title='Register')
