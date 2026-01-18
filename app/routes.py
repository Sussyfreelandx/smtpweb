from flask import render_template, flash, redirect, url_for, request, jsonify, Blueprint, make_response
from flask_login import login_user, logout_user, current_user, login_required
from app import db, create_app
from app.models import User, Campaign, Recipient, SMTPServer, Suppression
from app.tasks import send_campaign_task, test_smtp_connection_task
import csv
import io
import json
import redis
import os
from werkzeug.utils import secure_filename
from datetime import datetime

bp = Blueprint('main', __name__)
redis_client = redis.from_url(os.environ.get('REDIS_URL', 'redis://localhost:6379/0'))

# --- Campaign Management ---

@bp.route('/campaign/new', methods=['GET', 'POST'])
@login_required
def new_campaign():
    if request.method == 'POST':
        profile_id = request.form.get('smtp_profile')
        campaign = Campaign(
            name=request.form['campaign_name'],
            subject=request.form['subject'],
            body=request.form['body_html'],
            smtp_profile_id=profile_id,
            user_id=current_user.id,
            throttle_amount=int(request.form.get('throttle_amount', 20)),
            throttle_delay=int(request.form.get('throttle_delay', 1)),
            throttle_unit=request.form.get('throttle_unit', 'Minutes')
        )
        db.session.add(campaign)
        db.session.commit() # Commit ID first
        
        flash('Campaign created! Now add recipients.', 'success')
        return redirect(url_for('main.view_campaign', campaign_id=campaign.id))
        
    profiles = SMTPServer.query.filter_by(user_id=current_user.id).all()
    return render_template('create_campaign.html', title='New Campaign', profiles=profiles)

@bp.route('/campaign/<int:campaign_id>', methods=['GET'])
@login_required
def view_campaign(campaign_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    if campaign.author != current_user:
        flash("Permission denied.", "danger")
        return redirect(url_for('main.index'))
    
    page = request.args.get('page', 1, type=int)
    recipients = campaign.recipients.order_by(Recipient.id.asc()).paginate(page=page, per_page=50)
    
    return render_template('campaign.html', campaign=campaign, recipients=recipients, now=datetime.utcnow())

# --- Recipient Management (Load, Add, Clear, Validate) ---

@bp.route('/campaign/<int:campaign_id>/import_csv', methods=['POST'])
@login_required
def import_csv(campaign_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    file = request.files.get('file')
    if not file:
        flash('No file uploaded', 'danger')
        return redirect(url_for('main.view_campaign', campaign_id=campaign_id))

    try:
        stream = io.StringIO(file.stream.read().decode("UTF-8"), newline=None)
        csv_reader = csv.DictReader(stream)
        
        # Normalize headers
        headers = [h.lower() for h in csv_reader.fieldnames]
        if 'email' not in headers:
            flash("CSV must have an 'email' column.", "danger")
            return redirect(url_for('main.view_campaign', campaign_id=campaign_id))
            
        count = 0
        suppressed_count = 0
        
        for row in csv_reader:
            # Case insensitive key lookup
            row_lower = {k.lower(): v for k, v in row.items()}
            email = row_lower.get('email', '').strip().lower()
            
            if not email: continue
            
            # Check Suppression
            if Suppression.query.filter_by(email=email).first():
                suppressed_count += 1
                continue
                
            # Check Duplicate in Campaign
            if Recipient.query.filter_by(campaign_id=campaign.id, email=email).first():
                continue

            # Store extra data as JSON for personalization
            recipient = Recipient(
                email=email,
                campaign_id=campaign.id,
                data=json.dumps(row_lower)
            )
            db.session.add(recipient)
            count += 1
            
        db.session.commit()
        flash(f'Imported {count} recipients. {suppressed_count} skipped (suppressed).', 'success')
    except Exception as e:
        flash(f'Error importing CSV: {str(e)}', 'danger')
        
    return redirect(url_for('main.view_campaign', campaign_id=campaign_id))

@bp.route('/campaign/<int:campaign_id>/add_recipient', methods=['POST'])
@login_required
def add_recipient(campaign_id):
    email = request.form.get('email').strip().lower()
    if not email: return redirect(url_for('main.view_campaign', campaign_id=campaign_id))
    
    if Suppression.query.filter_by(email=email).first():
        flash(f'{email} is in suppression list.', 'warning')
        return redirect(url_for('main.view_campaign', campaign_id=campaign_id))

    recipient = Recipient(email=email, campaign_id=campaign_id, data="{}")
    db.session.add(recipient)
    db.session.commit()
    flash('Recipient added.', 'success')
    return redirect(url_for('main.view_campaign', campaign_id=campaign_id))

@bp.route('/campaign/<int:campaign_id>/clear_recipients', methods=['POST'])
@login_required
def clear_recipients(campaign_id):
    Recipient.query.filter_by(campaign_id=campaign_id).delete()
    db.session.commit()
    flash('All recipients cleared.', 'info')
    return redirect(url_for('main.view_campaign', campaign_id=campaign_id))

@bp.route('/campaign/<int:campaign_id>/validate_list', methods=['POST'])
@login_required
def validate_list(campaign_id):
    # Basic MX validation logic (placeholder for complexity)
    from app.core_logic.deliverability import DeliverabilityHelper
    helper = DeliverabilityHelper()
    
    recipients = Recipient.query.filter_by(campaign_id=campaign_id).all()
    invalid_count = 0
    for r in recipients:
        domain = r.email.split('@')[-1]
        mx_status = helper.check_mx_record(domain)
        if mx_status != "Valid":
            r.status = f"Invalid ({mx_status})"
            invalid_count += 1
    db.session.commit()
    flash(f'Validation complete. Found {invalid_count} issues.', 'info')
    return redirect(url_for('main.view_campaign', campaign_id=campaign_id))

@bp.route('/campaign/<int:campaign_id>/retry_failed', methods=['POST'])
@login_required
def retry_failed(campaign_id):
    failed = Recipient.query.filter_by(campaign_id=campaign_id, status='Failed').all()
    for r in failed:
        r.status = 'Queued'
        r.status_message = None
    db.session.commit()
    flash(f'Queued {len(failed)} failed recipients for retry.', 'success')
    return redirect(url_for('main.view_campaign', campaign_id=campaign_id))

@bp.route('/campaign/<int:campaign_id>/remove_selected', methods=['POST'])
@login_required
def remove_selected(campaign_id):
    ids = request.form.getlist('recipient_ids')
    if ids:
        Recipient.query.filter(Recipient.id.in_(ids), Recipient.campaign_id==campaign_id).delete(synchronize_session=False)
        db.session.commit()
        flash(f'Removed {len(ids)} recipients.', 'success')
    return redirect(url_for('main.view_campaign', campaign_id=campaign_id))

# --- Campaign Control (Start, Stop, Pause) ---

@bp.route('/campaign/<int:campaign_id>/control', methods=['POST'])
@login_required
def control_campaign(campaign_id):
    action = request.form.get('action')
    campaign = Campaign.query.get_or_404(campaign_id)
    
    redis_key_pause = f"campaign_{campaign_id}_pause"
    redis_key_stop = f"campaign_{campaign_id}_stop"

    if action == 'start':
        if campaign.status == 'Running':
            flash('Campaign is already running.', 'warning')
        else:
            send_campaign_task.delay(campaign_id)
            flash('Campaign started.', 'success')
            
    elif action == 'pause':
        redis_client.set(redis_key_pause, "true")
        campaign.status = 'Paused'
        db.session.commit()
        flash('Campaign paused. It will stop after the current batch.', 'info')
        
    elif action == 'resume':
        redis_client.delete(redis_key_pause)
        campaign.status = 'Running'
        db.session.commit()
        flash('Campaign resumed.', 'success')
        
    elif action == 'stop':
        redis_client.set(redis_key_stop, "true")
        redis_client.delete(redis_key_pause) # Ensure not stuck in pause loop
        campaign.status = 'Stopped'
        db.session.commit()
        flash('Campaign stopping...', 'danger')

    return redirect(url_for('main.view_campaign', campaign_id=campaign_id))

@bp.route('/campaign/<int:campaign_id>/export_csv')
@login_required
def export_csv(campaign_id):
    recipients = Recipient.query.filter_by(campaign_id=campaign_id).all()
    
    si = io.StringIO()
    cw = csv.writer(si)
    cw.writerow(['Email', 'Status', 'Sent At', 'Opened', 'Clicked', 'Message'])
    
    for r in recipients:
        cw.writerow([
            r.email, 
            r.status, 
            r.sent_at.strftime('%Y-%m-%d %H:%M:%S') if r.sent_at else '', 
            'Yes' if r.opened_at else 'No', 
            'Yes' if r.clicked_at else 'No',
            r.status_message or ''
        ])
        
    output = make_response(si.getvalue())
    output.headers["Content-Disposition"] = f"attachment; filename=campaign_{campaign_id}_export.csv"
    output.headers["Content-type"] = "text/csv"
    return output

# --- Other Routes ---

@bp.route('/')
@bp.route('/index')
@login_required
def index():
    campaigns = Campaign.query.filter_by(user_id=current_user.id).order_by(Campaign.id.desc())
    return render_template('dashboard.html', title='Dashboard', campaigns=campaigns)

@bp.route('/test_smtp_connection', methods=['POST'])
@login_required
def test_smtp_connection():
    data = request.get_json()
    profile_id = data.get('profile_id')
    
    # Run in background to avoid hanging browser
    task = test_smtp_connection_task.delay(profile_id)
    
    # In a real sync/async web call we might just wait 5s or return task ID
    # For simplicity, we will assume sync wait here or handle via frontend polling
    # But since user specifically asked for "Test Connection" functionality:
    
    # Synchronous Fallback for immediate feedback
    profile = SMTPServer.query.get(profile_id)
    from app.core_logic.smtp_handler import SMTPHandler
    handler = SMTPHandler(profile.to_dict())
    success, msg = handler.test_connection()
    
    return jsonify({'success': success, 'message': msg})

# ... Login/Logout/Register routes remain the same ...
