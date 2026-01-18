from flask import render_template, flash, redirect, url_for, request, jsonify, Blueprint, make_response, current_app
from flask_login import login_user, logout_user, current_user, login_required
from app import db
from app.models import User, Campaign, Recipient, SMTPServer
from app.core_logic.smtp_handler import SMTPHandler
import csv
import io
import json
import base64
from datetime import datetime

# Define the blueprint
bp = Blueprint('main', __name__)

# --- Helper Functions ---

def get_smtp_handler(smtp_profile):
    """Helper to initialize SMTPHandler from a profile model."""
    if not smtp_profile:
        return None
    return SMTPHandler(smtp_profile.to_dict())

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
    recipients = campaign.recipients.order_by(Recipient.id.asc()).paginate(page=page, per_page=50, error_out=False)
    
    return render_template('campaign.html', title=campaign.name, campaign=campaign, recipients=recipients)

@bp.route('/campaign/new', methods=['GET', 'POST'])
@login_required
def new_campaign():
    # Fetch user's SMTP profiles to populate the dropdown (if you add one to the UI)
    smtp_profiles = SMTPServer.query.filter_by(user_id=current_user.id).all()
    
    if request.method == 'POST':
        # 1. Create or Get SMTP Profile
        # Ideally, user selects an existing profile, but here we support creating one on the fly based on your form
        smtp_profile = SMTPServer(
            profile_name=f"Campaign-{request.form['campaign_name']}-Profile", # Temporary name
            server=request.form['smtp_server'],
            port=int(request.form['smtp_port']),
            username=request.form['smtp_username'],
            sender_name=request.form['smtp_sender_name'],
            sender_email=request.form['smtp_sender_email'],
            user_id=current_user.id
        )
        smtp_profile.set_password(request.form['smtp_password'])
        db.session.add(smtp_profile)
        db.session.flush() # Flush to get the ID

        # 2. Create Campaign
        campaign = Campaign(
            name=request.form['campaign_name'],
            subject=request.form['subject'],
            body=request.form['body_html'],
            smtp_profile_id=smtp_profile.id,
            user_id=current_user.id,
            # Add fields for threading/throttling if your model supports them
            # parallel_workers = int(request.form.get('parallel_workers', 1))
        )
        db.session.add(campaign)
        db.session.flush()
        
        # 3. Handle CSV Upload
        file = request.files.get('recipients_file')
        if file:
            try:
                stream = io.StringIO(file.stream.read().decode("UTF-8"), newline=None)
                csv_reader = csv.DictReader(stream)
                
                recipients_to_add = []
                for row in csv_reader:
                    # Clean up keys (lowercase)
                    row = {k.lower().strip(): v.strip() for k, v in row.items()}
                    
                    if 'email' in row:
                        email = row['email']
                        # Store the rest of the row as JSON for personalization (Autograb)
                        # Remove email from data to save space
                        del row['email'] 
                        
                        recipient = Recipient(
                            email=email, 
                            campaign_id=campaign.id,
                            # Store extra CSV data for [firstname], [company] etc.
                            # The Recipient model needs a 'data' column (Text or JSON type)
                            # Assuming your model has it, or we rely on autograb in tasks.py logic
                        )
                        # If Recipient model has a 'data' field:
                        # recipient.data = json.dumps(row) 
                        
                        recipients_to_add.append(recipient)
                
                db.session.add_all(recipients_to_add)
                
            except Exception as e:
                flash(f'Error reading CSV: {e}', 'danger')
                return redirect(url_for('main.new_campaign'))
        
        db.session.commit()
        flash('Campaign created successfully!', 'success')
        return redirect(url_for('main.view_campaign', campaign_id=campaign.id))
        
    return render_template('create_campaign.html', title='New Campaign')

@bp.route('/campaign/<int:campaign_id>/send')
@login_required
def send_campaign(campaign_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    if campaign.author != current_user:
        return redirect(url_for('main.index'))

    # Import task here to avoid circular dependency
    from app.tasks import send_campaign_task
    
    # Trigger Celery Task
    send_campaign_task.delay(campaign_id)
    
    flash('Campaign sending started in the background.', 'success')
    return redirect(url_for('main.view_campaign', campaign_id=campaign_id))

@bp.route('/campaign/<int:campaign_id>/pause')
@login_required
def pause_campaign(campaign_id):
    """Sets status of queued recipients to 'Paused'."""
    campaign = Campaign.query.get_or_404(campaign_id)
    if campaign.author != current_user:
        return redirect(url_for('main.index'))
    
    # We pause by updating the status of 'Queued' items to 'Paused'
    # The worker needs to check for this status
    Recipient.query.filter_by(campaign_id=campaign.id, status='Queued').update({'status': 'Paused'})
    db.session.commit()
    
    flash('Campaign paused.', 'warning')
    return redirect(url_for('main.view_campaign', campaign_id=campaign_id))

@bp.route('/campaign/<int:campaign_id>/resume')
@login_required
def resume_campaign(campaign_id):
    """Sets status of 'Paused' recipients back to 'Queued' and triggers task."""
    campaign = Campaign.query.get_or_404(campaign_id)
    if campaign.author != current_user:
        return redirect(url_for('main.index'))
    
    Recipient.query.filter_by(campaign_id=campaign.id, status='Paused').update({'status': 'Queued'})
    db.session.commit()
    
    # Re-trigger the task to pick up queued items
    from app.tasks import send_campaign_task
    send_campaign_task.delay(campaign_id)
    
    flash('Campaign resumed.', 'success')
    return redirect(url_for('main.view_campaign', campaign_id=campaign_id))

@bp.route('/campaign/<int:campaign_id>/stop')
@login_required
def stop_campaign(campaign_id):
    """Permanently stops a campaign."""
    campaign = Campaign.query.get_or_404(campaign_id)
    Recipient.query.filter_by(campaign_id=campaign.id, status='Queued').update({'status': 'Stopped'})
    Recipient.query.filter_by(campaign_id=campaign.id, status='Paused').update({'status': 'Stopped'})
    db.session.commit()
    flash('Campaign stopped.', 'danger')
    return redirect(url_for('main.view_campaign', campaign_id=campaign_id))

@bp.route('/campaign/<int:campaign_id>/retry')
@login_required
def retry_failed(campaign_id):
    """Resets 'Failed' recipients to 'Queued'."""
    campaign = Campaign.query.get_or_404(campaign_id)
    count = Recipient.query.filter_by(campaign_id=campaign.id, status='Failed').update({'status': 'Queued'})
    db.session.commit()
    
    if count > 0:
        from app.tasks import send_campaign_task
        send_campaign_task.delay(campaign_id)
        flash(f'Retrying {count} failed emails.', 'info')
    else:
        flash('No failed emails to retry.', 'info')
        
    return redirect(url_for('main.view_campaign', campaign_id=campaign_id))

@bp.route('/campaign/<int:campaign_id>/export')
@login_required
def export_campaign_data(campaign_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    
    def generate_csv():
        data = io.StringIO()
        w = csv.writer(data)
        
        # Write Header
        w.writerow(('Email', 'Status', 'Sent At', 'Opened At', 'Clicked At', 'Status Message'))
        yield data.getvalue()
        data.seek(0)
        data.truncate(0)
        
        # Write Rows
        for r in campaign.recipients.all():
            w.writerow((
                r.email, 
                r.status, 
                r.sent_at, 
                r.opened_at if hasattr(r, 'opened_at') else '', 
                r.clicked_at if hasattr(r, 'clicked_at') else '', 
                r.status_message
            ))
            yield data.getvalue()
            data.seek(0)
            data.truncate(0)

    response = make_response(generate_csv())
    response.headers['Content-Disposition'] = f'attachment; filename=campaign_{campaign_id}_report.csv'
    response.headers['Content-Type'] = 'text/csv'
    return response

@bp.route('/test_smtp_connection', methods=['POST'])
@login_required
def test_smtp_connection():
    """Endpoint for the 'Test SMTP' button."""
    data = request.get_json()
    profile_id = data.get('profile_id')
    
    # If testing from Create Campaign form (raw credentials)
    if not profile_id and 'server' in data:
        temp_config = {
            'server': data.get('server'),
            'port': data.get('port'),
            'username': data.get('username'),
            'password': data.get('password'),
            'use_tls': True, # Default assumption for test
            'sender_email': data.get('sender_email')
        }
        handler = SMTPHandler(temp_config)
    else:
        # If testing an existing profile
        profile = SMTPServer.query.get_or_404(profile_id)
        if profile.user_id != current_user.id:
            return jsonify({'success': False, 'message': 'Unauthorized'}), 403
        handler = SMTPHandler(profile.to_dict())

    # Perform connection test (SMTPHandler needs a test_connection method)
    # If SMTPHandler doesn't have one, we use send_email_sync to self
    try:
        # We try to send a test email to the sender themselves
        success, msg = handler.send_email_sync(
            to_email=handler.sender_email, 
            subject="Paris Sender - SMTP Test", 
            html_content="<p>Your SMTP configuration is working correctly.</p>"
        )
        if success:
            return jsonify({'success': True, 'message': 'Connection Successful! Test email sent.'})
        else:
            return jsonify({'success': False, 'message': f'Connection Failed: {msg}'})
    except Exception as e:
        return jsonify({'success': False, 'message': f'Error: {str(e)}'})

# --- Standard Auth & Utility Routes ---

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

# --- Tracking Routes (Placeholders/Implementations) ---
# These are often in __init__.py but defined here to ensure no 404s if blueprints are mixed up

@bp.route('/track/open/<token>')
def track_open(token):
    # Logic handled in core_logic blueprint usually, but ensuring safety
    return "Pixel", 200

@bp.route('/track/click/<token>')
def track_click(token):
    return redirect("http://google.com") # Default fallback

@bp.route('/unsubscribe/<token>')
def unsubscribe(token):
    return "Unsubscribed", 200

# --- SMTP Profile Management Routes (for smtp_profiles.html) ---

@bp.route('/smtp_profiles', methods=['GET', 'POST'])
@login_required
def smtp_profiles():
    if request.method == 'POST':
        profile_id = request.form.get('profile_id')
        if profile_id:
            # Edit existing
            profile = SMTPServer.query.get_or_404(profile_id)
            if profile.user_id != current_user.id: abort(403)
            profile.profile_name = request.form['name']
            profile.server = request.form['server']
            profile.port = int(request.form['port'])
            profile.username = request.form['username']
            if request.form.get('password'):
                profile.set_password(request.form['password'])
            profile.sender_name = request.form['sender_name']
            profile.sender_email = request.form['sender_email']
            profile.use_tls = 'use_tls' in request.form
            profile.use_ssl = 'use_ssl' in request.form
        else:
            # Create new
            profile = SMTPServer(
                profile_name=request.form['name'],
                server=request.form['server'],
                port=int(request.form['port']),
                username=request.form['username'],
                sender_name=request.form['sender_name'],
                sender_email=request.form['sender_email'],
                use_tls='use_tls' in request.form,
                use_ssl='use_ssl' in request.form,
                user_id=current_user.id
            )
            profile.set_password(request.form['password'])
            db.session.add(profile)
        
        db.session.commit()
        flash('SMTP Profile saved.', 'success')
        return redirect(url_for('main.smtp_profiles'))

    profiles = SMTPServer.query.filter_by(user_id=current_user.id).all()
    return render_template('smtp_profiles.html', title='SMTP Profiles', profiles=profiles)

@bp.route('/smtp_profiles/delete/<int:profile_id>', methods=['POST'])
@login_required
def delete_smtp_profile(profile_id):
    profile = SMTPServer.query.get_or_404(profile_id)
    if profile.user_id != current_user.id:
        flash('Unauthorized', 'danger')
        return redirect(url_for('main.smtp_profiles'))
    
    db.session.delete(profile)
    db.session.commit()
    flash('Profile deleted.', 'info')
    return redirect(url_for('main.smtp_profiles'))

# --- Suppression List Routes ---

@bp.route('/suppression', methods=['GET', 'POST'])
@login_required
def suppression_list():
    # Basic implementation for the suppression list page
    # Requires a form object, assuming simpler implementation here
    if request.method == 'POST':
        email = request.form.get('email')
        reason = request.form.get('reason', 'Manual Add')
        if email:
            from app.models import Suppression
            s = Suppression(email=email, reason=reason)
            db.session.add(s)
            try:
                db.session.commit()
                flash('Email suppressed.', 'success')
            except:
                db.session.rollback()
                flash('Email already suppressed.', 'warning')
                
    page = request.args.get('page', 1, type=int)
    # Import Suppression here to avoid top-level circle if not in models import
    from app.models import Suppression 
    pagination = Suppression.query.paginate(page=page, per_page=50)
    
    # Mocking form for template to avoid crash if form class missing
    class MockForm:
        hidden_tag = lambda: ""
        email = type('obj', (object,), {'label': type('l', (object,), {'__call__': lambda: "Email"}), '__call__': lambda **k: f"<input name='email' class='{k.get('class')}'>"})
        reason = type('obj', (object,), {'label': type('l', (object,), {'__call__': lambda: "Reason"}), '__call__': lambda **k: f"<input name='reason' class='{k.get('class')}'>"})
        submit = type('obj', (object,), {'__call__': lambda **k: f"<button type='submit' class='{k.get('class')}'>Add</button>"})
    
    return render_template('suppression.html', title='Suppression List', pagination=pagination, form=MockForm())

@bp.route('/suppression/delete/<int:suppressed_id>', methods=['POST'])
@login_required
def delete_suppressed_email(suppressed_id):
    from app.models import Suppression
    item = Suppression.query.get_or_404(suppressed_id)
    db.session.delete(item)
    db.session.commit()
    flash('Removed from suppression list.', 'info')
    return redirect(url_for('main.suppression_list'))

# --- AJAX Tool Routes (Deliverability) ---

@bp.route('/tools/deliverability/ajax', methods=['POST'])
@login_required
def deliverability_tools_ajax():
    data = request.get_json()
    subject = data.get('subject')
    body = data.get('body')
    provider = data.get('provider')
    
    from app.core_logic.deliverability import DeliverabilityHelper
    helper = DeliverabilityHelper()
    
    success, result = helper.analyze_spam_ai(subject, body, provider_type=provider)
    
    return jsonify({'success': success, 'result': result})
