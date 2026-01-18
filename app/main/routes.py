from flask import render_template, flash, redirect, url_for, request, current_app
from flask_login import login_user, logout_user, current_user, login_required
from app import db
from app.main import bp
from app.models import User, Campaign, Recipient, Suppression
from app.tasks import send_campaign_task
import csv
import io
from datetime import datetime

# --- Tracking Routes ---

@bp.route('/track/open/<token>')
def track_open(token):
    data = Recipient.verify_tracking_token(token)
    if data and data.get('action') == 'open':
        recipient = Recipient.query.get(data['recipient_id'])
        if recipient and not recipient.opened_at:
            recipient.opened_at = datetime.utcnow()
            db.session.commit()
    
    # Return a 1x1 transparent pixel
    from flask import make_response
    import base64
    pixel_data = base64.b64decode('R0lGODlhAQABAIAAAP///wAAACH5BAEAAAAALAAAAAABAAEAAAICRAEAOw==')
    response = make_response(pixel_data)
    response.headers['Content-Type'] = 'image/gif'
    return response

@bp.route('/track/click/<token>')
def track_click(token):
    data = Recipient.verify_tracking_token(token)
    if data and data.get('action') == 'click' and 'url' in data:
        recipient = Recipient.query.get(data['recipient_id'])
        if recipient:
            if not recipient.clicked_at: # Record only the first click time
                recipient.clicked_at = datetime.utcnow()
            if not recipient.opened_at: # If they click, they also opened
                recipient.opened_at = datetime.utcnow()
            db.session.commit()
        return redirect(data['url'])
    return redirect(url_for('main.index'))

@bp.route('/unsubscribe/<token>')
def unsubscribe(token):
    data = Recipient.verify_tracking_token(token)
    if data and data.get('action') == 'unsubscribe':
        recipient = Recipient.query.get(data['recipient_id'])
        if recipient:
            # Add to suppression list
            if not Suppression.query.filter_by(email=recipient.email).first():
                supp = Suppression(email=recipient.email, reason='unsubscribe')
                db.session.add(supp)
            
            # Update recipient status
            recipient.status = 'Unsubscribed'
            db.session.commit()
            flash(f"{recipient.email} has been unsubscribed.", "success")
            return render_template('message.html', title="Unsubscribed", message_title="Successfully Unsubscribed", message_body="Your email address has been removed from this mailing list.")
    
    flash("Invalid unsubscribe link.", "danger")
    return render_template('message.html', title="Error", message_title="Invalid Link", message_body="The unsubscribe link is invalid or has expired.")

# --- Main Application Routes ---

@bp.route('/')
@bp.route('/index')
@login_required
def index():
    """Dashboard page showing all campaigns for the logged-in user."""
    campaigns = Campaign.query.filter_by(user_id=current_user.id).order_by(Campaign.created_at.desc()).all()
    return render_template('dashboard.html', title='Dashboard', campaigns=campaigns)

@bp.route('/campaign/<int:campaign_id>')
@login_required
def view_campaign(campaign_id):
    """Page to view a specific campaign and its recipients."""
    campaign = Campaign.query.get_or_404(campaign_id)
    if campaign.author != current_user:
        return redirect(url_for('main.index'))
    
    page = request.args.get('page', 1, type=int)
    recipients_paginated = campaign.recipients.order_by(Recipient.id.asc()).paginate(
        page=page, per_page=current_app.config['ITEMS_PER_PAGE'], error_out=False
    )
    return render_template('campaign.html', title=campaign.name, campaign=campaign, recipients=recipients_paginated)

@bp.route('/campaign/new', methods=['GET', 'POST'])
@login_required
def new_campaign():
    """Page to create a new campaign."""
    if request.method == 'POST':
        campaign = Campaign(
            name=request.form['campaign_name'], subject=request.form['subject'],
            body_html=request.form['body_html'], smtp_server=request.form['smtp_server'],
            smtp_port=int(request.form['smtp_port']), smtp_username=request.form['smtp_username'],
            smtp_password=request.form['smtp_password'], smtp_sender_name=request.form['smtp_sender_name'],
            smtp_sender_email=request.form['smtp_sender_email'], author=current_user
        )
        db.session.add(campaign)
        
        file = request.files['recipients_file']
        if file:
            try:
                stream = io.StringIO(file.stream.read().decode("UTF8"), newline=None)
                csv_reader = csv.DictReader(stream)
                
                if 'email' not in csv_reader.fieldnames:
                    flash("CSV file must have an 'email' column.", 'danger')
                    return redirect(url_for('main.new_campaign'))

                for row in csv_reader:
                    email = row.get('email', '').strip().lower()
                    if email and not Suppression.query.filter_by(email=email).first():
                        recipient = Recipient(
                            email=email,
                            campaign=campaign,
                            data=json.dumps({k: v for k, v in row.items() if k != 'email'})
                        )
                        db.session.add(recipient)
                db.session.commit()
                flash('Your campaign has been created!', 'success')
                return redirect(url_for('main.view_campaign', campaign_id=campaign.id))
            except Exception as e:
                db.session.rollback()
                flash(f"Error processing file: {e}", 'danger')
                return redirect(url_for('main.new_campaign'))
    
    return render_template('create_campaign.html', title='New Campaign')

@bp.route('/campaign/<int:campaign_id>/send')
@login_required
def send_campaign(campaign_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    if campaign.author != current_user:
        return redirect(url_for('main.index'))
    
    result = send_campaign_task.delay(campaign_id)
    flash(f'Your campaign is being sent in the background! Task ID: {result.id}', 'info')
    return redirect(url_for('main.view_campaign', campaign_id=campaign.id))

# --- Auth Routes ---

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
        flash('Congratulations, you are now a registered user!', 'success')
        return redirect(url_for('main.login'))
    return render_template('register.html', title='Register')
