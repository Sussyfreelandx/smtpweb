from flask import render_template, flash, redirect, url_for, request, jsonify, Blueprint
from flask_login import login_user, logout_user, current_user, login_required
from werkzeug.security import generate_password_hash
from app import db
from app.models import User, Campaign, Recipient
from app.tasks import send_campaign_task
from core_logic.ai_handler import AIHandler, LocalAIHandler
from core_logic.deliverability import DeliverabilityHelper
import csv
import io
import json

# --- THIS IS THE FIX ---
# Create the Blueprint object here, in the same file as the routes.
bp = Blueprint('main', __name__)

# --- Main Dashboard and Campaign Routes ---

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
    """Page to create a new campaign, including SMTP settings and recipient upload."""
    if request.method == 'POST':
        # Create a new campaign from the form data
        campaign = Campaign(
            name=request.form['campaign_name'],
            subject=request.form['subject'],
            body_html=request.form['body_html'],
            smtp_server=request.form['smtp_server'],
            smtp_port=int(request.form['smtp_port']),
            smtp_username=request.form['smtp_username'],
            smtp_password=request.form['smtp_password'], # Handle secrets securely in production!
            smtp_sender_name=request.form['smtp_sender_name'],
            smtp_sender_email=request.form['smtp_sender_email'],
            author=current_user
        )
        db.session.add(campaign)
        db.session.flush() # Flush to get the campaign ID for recipients
        
        # Process uploaded recipient file
        file = request.files['recipients_file']
        if file:
            try:
                stream = io.StringIO(file.stream.read().decode("UTF-8"), newline=None)
                csv_reader = csv.reader(stream)
                headers = [h.strip().lower() for h in next(csv_reader)]
                
                if 'email' not in headers:
                    flash('CSV file must have an "email" column.', 'danger')
                    return redirect(url_for('main.new_campaign'))

                for row_data in csv.DictReader(io.StringIO(file.stream.read().decode("UTF-8"))):
                    recipient_email = row_data.get('email', '').strip()
                    if recipient_email:
                        # Store all other columns as a JSON string in the 'data' field
                        personal_data = {k: v for k, v in row_data.items() if k != 'email'}
                        recipient = Recipient(
                            email=recipient_email, 
                            campaign_id=campaign.id,
                            data=json.dumps(personal_data)
                        )
                        db.session.add(recipient)

            except Exception as e:
                db.session.rollback()
                flash(f'Error processing CSV file: {e}', 'danger')
                return redirect(url_for('main.new_campaign'))
        
        db.session.commit()
        flash('Your campaign has been created!', 'success')
        return redirect(url_for('main.view_campaign', campaign_id=campaign.id))

    return render_template('create_campaign.html', title='New Campaign')

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
    flash('Your campaign is being sent in the background! Statuses will update automatically.', 'info')
    return redirect(url_for('main.view_campaign', campaign_id=campaign.id))

# --- Tracking Routes ---

@bp.route('/track/open/<int:recipient_id>')
def track_open(recipient_id):
    # This is where the logic from the desktop app's TrackingServer is implemented
    # It finds the recipient, updates their status, and returns a 1x1 pixel image.
    pass # TODO: Implement open tracking logic

@bp.route('/track/click/<int:recipient_id>')
def track_click(recipient_id):
    # This tracks the click and redirects the user to the final destination.
    pass # TODO: Implement click tracking logic

@bp.route('/unsubscribe/<int:recipient_id>')
def unsubscribe(recipient_id):
    # Marks a recipient as unsubscribed.
    pass # TODO: Implement unsubscribe logic

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
        next_page = request.args.get('next')
        return redirect(next_page or url_for('main.index'))
    return render_template('login.html', title='Sign In')

@bp.route('/logout')
def logout():
    logout_user()
    flash('You have been logged out.', 'success')
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
