from flask import (render_template, flash, redirect, url_for, request,
                   jsonify, current_app, Response)
from flask_login import login_user, logout_user, current_user, login_required
from app import db
from app.models import (User, Campaign, Recipient, SMTPServer,
                        Suppression, GlobalSettings, Sequence, SequenceRecipient)
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
import base64
from datetime import datetime, timedelta
from collections import Counter


# ==================== FORMS ====================

class DeliverabilityForm(FlaskForm):
    domain_ip = StringField('Domain or IP', validators=[DataRequired()])
    check_auth = SubmitField('Check Authentication')
    check_blacklist = SubmitField('Check Blacklist')


class SuppressionForm(FlaskForm):
    email = StringField('Email Address', validators=[DataRequired()])
    reason = StringField('Reason', default="Manual")
    submit = SubmitField('Add to Suppression List')


# ==================== HELPER FUNCTIONS ====================

def is_valid_email(email):
    """Validate email format."""
    pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    return re.match(pattern, email) is not None


def html_to_plain_text(html):
    """Convert HTML to plain text."""
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


def get_rotation_smtp_profiles(user_id):
    """Get all active SMTP profiles for rotation."""
    profiles = SMTPServer. query.filter_by(user_id=user_id, is_active=True).order_by(SMTPServer.priority).all()
    valid_profiles = []
    for profile in profiles:
        profile.reset_daily_count_if_needed()
        if profile.get_password() and profile.sent_today < profile.daily_limit:
            valid_profiles.append(profile)
    return valid_profiles


def get_next_smtp_profile(profiles, current_index):
    """Get next available SMTP profile for rotation."""
    if not profiles:
        return None, 0
    next_index = (current_index + 1) % len(profiles)
    return profiles[next_index], next_index


# ==================== CAMPAIGN SENDING ====================

def run_campaign_sending(app, campaign_id):
    """Background task to send campaign emails."""
    with app.app_context():
        try:
            campaign = Campaign.query. get(campaign_id)
            if not campaign: 
                log_activity(f"Campaign {campaign_id} not found", "ERROR")
                return

            # Get SMTP profiles
            if campaign.smtp_rotation_enabled:
                smtp_profiles = get_rotation_smtp_profiles(campaign.user_id)
                if not smtp_profiles: 
                    log_activity(f"No valid SMTP profiles for rotation", "ERROR")
                    campaign.status = 'Failed'
                    db.session.commit()
                    return
                current_profile_index = 0
                smtp_profile = smtp_profiles[0]
            else: 
                smtp_profile = campaign.smtp_profile
                smtp_profiles = None
                current_profile_index = 0

            if not smtp_profile:
                log_activity(f"No SMTP profile for campaign {campaign. name}", "ERROR")
                campaign.status = 'Failed'
                db.session. commit()
                return

            smtp_config = smtp_profile.to_dict()
            if not smtp_config.get('password'):
                log_activity(f"No password for SMTP profile {smtp_profile.profile_name}", "ERROR")
                campaign.status = 'Failed'
                db.session. commit()
                return

            smtp_handler = SMTPHandler(smtp_config)

            batch_size = campaign.throttle_amount or 20
            delay_seconds = campaign.throttle_delay or 60
            attachments = campaign.get_attachments()

            log_activity(f"Starting campaign:  {campaign.name}.  Batch:  {batch_size}, Delay: {delay_seconds}s", "INFO")

            emails_sent_in_batch = 0
            total_sent = 0
            total_failed = 0

            while True:
                db.session.expire_all()
                campaign = Campaign.query.get(campaign_id)

                if not campaign or campaign.status != 'Sending': 
                    log_activity(f"Campaign {campaign_id} status changed.  Stopping.", "WARNING")
                    break

                recipients = campaign.recipients. filter_by(status='Queued').limit(batch_size).all()

                if not recipients: 
                    campaign.status = 'Completed'
                    campaign.completed_at = datetime. utcnow()
                    db. session.commit()
                    log_activity(f"Campaign {campaign. name} completed.  Sent:  {total_sent}, Failed:  {total_failed}", "SUCCESS")
                    break

                log_activity(f"Processing batch of {len(recipients)} recipients...", "INFO")

                for recipient in recipients:
                    db.session.expire_all()
                    campaign = Campaign.query.get(campaign_id)

                    if not campaign or campaign.status != 'Sending': 
                        break

                    recipient = Recipient. query.get(recipient.id)
                    if not recipient or recipient.status != 'Queued': 
                        continue

                    # SMTP Rotation logic
                    if smtp_profiles and campaign.smtp_rotation_enabled:
                        smtp_profile = smtp_profiles[current_profile_index]
                        smtp_profile. reset_daily_count_if_needed()
                        
                        if smtp_profile.sent_today >= smtp_profile.daily_limit:
                            smtp_profile, current_profile_index = get_next_smtp_profile(smtp_profiles, current_profile_index)
                            if not smtp_profile:
                                log_activity("All SMTP profiles reached daily limit", "WARNING")
                                campaign.status = 'Paused'
                                db.session.commit()
                                return
                        
                        smtp_config = smtp_profile. to_dict()
                        smtp_handler = SMTPHandler(smtp_config)

                    try:
                        recipient.status = 'Sending'
                        recipient.attempts += 1
                        db.session.commit()

                        # Personalize content
                        personalizer = PersonalizationEngine(campaign, recipient)
                        p_subject, p_body_html, p_body_plain = personalizer.personalize()

                        # Generate unsubscribe URL
                        unsubscribe_token = recipient.get_tracking_token('unsubscribe')
                        unsubscribe_url = url_for('main.unsubscribe', token=unsubscribe_token, _external=True)

                        # Send email
                        success, message = smtp_handler.send_email_sync(
                            to_email=recipient.email,
                            subject=p_subject,
                            html_content=p_body_html,
                            plain_content=p_body_plain,
                            unsubscribe_url=unsubscribe_url,
                            attachments=attachments
                        )

                        if success:
                            recipient.status = 'Sent'
                            recipient.sent_at = datetime.utcnow()
                            recipient.status_message = "OK"
                            total_sent += 1
                            emails_sent_in_batch += 1
                            
                            if smtp_profile:
                                smtp_profile.sent_today += 1
                            
                            log_activity(f"Sent to {recipient.email}", "SUCCESS")
                        else:
                            recipient.status = 'Failed'
                            recipient.status_message = message[: 250] if message else "Unknown error"
                            total_failed += 1
                            log_activity(f"Failed to send to {recipient.email}: {message}", "ERROR")

                        db.session.commit()

                        # Rotate to next SMTP profile after each email (if rotation enabled)
                        if smtp_profiles and campaign.smtp_rotation_enabled:
                            current_profile_index = (current_profile_index + 1) % len(smtp_profiles)

                    except Exception as e:
                        recipient.status = 'Failed'
                        recipient.status_message = str(e)[: 250]
                        total_failed += 1
                        db.session.commit()
                        log_activity(f"Exception sending to {recipient. email}: {e}", "ERROR")

                # Throttling delay between batches
                db.session.expire_all()
                campaign = Campaign.query. get(campaign_id)

                if campaign and campaign.status == 'Sending':
                    remaining = campaign.recipients.filter_by(status='Queued').count()
                    if remaining > 0:
                        log_activity(f"Throttling:  waiting {delay_seconds}s.  {remaining} remaining.", "INFO")
                        time.sleep(delay_seconds)
                        emails_sent_in_batch = 0

        except Exception as e: 
            log_activity(f"Campaign sending error: {str(e)}", "ERROR")
            try:
                campaign = Campaign.query.get(campaign_id)
                if campaign: 
                    campaign. status = 'Failed'
                    db.session.commit()
            except Exception: 
                pass


# ==================== MAIN ROUTES ====================

@bp.route('/')
@bp.route('/index')
@login_required
def index():
    """Dashboard view."""
    campaigns = Campaign.query. filter_by(user_id=current_user.id).order_by(Campaign. timestamp.desc())
    return render_template('dashboard.html', title='Dashboard', campaigns=campaigns)


@bp.route('/campaign/<int:campaign_id>')
@login_required
def view_campaign(campaign_id):
    """View single campaign with recipients and analytics."""
    campaign = Campaign.query.get_or_404(campaign_id)
    if campaign.author != current_user:
        flash("You do not have permission.", "danger")
        return redirect(url_for('main.index'))

    page = request.args. get('page', 1, type=int)
    recipients = campaign.recipients. order_by(Recipient.id.asc()).paginate(page=page, per_page=50, error_out=False)

    # Calculate analytics
    total = campaign.recipients.count()
    sent = campaign.recipients. filter_by(status='Sent').count()
    failed = campaign.recipients.filter_by(status='Failed').count()
    queued = campaign.recipients.filter_by(status='Queued').count()
    opened = campaign.recipients. filter(Recipient.opened_at. isnot(None)).count()
    clicked = campaign.recipients. filter(Recipient. clicked_at.isnot(None)).count()
    unsubscribed = campaign.recipients. filter_by(status='Unsubscribed').count()

    # A/B Testing analytics
    ab_stats = None
    if campaign. ab_testing_enabled:
        a_sent = campaign.recipients. filter_by(ab_version='A', status='Sent').count()
        b_sent = campaign. recipients.filter_by(ab_version='B', status='Sent').count()
        a_opened = campaign.recipients. filter(Recipient. ab_version == 'A', Recipient.opened_at.isnot(None)).count()
        b_opened = campaign.recipients.filter(Recipient.ab_version == 'B', Recipient. opened_at.isnot(None)).count()
        a_clicked = campaign. recipients.filter(Recipient.ab_version == 'A', Recipient.clicked_at.isnot(None)).count()
        b_clicked = campaign.recipients. filter(Recipient. ab_version == 'B', Recipient.clicked_at. isnot(None)).count()
        
        ab_stats = {
            'a_sent': a_sent,
            'b_sent': b_sent,
            'a_opened':  a_opened,
            'b_opened': b_opened,
            'a_clicked': a_clicked,
            'b_clicked': b_clicked,
            'a_open_rate': round((a_opened / a_sent * 100), 1) if a_sent > 0 else 0,
            'b_open_rate': round((b_opened / b_sent * 100), 1) if b_sent > 0 else 0,
            'a_click_rate': round((a_clicked / a_sent * 100), 1) if a_sent > 0 else 0,
            'b_click_rate': round((b_clicked / b_sent * 100), 1) if b_sent > 0 else 0,
        }

    analytics = {
        'total': total,
        'sent': sent,
        'failed': failed,
        'queued': queued,
        'opened': opened,
        'clicked': clicked,
        'unsubscribed': unsubscribed,
        'open_rate': round((opened / sent * 100), 1) if sent > 0 else 0,
        'click_rate': round((clicked / sent * 100), 1) if sent > 0 else 0
    }

    return render_template('campaign.html', title=campaign.name, campaign=campaign,
                           recipients=recipients, analytics=analytics, ab_stats=ab_stats)


@bp.route('/campaign/new', methods=['GET', 'POST'])
@login_required
def new_campaign():
    """Create new campaign."""
    smtp_profiles = SMTPServer.query. filter_by(user_id=current_user.id).all()

    global_settings = GlobalSettings.query.first()
    default_burner = global_settings.burner_domain if global_settings else ""
    default_lure = global_settings.lure_path if global_settings else ""
    default_throttle_amount = global_settings. default_throttle_amount if global_settings else 20
    default_throttle_delay = global_settings.default_throttle_delay if global_settings else 60

    if request.method == 'POST':
        try:
            ab_enabled = 'ab_testing_enabled' in request. form
            tracking_enabled = 'tracking_enabled' in request.form or request.form.get('tracking_enabled') == 'on'
            warmup_mode = 'warmup_mode' in request.form
            smtp_rotation = 'smtp_rotation_enabled' in request. form

            body_html = request.form.get('body_html', '')
            body_plain = html_to_plain_text(body_html)

            campaign = Campaign(
                name=request.form['campaign_name'],
                subject=request.form['subject'],
                body_html=body_html,
                body_plain=body_plain,
                ab_testing_enabled=ab_enabled,
                subject_b=request.form. get('subject_b'),
                body_b=request. form.get('body_b'),
                ab_split_ratio=int(request.form. get('ab_split_ratio', 50)),
                burner_domain=request.form.get('burner_domain') or default_burner,
                lure_path=request. form.get('lure_path') or default_lure,
                smtp_profile_id=request.form.get('smtp_profile_id'),
                throttle_amount=int(request.form. get('throttle_amount', default_throttle_amount)),
                throttle_delay=int(request. form.get('throttle_delay', default_throttle_delay)),
                parallel_workers=int(request. form.get('parallel_workers', 10)),
                tracking_enabled=tracking_enabled if tracking_enabled else True,
                warmup_mode=warmup_mode,
                smtp_rotation_enabled=smtp_rotation,
                user_id=current_user.id
            )

            # Handle scheduled time
            scheduled_date = request.form. get('scheduled_date')
            scheduled_time = request. form.get('scheduled_time')
            if scheduled_date and scheduled_time: 
                try:
                    campaign.scheduled_at = datetime.strptime(f"{scheduled_date} {scheduled_time}", "%Y-%m-%d %H:%M")
                except ValueError: 
                    pass

            db.session. add(campaign)
            db.session. flush()

            # Handle file upload
            file = request.files. get('recipients_file')
            if file and file. filename: 
                stream = io.StringIO(file.stream.read().decode("UTF-8"), newline=None)
                csv_reader = csv. DictReader(stream)
                if csv_reader.fieldnames:
                    csv_reader.fieldnames = [f.lower().strip() for f in csv_reader. fieldnames]

                count = 0
                skipped = 0
                for row in csv_reader:
                    if 'email' in row and row['email']: 
                        email = row['email']. strip().lower()
                        if not is_valid_email(email):
                            skipped += 1
                            continue
                        is_suppressed = Suppression.query. filter_by(email=email).first()
                        recipient = Recipient(
                            email=email,
                            campaign_id=campaign. id,
                            data=json.dumps(row),
                            status='Suppressed' if is_suppressed else 'Queued',
                            status_message='Suppressed by global list' if is_suppressed else None
                        )
                        db.session. add(recipient)
                        count += 1

                log_activity(f"Campaign '{campaign.name}' created with {count} recipients.  Skipped {skipped} invalid.", "SUCCESS")
                flash(f"Loaded {count} recipients.  Skipped {skipped} invalid emails.", "info")

            db.session. commit()
            flash('Campaign created! ', 'success')
            return redirect(url_for('main.view_campaign', campaign_id=campaign.id))

        except Exception as e: 
            db.session.rollback()
            log_activity(f"Error creating campaign: {str(e)}", "ERROR")
            flash(f"Error creating campaign: {str(e)}", "danger")

    return render_template('create_campaign.html', title='New Campaign',
                           smtp_profiles=smtp_profiles,
                           default_burner=default_burner, 
                           default_lure=default_lure,
                           default_throttle_amount=default_throttle_amount,
                           default_throttle_delay=default_throttle_delay)


@bp.route('/campaign/<int:campaign_id>/add_recipient', methods=['POST'])
@login_required
def add_recipient_manual(campaign_id):
    """Manually add a recipient to campaign."""
    campaign = Campaign.query.get_or_404(campaign_id)
    if campaign.author != current_user:
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403

    email = request.form.get('email', '').strip().lower()
    if not email:
        return jsonify({'success':  False, 'message': 'Email required'})

    if not is_valid_email(email):
        return jsonify({'success': False, 'message': 'Invalid email format'})

    exists = Recipient.query.filter_by(campaign_id=campaign. id, email=email).first()
    if exists:
        return jsonify({'success': False, 'message': 'Email already in list'})

    is_suppressed = Suppression.query.filter_by(email=email).first()

    recipient = Recipient(
        email=email,
        campaign_id=campaign. id,
        data=json.dumps({'email': email}),
        status='Suppressed' if is_suppressed else 'Queued',
        status_message='Suppressed by global list' if is_suppressed else None
    )
    db.session.add(recipient)
    db.session.commit()

    log_activity(f"Manually added {email} to campaign {campaign. name}", "INFO")
    return jsonify({'success': True, 'message': 'Recipient added'})


@bp.route('/campaign/<int:campaign_id>/control/<action>')
@login_required
def campaign_control(campaign_id, action):
    """Control campaign (start, pause, stop, retry)."""
    campaign = Campaign.query. get_or_404(campaign_id)
    if campaign. author != current_user:
        return redirect(url_for('main.index'))

    try:
        if action == 'start': 
            queued_count = campaign.recipients.filter_by(status='Queued').count()
            if queued_count == 0:
                flash('No queued recipients to send to.', 'warning')
                return redirect(url_for('main.view_campaign', campaign_id=campaign.id))

            if not campaign. smtp_profile and not campaign.smtp_rotation_enabled: 
                flash('No SMTP profile configured for this campaign.', 'danger')
                return redirect(url_for('main.view_campaign', campaign_id=campaign.id))

            if campaign.smtp_profile: 
                smtp_config = campaign.smtp_profile.to_dict()
                if not smtp_config.get('password'):
                    flash('SMTP password not configured.  Please update your SMTP profile.', 'danger')
                    return redirect(url_for('main.view_campaign', campaign_id=campaign.id))

            campaign.status = 'Sending'
            db.session. commit()

            thread = threading.Thread(
                target=run_campaign_sending,
                args=(current_app._get_current_object(), campaign_id)
            )
            thread.daemon = True
            thread.start()

            log_activity(f"Started campaign:  {campaign.name}", "SUCCESS")
            flash('Campaign started successfully.', 'success')

        elif action == 'pause':
            campaign.status = 'Paused'
            db.session. commit()
            log_activity(f"Paused campaign:  {campaign.name}", "WARNING")
            flash('Campaign paused.', 'warning')

        elif action == 'stop':
            campaign.status = 'Stopped'
            db. session.commit()
            log_activity(f"Stopped campaign: {campaign.name}", "ERROR")
            flash('Campaign stopped.', 'danger')

        elif action == 'retry':
            failed = campaign.recipients.filter_by(status='Failed').all()
            for r in failed:
                r.status = 'Queued'
                r.status_message = None
                r.attempts = 0
            db.session.commit()
            log_activity(f"Queued {len(failed)} failed recipients for retry.", "INFO")
            flash(f'Queued {len(failed)} failed recipients for retry.', 'info')

    except Exception as e: 
        log_activity(f"Control Error ({action}): {str(e)}", "ERROR")
        flash(f"Error: {str(e)}", "danger")
        if action == 'start':
            campaign.status = 'Draft'
            db. session.commit()

    return redirect(url_for('main.view_campaign', campaign_id=campaign.id))


@bp.route('/campaign/<int:campaign_id>/validate_list')
@login_required
def validate_list(campaign_id):
    """Validate recipient list with MX checks."""
    campaign = Campaign.query.get_or_404(campaign_id)
    if campaign.author != current_user: 
        return redirect(url_for('main.index'))

    recipients = campaign.recipients. filter_by(status='Queued').limit(100).all()
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
            r.status = 'Invalid'
            r.status_message = "Invalid email format"
            invalid += 1
        count += 1

    db.session.commit()
    log_activity(f"Validated {count} recipients.  {valid} valid, {invalid} invalid.", "INFO")
    flash(f"Validated {count} emails. {valid} valid, {invalid} invalid.", "info")
    return redirect(url_for('main.view_campaign', campaign_id=campaign.id))


@bp.route('/campaign/<int:campaign_id>/clear_list')
@login_required
def clear_recipient_list(campaign_id):
    """Clear all recipients from campaign."""
    campaign = Campaign. query.get_or_404(campaign_id)
    if campaign.author != current_user:
        return redirect(url_for('main.index'))
    try:
        Recipient.query.filter_by(campaign_id=campaign. id).delete()
        db.session. commit()
        log_activity(f"Cleared recipient list for {campaign.name}", "WARNING")
        flash("Recipient list cleared.", "success")
    except Exception as e:
        flash(f"Error:  {e}", "danger")
    return redirect(url_for('main.view_campaign', campaign_id=campaign.id))


@bp.route('/campaign/<int:campaign_id>/export')
@login_required
def export_campaign_report(campaign_id):
    """Export campaign report as CSV."""
    campaign = Campaign. query.get_or_404(campaign_id)
    if campaign.author != current_user:
        return redirect(url_for('main.index'))

    recipients = campaign.recipients. all()

    def generate():
        data = io.StringIO()
        w = csv.writer(data)
        w.writerow(('Email', 'Status', 'AB Version', 'Sent At', 'Opened At', 'Clicked At', 
                    'Open Count', 'Click Count', 'Attempts', 'Error Message'))
        yield data.getvalue()
        data.seek(0)
        data.truncate(0)
        for r in recipients: 
            w.writerow((r.email, r. status, r.ab_version or '', r.sent_at, r.opened_at, 
                        r.clicked_at, r.open_count, r.click_count, r.attempts, r.status_message))
            yield data.getvalue()
            data.seek(0)
            data.truncate(0)

    response = Response(generate(), mimetype='text/csv')
    response.headers. set("Content-Disposition", "attachment", filename=f"report_{campaign.id}. csv")
    return response


@bp.route('/campaign/<int:campaign_id>/delete', methods=['POST'])
@login_required
def delete_campaign(campaign_id):
    """Delete a campaign."""
    campaign = Campaign.query. get_or_404(campaign_id)
    if campaign. author != current_user:
        flash("You do not have permission.", "danger")
        return redirect(url_for('main.index'))
    
    try: 
        db.session.delete(campaign)
        db.session.commit()
        log_activity(f"Deleted campaign: {campaign. name}", "WARNING")
        flash("Campaign deleted.", "success")
    except Exception as e:
        flash(f"Error deleting campaign: {e}", "danger")
    
    return redirect(url_for('main. index'))


# ==================== SMTP SETTINGS ====================

@bp.route('/settings/smtp', methods=['GET', 'POST'])
@login_required
def smtp_profiles():
    """Manage SMTP profiles."""
    if request. method == 'POST':
        try:
            profile_id = request.form.get('profile_id')
            if profile_id:
                profile = SMTPServer.query.get(profile_id)
                if not profile or profile.user_id != current_user.id:
                    flash("Profile not found.", "danger")
                    return redirect(url_for('main.smtp_profiles'))
            else:
                profile = SMTPServer(user_id=current_user.id)

            profile.profile_name = request.form. get('name')
            profile.server = request.form.get('server')
            profile.port = int(request.form. get('port', 587))
            profile. username = request.form. get('username')
            profile.sender_name = request.form. get('sender_name')
            profile. sender_email = request.form.get('sender_email')
            profile.use_tls = 'use_tls' in request.form
            profile.use_ssl = 'use_ssl' in request.form
            profile.is_active = 'is_active' in request. form or request.form.get('is_active') == 'on'
            profile.daily_limit = int(request.form.get('daily_limit', 500))
            profile. priority = int(request. form.get('priority', 1))

            # IMAP settings
            profile.imap_server = request.form. get('imap_server')
            profile.imap_port = int(request. form.get('imap_port', 993))
            profile.imap_username = request.form. get('imap_username')
            
            imap_password = request.form.get('imap_password')
            if imap_password and imap_password. strip():
                profile.set_imap_password(imap_password)

            password = request.form. get('password')
            if password and password.strip():
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
    return render_template('smtp_profiles. html', title='SMTP Profiles', profiles=profiles)


@bp.route('/settings/smtp/test', methods=['POST'])
@login_required
def test_smtp_connection():
    """Test SMTP connection."""
    try:
        data = request.get_json()
        if not data: 
            return jsonify({'success': False, 'message': 'No data provided'}), 400

        profile_id = data. get('profile_id')
        if not profile_id: 
            return jsonify({'success': False, 'message': 'Profile ID required'}), 400

        profile = SMTPServer.query.get(profile_id)
        if not profile:
            return jsonify({'success': False, 'message': 'Profile not found'}), 404

        if profile.user_id != current_user.id:
            return jsonify({'success': False, 'message': 'Unauthorized'}), 403

        smtp_config = profile.to_dict()
        if not smtp_config. get('password'):
            return jsonify({'success': False, 'message': 'Password not set for this profile'}), 400

        handler = SMTPHandler(smtp_config)
        success, msg = handler.test_connection()

        if success: 
            log_activity(f"SMTP Test successful for {profile.profile_name}", "SUCCESS")
            return jsonify({'success':  True, 'message': f'Success: {msg}'})
        else:
            log_activity(f"SMTP Test failed for {profile.profile_name}:  {msg}", "ERROR")
            return jsonify({'success': False, 'message':  f'Failed: {msg}'})

    except Exception as e: 
        log_activity(f"SMTP Test error: {str(e)}", "ERROR")
        return jsonify({'success': False, 'message':  f'Error: {str(e)}'}), 500


@bp.route('/settings/smtp/delete/<int:profile_id>', methods=['POST'])
@login_required
def delete_smtp_profile(profile_id):
    """Delete SMTP profile."""
    profile = SMTPServer. query.get_or_404(profile_id)
    if profile.user_id != current_user. id:
        return redirect(url_for('main.smtp_profiles'))
    db.session.delete(profile)
    db.session.commit()
    flash('Profile deleted.', 'success')
    return redirect(url_for('main.smtp_profiles'))


# ==================== SUPPRESSION LIST ====================

@bp.route('/settings/suppression', methods=['GET', 'POST'])
@login_required
def suppression_list():
    """Manage suppression list."""
    form = SuppressionForm()
    if form.validate_on_submit():
        email = form.email. data.lower().strip()
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
    """Remove email from suppression list."""
    item = Suppression. query.get_or_404(suppressed_id)
    db.session.delete(item)
    db.session.commit()
    flash('Removed from suppression list. ', 'success')
    return redirect(url_for('main.suppression_list'))


@bp.route('/settings/suppression/import', methods=['POST'])
@login_required
def import_suppression_list():
    """Import suppression list from CSV."""
    file = request.files. get('file')
    if not file or not file.filename:
        flash('No file selected.', 'danger')
        return redirect(url_for('main.suppression_list'))
    
    try:
        stream = io.StringIO(file.stream. read().decode("UTF-8"), newline=None)
        csv_reader = csv.reader(stream)
        count = 0
        for row in csv_reader:
            if row and row[0]: 
                email = row[0].strip().lower()
                if is_valid_email(email) and not Suppression.query.filter_by(email=email).first():
                    reason = row[1] if len(row) > 1 else "Imported"
                    s = Suppression(email=email, reason=reason)
                    db.session. add(s)
                    count += 1
        db.session.commit()
        flash(f'Imported {count} emails to suppression list.', 'success')
    except Exception as e:
        flash(f'Error importing:  {e}', 'danger')
    
    return redirect(url_for('main. suppression_list'))


@bp.route('/settings/suppression/export')
@login_required
def export_suppression_list():
    """Export suppression list as CSV."""
    items = Suppression. query.all()
    
    def generate():
        data = io.StringIO()
        w = csv.writer(data)
        w.writerow(('Email', 'Reason', 'Date Added'))
        yield data.getvalue()
        data.seek(0)
        data.truncate(0)
        for item in items: 
            w.writerow((item.email, item.reason, item.timestamp))
            yield data.getvalue()
            data.seek(0)
            data.truncate(0)

    response = Response(generate(), mimetype='text/csv')
    response.headers.set("Content-Disposition", "attachment", filename="suppression_list. csv")
    return response


# ==================== GLOBAL SETTINGS ====================

@bp.route('/settings/general', methods=['GET', 'POST'])
@login_required
def general_settings():
    """Manage global settings."""
    settings = GlobalSettings.query.first()
    if not settings:
        settings = GlobalSettings()
        db.session.add(settings)
        db.session.commit()

    if request.method == 'POST':
        settings.burner_domain = request.form.get('burner_domain')
        settings.lure_path = request.form.get('lure_path')
        settings.default_throttle_amount = int(request.form. get('default_throttle_amount', 20))
        settings.default_throttle_delay = int(request.form.get('default_throttle_delay', 60))

        pdf_file = request.files. get('template_pdf')
        if pdf_file and pdf_file.filename:
            filename = secure_filename(pdf_file.filename)
            upload_folder = os. path.join(current_app.root_path, 'static', 'uploads')
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


# ==================== DELIVERABILITY TOOLS ====================

@bp.route('/tools/deliverability', methods=['GET', 'POST'])
@login_required
def deliverability_tools():
    """Deliverability checking tools."""
    form = DeliverabilityForm()
    results = None
    helper = DeliverabilityHelper()
    
    if form. validate_on_submit():
        target = form.domain_ip.data
        if form.check_auth.data:
            results = {'type': 'auth', 'target': target, 'auth':  helper.check_domain_authentication(target)}
        elif form.check_blacklist.data:
            results = {'type': 'blacklist', 'target':  target, 'blacklist': helper.check_blacklist(target)}
    
    return render_template('deliverability. html', title='Deliverability Tools', form=form, results=results)


@bp.route('/tools/spam_check', methods=['POST'])
@login_required
def spam_check():
    """Perform spam check on content."""
    try:
        data = request.get_json()
        subject = data.get('subject', '')
        body = data.get('body', '')
        check_type = data.get('type', 'basic')
        
        helper = DeliverabilityHelper()
        
        if check_type == 'ai':
            success, result = helper. analyze_spam_ai(subject, body)
            return jsonify({'success':  success, 'result': result})
        else:
            result = helper.basic_spam_check(subject, body)
            return jsonify({'success': True, 'result':  result})
    except Exception as e: 
        return jsonify({'success': False, 'result': str(e)})


@bp.route('/tools/link_check', methods=['POST'])
@login_required
def link_check():
    """Check health of links in content."""
    try:
        data = request.get_json()
        content = data.get('content', '')
        
        helper = DeliverabilityHelper()
        results = helper.check_link_health(content)
        
        return jsonify({'success': True, 'results': results})
    except Exception as e:
        return jsonify({'success':  False, 'error': str(e)})


@bp.route('/tools/ajax_analyze', methods=['POST'])
@login_required
def deliverability_tools_ajax():
    """AI-powered deliverability analysis."""
    try:
        data = request.get_json()
        helper = DeliverabilityHelper()
        success, result = helper. analyze_spam_ai(
            data.get('subject'),
            data.get('body'),
            provider_type=data.get('provider', 'openai')
        )
        return jsonify({'success':  success, 'result': result})
    except Exception as e: 
        return jsonify({'success': False, 'result': str(e)})


# ==================== AI TOOLS ====================

@bp.route('/tools/ai_rewrite', methods=['POST'])
@login_required
def ai_rewrite():
    """AI rewrite content."""
    try:
        data = request.get_json()
        content = data.get('content')
        if not content:
            return jsonify({'success': False, 'result': 'No content'})
        handler = AIHandler()
        success, result = handler. rewrite_content(content)
        return jsonify({'success':  success, 'result': result})
    except Exception as e: 
        return jsonify({'success': False, 'result': str(e)})


@bp.route('/tools/ai_subject', methods=['POST'])
@login_required
def ai_subject():
    """AI generate subject lines."""
    try:
        data = request.get_json()
        content = data.get('content')
        if not content:
            return jsonify({'success': False, 'result':  'No content'})
        handler = AIHandler()
        success, result = handler.generate_subjects(content)
        return jsonify({'success': success, 'result': result})
    except Exception as e:
        return jsonify({'success': False, 'result':  str(e)})


@bp.route('/tools/css_inline', methods=['POST'])
@login_required
def css_inline():
    """Inline CSS in HTML content."""
    try:
        data = request.get_json()
        content = data.get('content', '')
        
        try:
            import css_inline
            inliner = css_inline.CSSInliner()
            result = inliner. inline(content)
            return jsonify({'success':  True, 'result': result})
        except ImportError:
            return jsonify({'success':  False, 'result': 'css_inline library not installed'})
    except Exception as e: 
        return jsonify({'success': False, 'result': str(e)})


# ==================== ANALYTICS ====================

@bp.route('/analytics')
@login_required
def analytics_dashboard():
    """Analytics dashboard."""
    campaigns = Campaign.query. filter_by(user_id=current_user.id).order_by(Campaign. timestamp.desc()).limit(10).all()
    
    # Aggregate stats
    total_sent = 0
    total_opened = 0
    total_clicked = 0
    total_failed = 0
    
    campaign_stats = []
    for campaign in campaigns:
        sent = campaign.recipients. filter_by(status='Sent').count()
        opened = campaign.recipients.filter(Recipient.opened_at. isnot(None)).count()
        clicked = campaign.recipients.filter(Recipient.clicked_at. isnot(None)).count()
        failed = campaign.recipients.filter_by(status='Failed').count()
        
        total_sent += sent
        total_opened += opened
        total_clicked += clicked
        total_failed += failed
        
        campaign_stats.append({
            'id':  campaign.id,
            'name': campaign.name,
            'sent': sent,
            'opened':  opened,
            'clicked': clicked,
            'failed': failed,
            'open_rate': round((opened / sent * 100), 1) if sent > 0 else 0,
            'click_rate': round((clicked / sent * 100), 1) if sent > 0 else 0
        })
    
    summary = {
        'total_sent': total_sent,
        'total_opened':  total_opened,
        'total_clicked': total_clicked,
        'total_failed': total_failed,
        'avg_open_rate':  round((total_opened / total_sent * 100), 1) if total_sent > 0 else 0,
        'avg_click_rate':  round((total_clicked / total_sent * 100), 1) if total_sent > 0 else 0
    }
    
    return render_template('analytics.html', title='Analytics', 
                           campaign_stats=campaign_stats, summary=summary)


@bp.route('/campaign/<int:campaign_id>/click_heatmap')
@login_required
def click_heatmap(campaign_id):
    """Get click heatmap data for campaign."""
    campaign = Campaign.query.get_or_404(campaign_id)
    if campaign.author != current_user: 
        return jsonify({'error': 'Unauthorized'}), 403
    
    # Get all clicked links
    click_counts = Counter()
    recipients = campaign.recipients. filter(Recipient. clicked_links.isnot(None)).all()
    
    for recipient in recipients:
        try:
            links = json.loads(recipient. clicked_links)
            for link in links: 
                click_counts[link] += 1
        except: 
            pass
    
    return jsonify({'success': True, 'clicks': dict(click_counts)})


# ==================== API ENDPOINTS ====================

@bp.route('/api/logs')
@login_required
def api_get_logs():
    """Get activity logs."""
    return jsonify(get_logs())


@bp.route('/api/campaign/<int:campaign_id>/status')
@login_required
def api_campaign_status(campaign_id):
    """Get campaign status via API."""
    campaign = Campaign.query. get_or_404(campaign_id)
    if campaign. author != current_user:
        return jsonify({'error': 'Unauthorized'}), 403

    total = campaign.recipients.count()
    sent = campaign.recipients.filter_by(status='Sent').count()
    failed = campaign.recipients. filter_by(status='Failed').count()
    queued = campaign.recipients.filter_by(status='Queued').count()
    opened = campaign.recipients. filter(Recipient. opened_at.isnot(None)).count()
    clicked = campaign.recipients. filter(Recipient. clicked_at.isnot(None)).count()

    return jsonify({
        'status': campaign.status,
        'total': total,
        'sent':  sent,
        'failed': failed,
        'queued':  queued,
        'opened': opened,
        'clicked':  clicked,
        'open_rate': round((opened / sent * 100), 1) if sent > 0 else 0,
        'click_rate': round((clicked / sent * 100), 1) if sent > 0 else 0
    })


# ==================== TRACKING ENDPOINTS ====================

@bp.route('/unsubscribe/<token>')
def unsubscribe(token):
    """Handle unsubscribe requests."""
    try:
        from itsdangerous import URLSafeTimedSerializer as Serializer
        s = Serializer(current_app.config['SECRET_KEY'])
        data = s.loads(token, salt='track', max_age=86400 * 30)

        recipient_id = data. get('rid')
        if recipient_id:
            recipient = Recipient. query.get(recipient_id)
            if recipient: 
                recipient.status = 'Unsubscribed'
                recipient.unsubscribed_at = datetime. utcnow()
                db.session.commit()

                if not Suppression.query.filter_by(email=recipient.email).first():
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
    """Track email opens."""
    try:
        from itsdangerous import URLSafeTimedSerializer as Serializer
        s = Serializer(current_app.config['SECRET_KEY'])
        data = s.loads(token, salt='track', max_age=86400 * 30)

        recipient_id = data.get('rid')
        if recipient_id:
            recipient = Recipient.query.get(recipient_id)
            if recipient:
                recipient.open_count = (recipient.open_count or 0) + 1
                if not recipient.opened_at:
                    recipient.opened_at = datetime.utcnow()
                    if recipient.status not in ['Clicked', 'Unsubscribed']: 
                        recipient.status = 'Opened'
                
                # Store user agent and IP
                recipient.user_agent = request.headers.get('User-Agent', '')[:255]
                recipient.ip_address = request.remote_addr
                
                db.session.commit()
    except Exception: 
        pass

    # Return 1x1 transparent pixel
    pixel_data = base64.b64decode(b'R0lGODlhAQABAIAAAP///wAAACH5BAEAAAAALAAAAAABAAEAAAICRAEAOw==')
    response = Response(pixel_data, mimetype='image/gif')
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    return response


@bp.route('/track/click/<token>')
def track_click(token):
    """Track link clicks."""
    redirect_url = request. args.get('url', '#')
    
    try:
        from itsdangerous import URLSafeTimedSerializer as Serializer
        s = Serializer(current_app.config['SECRET_KEY'])
        data = s. loads(token, salt='track', max_age=86400 * 30)

        # Decode URL from payload
        if 'url' in data:
            try:
                redirect_url = base64.urlsafe_b64decode(data['url']. encode()).decode()
            except:
                redirect_url = data['url']

        recipient_id = data. get('rid')
        if recipient_id:
            recipient = Recipient.query. get(recipient_id)
            if recipient:
                recipient.click_count = (recipient.click_count or 0) + 1
                if not recipient.clicked_at:
                    recipient.clicked_at = datetime.utcnow()
                    if recipient.status != 'Unsubscribed': 
                        recipient.status = 'Clicked'
                
                # Store clicked link
                recipient. add_clicked_link(redirect_url)
                
                # Store user agent and IP
                recipient.user_agent = request.headers.get('User-Agent', '')[:255]
                recipient. ip_address = request.remote_addr
                
                db. session.commit()
    except Exception: 
        pass

    return redirect(redirect_url)


# ==================== AUTHENTICATION ====================

@bp.route('/login', methods=['GET', 'POST'])
def login():
    """User login."""
    if current_user.is_authenticated:
        return redirect(url_for('main.index'))
    if request.method == 'POST':
        user = User.query. filter_by(username=request.form. get('username')).first()
        if user and user.check_password(request. form.get('password')):
            login_user(user, remember=True)
            next_page = request. args.get('next')
            return redirect(next_page or url_for('main.index'))
        flash('Invalid username or password', 'danger')
    return render_template('login. html', title='Sign In')


@bp.route('/logout')
def logout():
    """User logout."""
    logout_user()
    return redirect(url_for('main.login'))


@bp.route('/register', methods=['GET', 'POST'])
def register():
    """User registration."""
    if current_user.is_authenticated:
        return redirect(url_for('main.index'))
    if request.method == 'POST':
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
        flash('Registration successful!  Please login.', 'success')
        return redirect(url_for('main.login'))
    return render_template('register. html', title='Register')
