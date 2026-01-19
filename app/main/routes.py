from flask import (render_template, flash, redirect, url_for, request,
                   jsonify, current_app, Response)
from flask_login import login_user, logout_user, current_user, login_required
from app import db
from app.models import (User, Campaign, Recipient, SMTPServer,
                        Suppression, GlobalSettings)
from app.main import bp
from app.utils import log_activity, get_logs
from flask_wtf import FlaskForm
from wtforms import StringField, SubmitField
from wtforms.validators import DataRequired
from werkzeug.utils import secure_filename
import csv
import io
import json
import os
import re
import base64
from datetime import datetime


# ==================== FORMS ====================

class DeliverabilityForm(FlaskForm):
    domain_ip = StringField('Domain or IP', validators=[DataRequired()])
    check_auth = SubmitField('Check Authentication')
    check_blacklist = SubmitField('Check Blacklist')


class SuppressionForm(FlaskForm):
    email = StringField('Email Address', validators=[DataRequired()])
    reason = StringField('Reason', default="Manual")
    submit = SubmitField('Add to Suppression List')


# ==================== HELPERS ====================

def is_valid_email(email):
    if not email:
        return False
    pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    return re.match(pattern, email) is not None


def html_to_plain_text(html):
    if not html:
        return ""
    text = re.sub(r'<(script|style).*?>.*?</\1>', '', html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'</(p|h[1-6]|li|div|tr)\s*>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


# ==================== MAIN ROUTES ====================

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

    # Analytics
    total = campaign.recipients.count()
    sent = campaign.recipients.filter_by(status='Sent').count()
    failed = campaign.recipients.filter_by(status='Failed').count()
    queued = campaign.recipients.filter_by(status='Queued').count()
    opened = campaign.recipients.filter(Recipient.opened_at.isnot(None)).count()
    clicked = campaign.recipients.filter(Recipient.clicked_at.isnot(None)).count()

    analytics = {
        'total': total, 'sent': sent, 'failed': failed, 'queued': queued,
        'opened': opened, 'clicked': clicked,
        'open_rate': round((opened / sent * 100), 1) if sent > 0 else 0,
        'click_rate': round((clicked / sent * 100), 1) if sent > 0 else 0
    }

    ab_stats = None
    if campaign.ab_testing_enabled:
        a_sent = campaign.recipients.filter_by(ab_version='A', status='Sent').count()
        b_sent = campaign.recipients.filter_by(ab_version='B', status='Sent').count()
        a_opened = campaign.recipients.filter(Recipient.ab_version == 'A', Recipient.opened_at.isnot(None)).count()
        b_opened = campaign.recipients.filter(Recipient.ab_version == 'B', Recipient.opened_at.isnot(None)).count()
        a_clicked = campaign.recipients.filter(Recipient.ab_version == 'A', Recipient.clicked_at.isnot(None)).count()
        b_clicked = campaign.recipients.filter(Recipient.ab_version == 'B', Recipient.clicked_at.isnot(None)).count()
        ab_stats = {
            'a_sent': a_sent, 'b_sent': b_sent,
            'a_opened': a_opened, 'b_opened': b_opened,
            'a_clicked': a_clicked, 'b_clicked': b_clicked,
            'a_open_rate': round((a_opened / a_sent * 100), 1) if a_sent > 0 else 0,
            'b_open_rate': round((b_opened / b_sent * 100), 1) if b_sent > 0 else 0,
            'a_click_rate': round((a_clicked / a_sent * 100), 1) if a_sent > 0 else 0,
            'b_click_rate': round((b_clicked / b_sent * 100), 1) if b_sent > 0 else 0,
        }

    return render_template('campaign.html', title=campaign.name, campaign=campaign,
                           recipients=recipients, analytics=analytics, ab_stats=ab_stats)


@bp.route('/campaign/new', methods=['GET', 'POST'])
@login_required
def new_campaign():
    smtp_profiles = SMTPServer.query.filter_by(user_id=current_user.id).all()
    global_settings = GlobalSettings.query.first()
    
    default_burner = global_settings.burner_domain if global_settings else ""
    default_lure = global_settings.lure_path if global_settings else ""
    default_throttle_amount = global_settings.default_throttle_amount if global_settings else 20
    default_throttle_delay = global_settings.default_throttle_delay if global_settings else 60

    if request.method == 'POST':
        try:
            ab_enabled = 'ab_testing_enabled' in request.form
            tracking_enabled = 'tracking_enabled' in request.form
            warmup_mode = 'warmup_mode' in request.form
            smtp_rotation = 'smtp_rotation_enabled' in request.form

            body_html = request.form.get('body_html', '')
            body_plain = html_to_plain_text(body_html)
            
            smtp_id = request.form.get('smtp_profile_id')

            campaign = Campaign(
                name=request.form['campaign_name'],
                subject=request.form['subject'],
                body_html=body_html,
                body_plain=body_plain,
                ab_testing_enabled=ab_enabled,
                subject_b=request.form.get('subject_b'),
                body_b=request.form.get('body_b'),
                ab_split_ratio=int(request.form.get('ab_split_ratio', 50)),
                burner_domain=request.form.get('burner_domain') or default_burner,
                lure_path=request.form.get('lure_path') or default_lure,
                smtp_profile_id=int(smtp_id) if smtp_id else None,
                throttle_amount=int(request.form.get('throttle_amount', default_throttle_amount)),
                throttle_delay=int(request.form.get('throttle_delay', default_throttle_delay)),
                tracking_enabled=tracking_enabled,
                warmup_mode=warmup_mode,
                smtp_rotation_enabled=smtp_rotation,
                user_id=current_user.id
            )

            db.session.add(campaign)
            db.session.flush()

            # Handle CSV upload
            file = request.files.get('recipients_file')
            if file and file.filename:
                stream = io.StringIO(file.stream.read().decode("UTF-8"), newline=None)
                csv_reader = csv.DictReader(stream)
                if csv_reader.fieldnames:
                    csv_reader.fieldnames = [f.lower().strip() for f in csv_reader.fieldnames]

                count = 0
                for row in csv_reader:
                    if 'email' in row and row['email']:
                        email = row['email'].strip().lower()
                        if not is_valid_email(email):
                            continue
                        is_suppressed = Suppression.query.filter_by(email=email).first()
                        recipient = Recipient(
                            email=email,
                            campaign_id=campaign.id,
                            data=json.dumps(row),
                            status='Suppressed' if is_suppressed else 'Queued',
                            status_message='Suppressed' if is_suppressed else None
                        )
                        db.session.add(recipient)
                        count += 1

                log_activity(f"Campaign '{campaign.name}' created with {count} recipients.", "SUCCESS")
                flash(f"Loaded {count} recipients.", "info")

            db.session.commit()
            flash('Campaign created!', 'success')
            return redirect(url_for('main.view_campaign', campaign_id=campaign.id))

        except Exception as e:
            db.session.rollback()
            log_activity(f"Error creating campaign: {str(e)}", "ERROR")
            flash(f"Error creating campaign: {str(e)}", "danger")

    return render_template('create_campaign.html', title='New Campaign',
                           smtp_profiles=smtp_profiles,
                           default_burner=default_burner, default_lure=default_lure,
                           default_throttle_amount=default_throttle_amount,
                           default_throttle_delay=default_throttle_delay)


@bp.route('/campaign/<int:campaign_id>/add_recipient', methods=['POST'])
@login_required
def add_recipient_manual(campaign_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    if campaign.author != current_user:
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403

    email = request.form.get('email', '').strip().lower()
    if not email or not is_valid_email(email):
        return jsonify({'success': False, 'message': 'Invalid email'})

    if Recipient.query.filter_by(campaign_id=campaign.id, email=email).first():
        return jsonify({'success': False, 'message': 'Already exists'})

    is_suppressed = Suppression.query.filter_by(email=email).first()
    recipient = Recipient(
        email=email, campaign_id=campaign.id,
        data=json.dumps({'email': email}),
        status='Suppressed' if is_suppressed else 'Queued'
    )
    db.session.add(recipient)
    db.session.commit()

    log_activity(f"Added {email} to {campaign.name}", "INFO")
    return jsonify({'success': True, 'message': 'Added'})


@bp.route('/campaign/<int:campaign_id>/control/<action>')
@login_required
def campaign_control(campaign_id, action):
    campaign = Campaign.query.get_or_404(campaign_id)
    if campaign.author != current_user:
        return redirect(url_for('main.index'))

    if action == 'start':
        campaign.status = 'Sending'
        campaign.started_at = datetime.utcnow()
        db.session.commit()
        log_activity(f"Started: {campaign.name}", "SUCCESS")
        flash('Campaign started.', 'success')
    elif action == 'pause':
        campaign.status = 'Paused'
        db.session.commit()
        log_activity(f"Paused: {campaign.name}", "WARNING")
        flash('Campaign paused.', 'warning')
    elif action == 'stop':
        campaign.status = 'Stopped'
        db.session.commit()
        log_activity(f"Stopped: {campaign.name}", "ERROR")
        flash('Campaign stopped.', 'danger')
    elif action == 'retry':
        failed = campaign.recipients.filter_by(status='Failed').all()
        for r in failed:
            r.status = 'Queued'
            r.status_message = None
            r.attempts = 0
        db.session.commit()
        flash(f'Queued {len(failed)} for retry.', 'info')

    return redirect(url_for('main.view_campaign', campaign_id=campaign.id))


@bp.route('/campaign/<int:campaign_id>/validate_list')
@login_required
def validate_list(campaign_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    if campaign.author != current_user:
        return redirect(url_for('main.index'))

    recipients = campaign.recipients.filter_by(status='Queued').limit(100).all()
    valid, invalid = 0, 0
    for r in recipients:
        if is_valid_email(r.email):
            valid += 1
        else:
            r.status = 'Invalid'
            invalid += 1
    db.session.commit()
    flash(f"Validated: {valid} valid, {invalid} invalid.", "info")
    return redirect(url_for('main.view_campaign', campaign_id=campaign.id))


@bp.route('/campaign/<int:campaign_id>/clear_list')
@login_required
def clear_recipient_list(campaign_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    if campaign.author != current_user:
        return redirect(url_for('main.index'))
    Recipient.query.filter_by(campaign_id=campaign.id).delete()
    db.session.commit()
    flash("List cleared.", "success")
    return redirect(url_for('main.view_campaign', campaign_id=campaign.id))


@bp.route('/campaign/<int:campaign_id>/export')
@login_required
def export_campaign_report(campaign_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    if campaign.author != current_user:
        return redirect(url_for('main.index'))

    recipients = campaign.recipients.all()

    def generate():
        data = io.StringIO()
        w = csv.writer(data)
        w.writerow(('Email', 'Status', 'Sent At', 'Opened', 'Clicked', 'Attempts', 'Message'))
        yield data.getvalue()
        data.seek(0)
        data.truncate(0)
        for r in recipients:
            w.writerow((r.email, r.status, r.sent_at, r.opened_at, r.clicked_at, r.attempts, r.status_message))
            yield data.getvalue()
            data.seek(0)
            data.truncate(0)

    response = Response(generate(), mimetype='text/csv')
    response.headers.set("Content-Disposition", "attachment", filename=f"report_{campaign.id}.csv")
    return response


@bp.route('/campaign/<int:campaign_id>/delete', methods=['POST'])
@login_required
def delete_campaign(campaign_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    if campaign.author != current_user:
        flash("Not authorized.", "danger")
        return redirect(url_for('main.index'))
    db.session.delete(campaign)
    db.session.commit()
    flash("Deleted.", "success")
    return redirect(url_for('main.index'))


# ==================== SMTP ====================

@bp.route('/settings/smtp', methods=['GET', 'POST'])
@login_required
def smtp_profiles():
    if request.method == 'POST':
        try:
            profile_id = request.form.get('profile_id')
            if profile_id:
                profile = SMTPServer.query.get(profile_id)
                if not profile or profile.user_id != current_user.id:
                    flash("Not found.", "danger")
                    return redirect(url_for('main.smtp_profiles'))
            else:
                profile = SMTPServer(user_id=current_user.id)

            profile.profile_name = request.form.get('name')
            profile.server = request.form.get('server')
            profile.port = int(request.form.get('port', 587))
            profile.username = request.form.get('username')
            profile.sender_name = request.form.get('sender_name')
            profile.sender_email = request.form.get('sender_email')
            profile.use_tls = 'use_tls' in request.form
            profile.use_ssl = 'use_ssl' in request.form
            profile.is_active = 'is_active' in request.form
            profile.daily_limit = int(request.form.get('daily_limit', 500))
            profile.priority = int(request.form.get('priority', 1))
            profile.imap_server = request.form.get('imap_server')
            profile.imap_port = int(request.form.get('imap_port', 993))
            profile.imap_username = request.form.get('imap_username')

            password = request.form.get('password')
            if password and password.strip():
                profile.set_password(password)

            imap_password = request.form.get('imap_password')
            if imap_password and imap_password.strip():
                profile.set_imap_password(imap_password)

            db.session.add(profile)
            db.session.commit()
            flash('Saved.', 'success')
        except Exception as e:
            db.session.rollback()
            flash(f"Error: {e}", "danger")

        return redirect(url_for('main.smtp_profiles'))

    profiles = SMTPServer.query.filter_by(user_id=current_user.id).all()
    return render_template('smtp_profiles.html', title='SMTP Profiles', profiles=profiles)


@bp.route('/settings/smtp/test', methods=['POST'])
@login_required
def test_smtp_connection():
    try:
        data = request.get_json()
        profile_id = data.get('profile_id')
        profile = SMTPServer.query.get(profile_id)
        
        if not profile or profile.user_id != current_user.id:
            return jsonify({'success': False, 'message': 'Not found'}), 404

        password = profile.get_password()
        if not password:
            return jsonify({'success': False, 'message': 'No password set'})

        import smtplib
        import ssl

        context = ssl.create_default_context()
        if profile.use_ssl or profile.port == 465:
            server = smtplib.SMTP_SSL(profile.server, profile.port, context=context, timeout=10)
        else:
            server = smtplib.SMTP(profile.server, profile.port, timeout=10)
            if profile.use_tls:
                server.starttls(context=context)

        server.login(profile.username, password)
        server.quit()

        return jsonify({'success': True, 'message': 'Connection successful!'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})


@bp.route('/settings/smtp/delete/<int:profile_id>', methods=['POST'])
@login_required
def delete_smtp_profile(profile_id):
    profile = SMTPServer.query.get_or_404(profile_id)
    if profile.user_id != current_user.id:
        return redirect(url_for('main.smtp_profiles'))
    db.session.delete(profile)
    db.session.commit()
    flash('Deleted.', 'success')
    return redirect(url_for('main.smtp_profiles'))


# ==================== SUPPRESSION ====================

@bp.route('/settings/suppression', methods=['GET', 'POST'])
@login_required
def suppression_list():
    form = SuppressionForm()
    if form.validate_on_submit():
        email = form.email.data.lower().strip()
        if not Suppression.query.filter_by(email=email).first():
            db.session.add(Suppression(email=email, reason=form.reason.data))
            db.session.commit()
            flash(f'{email} added.', 'success')
        else:
            flash('Already exists.', 'warning')
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
    flash('Removed.', 'success')
    return redirect(url_for('main.suppression_list'))


@bp.route('/settings/suppression/import', methods=['POST'])
@login_required
def import_suppression_list():
    file = request.files.get('file')
    if not file:
        flash('No file.', 'danger')
        return redirect(url_for('main.suppression_list'))

    try:
        stream = io.StringIO(file.stream.read().decode("UTF-8"), newline=None)
        count = 0
        for row in csv.reader(stream):
            if row and row[0]:
                email = row[0].strip().lower()
                if is_valid_email(email) and not Suppression.query.filter_by(email=email).first():
                    db.session.add(Suppression(email=email, reason=row[1] if len(row) > 1 else "Imported"))
                    count += 1
        db.session.commit()
        flash(f'Imported {count}.', 'success')
    except Exception as e:
        flash(f'Error: {e}', 'danger')

    return redirect(url_for('main.suppression_list'))


@bp.route('/settings/suppression/export')
@login_required
def export_suppression_list():
    items = Suppression.query.all()

    def generate():
        data = io.StringIO()
        w = csv.writer(data)
        w.writerow(('Email', 'Reason', 'Date'))
        yield data.getvalue()
        data.seek(0)
        data.truncate(0)
        for item in items:
            w.writerow((item.email, item.reason, item.timestamp))
            yield data.getvalue()
            data.seek(0)
            data.truncate(0)

    response = Response(generate(), mimetype='text/csv')
    response.headers.set("Content-Disposition", "attachment", filename="suppression.csv")
    return response


# ==================== SETTINGS ====================

@bp.route('/settings/general', methods=['GET', 'POST'])
@login_required
def general_settings():
    settings = GlobalSettings.query.first()
    if not settings:
        settings = GlobalSettings()
        db.session.add(settings)
        db.session.commit()

    if request.method == 'POST':
        settings.burner_domain = request.form.get('burner_domain')
        settings.lure_path = request.form.get('lure_path')
        settings.default_throttle_amount = int(request.form.get('default_throttle_amount', 20))
        settings.default_throttle_delay = int(request.form.get('default_throttle_delay', 60))

        pdf_file = request.files.get('template_pdf')
        if pdf_file and pdf_file.filename:
            filename = secure_filename(pdf_file.filename)
            upload_folder = os.path.join(current_app.root_path, 'static', 'uploads')
            os.makedirs(upload_folder, exist_ok=True)
            path = os.path.join(upload_folder, filename)
            pdf_file.save(path)
            settings.template_pdf_path = path

        db.session.commit()
        flash("Saved.", "success")
        return redirect(url_for('main.general_settings'))

    return render_template('settings_general.html', title='Settings', settings=settings)


# ==================== DELIVERABILITY ====================

@bp.route('/tools/deliverability', methods=['GET', 'POST'])
@login_required
def deliverability_tools():
    form = DeliverabilityForm()
    results = None

    if form.validate_on_submit():
        target = form.domain_ip.data
        if form.check_auth.data:
            results = {'type': 'auth', 'target': target, 'auth': {
                'spf': '⚠️ Check manually', 'dmarc': '⚠️ Check manually', 'dkim': '⚠️ Check manually'
            }}
            try:
                import dns.resolver
                resolver = dns.resolver.Resolver()
                resolver.timeout = 3

                try:
                    txt = resolver.resolve(target, 'TXT')
                    spf = next((str(r) for r in txt if 'v=spf1' in str(r).lower()), None)
                    results['auth']['spf'] = "✅ Found" if spf else "❌ Missing"
                except:
                    results['auth']['spf'] = "❌ Missing"

                try:
                    dmarc = resolver.resolve(f'_dmarc.{target}', 'TXT')
                    results['auth']['dmarc'] = "✅ Found" if dmarc else "❌ Missing"
                except:
                    results['auth']['dmarc'] = "❌ Missing"

                for sel in ["google", "selector1", "default"]:
                    try:
                        resolver.resolve(f'{sel}._domainkey.{target}', 'TXT')
                        results['auth']['dkim'] = "✅ Found"
                        break
                    except:
                        continue
            except ImportError:
                pass
        elif form.check_blacklist.data:
            results = {'type': 'blacklist', 'target': target, 'blacklist': '⚠️ Check mxtoolbox.com'}

    return render_template('deliverability.html', title='Deliverability', form=form, results=results)


@bp.route('/tools/ajax_analyze', methods=['POST'])
@login_required
def deliverability_tools_ajax():
    data = request.get_json()
    subject = data.get('subject', '')
    body = data.get('body', '')
    
    analysis = ["=== Analysis ===\n"]
    if len(subject) > 60:
        analysis.append("⚠️ Subject too long")
    if not body:
        analysis.append("❌ Empty body")
    if "unsubscribe" not in body.lower():
        analysis.append("⚠️ No unsubscribe link")
    
    return jsonify({'success': True, 'result': '\n'.join(analysis)})


@bp.route('/tools/spam_check', methods=['POST'])
@login_required
def spam_check():
    data = request.get_json()
    full_text = (data.get('subject', '') + " " + data.get('body', '')).lower()
    
    triggers = []
    for word in ["free", "guarantee", "urgent", "winner", "cash", "buy now"]:
        if word in full_text:
            triggers.append(word)
    
    return jsonify({'success': True, 'result': {
        'score': min(len(triggers), 10), 'rating': "Low" if len(triggers) < 3 else "High", 'triggers': triggers
    }})


@bp.route('/tools/link_check', methods=['POST'])
@login_required
def link_check():
    data = request.get_json()
    links = re.findall(r'href=["\']([^"\']+)["\']', data.get('content', ''))
    if not links:
        return jsonify({'success': True, 'results': {'message': 'No links found'}})
    return jsonify({'success': True, 'results': {link: '⚠️ Check manually' for link in set(links)}})


@bp.route('/tools/ai_rewrite', methods=['POST'])
@login_required
def ai_rewrite():
    return jsonify({'success': False, 'result': 'AI requires OpenAI API key in settings.'})


@bp.route('/tools/ai_subject', methods=['POST'])
@login_required
def ai_subject():
    return jsonify({'success': False, 'result': 'AI requires OpenAI API key in settings.'})


@bp.route('/tools/css_inline', methods=['POST'])
@login_required
def css_inline():
    try:
        import css_inline
        data = request.get_json()
        result = css_inline.CSSInliner().inline(data.get('content', ''))
        return jsonify({'success': True, 'result': result})
    except ImportError:
        return jsonify({'success': False, 'result': 'css_inline not installed'})


# ==================== ANALYTICS ====================

@bp.route('/analytics')
@login_required
def analytics_dashboard():
    campaigns = Campaign.query.filter_by(user_id=current_user.id).order_by(Campaign.timestamp.desc()).limit(10).all()
    
    total_sent, total_opened, total_clicked, total_failed = 0, 0, 0, 0
    campaign_stats = []
    
    for c in campaigns:
        sent = c.recipients.filter_by(status='Sent').count()
        opened = c.recipients.filter(Recipient.opened_at.isnot(None)).count()
        clicked = c.recipients.filter(Recipient.clicked_at.isnot(None)).count()
        failed = c.recipients.filter_by(status='Failed').count()
        
        total_sent += sent
        total_opened += opened
        total_clicked += clicked
        total_failed += failed
        
        campaign_stats.append({
            'id': c.id, 'name': c.name, 'sent': sent, 'opened': opened,
            'clicked': clicked, 'failed': failed,
            'open_rate': round((opened / sent * 100), 1) if sent > 0 else 0,
            'click_rate': round((clicked / sent * 100), 1) if sent > 0 else 0
        })

    summary = {
        'total_sent': total_sent, 'total_opened': total_opened,
        'total_clicked': total_clicked, 'total_failed': total_failed,
        'avg_open_rate': round((total_opened / total_sent * 100), 1) if total_sent > 0 else 0,
        'avg_click_rate': round((total_clicked / total_sent * 100), 1) if total_sent > 0 else 0
    }

    return render_template('analytics.html', title='Analytics', campaign_stats=campaign_stats, summary=summary)


# ==================== API ====================

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
    return jsonify({
        'status': campaign.status,
        'sent': campaign.recipients.filter_by(status='Sent').count(),
        'failed': campaign.recipients.filter_by(status='Failed').count()
    })


# ==================== TRACKING ====================

@bp.route('/unsubscribe/<token>')
def unsubscribe(token):
    try:
        from itsdangerous import URLSafeTimedSerializer as Serializer
        s = Serializer(current_app.config['SECRET_KEY'])
        data = s.loads(token, salt='track', max_age=86400*30)
        
        recipient = Recipient.query.get(data.get('rid'))
        if recipient:
            recipient.status = 'Unsubscribed'
            recipient.unsubscribed_at = datetime.utcnow()
            if not Suppression.query.filter_by(email=recipient.email).first():
                db.session.add(Suppression(email=recipient.email, reason='Unsubscribed'))
            db.session.commit()
        
        return render_template('message.html', title='Unsubscribed',
                               message_title='Unsubscribed', message_body='You have been unsubscribed.')
    except:
        return render_template('message.html', title='Error',
                               message_title='Error', message_body='Invalid request.')


@bp.route('/track/open/<token>')
def track_open(token):
    try:
        from itsdangerous import URLSafeTimedSerializer as Serializer
        s = Serializer(current_app.config['SECRET_KEY'])
        data = s.loads(token, salt='track', max_age=86400*30)
        
        recipient = Recipient.query.get(data.get('rid'))
        if recipient:
            recipient.open_count = (recipient.open_count or 0) + 1
            if not recipient.opened_at:
                recipient.opened_at = datetime.utcnow()
                if recipient.status not in ['Clicked', 'Unsubscribed']:
                    recipient.status = 'Opened'
            db.session.commit()
    except:
        pass

    pixel = base64.b64decode(b'R0lGODlhAQABAIAAAP///wAAACH5BAEAAAAALAAAAAABAAEAAAICRAEAOw==')
    response = Response(pixel, mimetype='image/gif')
    response.headers['Cache-Control'] = 'no-cache'
    return response


@bp.route('/track/click/<token>')
def track_click(token):
    redirect_url = '#'
    try:
        from itsdangerous import URLSafeTimedSerializer as Serializer
        s = Serializer(current_app.config['SECRET_KEY'])
        data = s.loads(token, salt='track', max_age=86400*30)
        
        if 'url' in data:
            redirect_url = base64.urlsafe_b64decode(data['url'].encode()).decode()
        
        recipient = Recipient.query.get(data.get('rid'))
        if recipient:
            recipient.click_count = (recipient.click_count or 0) + 1
            if not recipient.clicked_at:
                recipient.clicked_at = datetime.utcnow()
                if recipient.status != 'Unsubscribed':
                    recipient.status = 'Clicked'
            db.session.commit()
    except:
        pass

    return redirect(redirect_url)


# ==================== AUTH ====================

@bp.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('main.index'))
    if request.method == 'POST':
        user = User.query.filter_by(username=request.form.get('username')).first()
        if user and user.check_password(request.form.get('password')):
            login_user(user, remember=True)
            return redirect(request.args.get('next') or url_for('main.index'))
        flash('Invalid credentials', 'danger')
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
        username = request.form['username']
        email = request.form['email']
        
        if User.query.filter_by(username=username).first():
            flash('Username exists', 'danger')
            return redirect(url_for('main.register'))
        
        if User.query.filter_by(email=email).first():
            flash('Email exists', 'danger')
            return redirect(url_for('main.register'))
        
        user = User(username=username, email=email)
        user.set_password(request.form['password'])
        db.session.add(user)
        db.session.commit()
        flash('Registered! Please login.', 'success')
        return redirect(url_for('main.login'))
    return render_template('register.html', title='Register')
