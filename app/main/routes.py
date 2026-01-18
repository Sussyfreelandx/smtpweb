from flask import render_template, flash, redirect, url_for, request
from flask_login import login_user, logout_user, current_user, login_required
from app import db
from app.main import bp
from app.models import User, Campaign, Recipient
from app.tasks import send_campaign_task
import csv
import io

# --- Main Routes ---

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
    # TODO: Add pagination for recipients
    recipients = campaign.recipients.order_by(Recipient.id.asc()).all()
    return render_template('campaign.html', title=campaign.name, campaign=campaign, recipients=recipients)

@bp.route('/campaign/new', methods=['GET', 'POST'])
@login_required
def new_campaign():
    """Page to create a new campaign."""
    if request.method == 'POST':
        # Create a new campaign from the form data
        campaign = Campaign(
            name=request.form['campaign_name'],
            subject=request.form['subject'],
            body_html=request.form['body_html'],
            smtp_server=request.form['smtp_server'],
            smtp_port=int(request.form['smtp_port']),
            smtp_username=request.form['smtp_username'],
            smtp_password=request.form['smtp_password'], # Handle secrets securely!
            smtp_sender_name=request.form['smtp_sender_name'],
            smtp_sender_email=request.form['smtp_sender_email'],
            author=current_user
        )
        db.session.add(campaign)
        
        # Process uploaded recipient file
        file = request.files['recipients_file']
        if file:
            stream = io.StringIO(file.stream.read().decode("UTF8"), newline=None)
            csv_reader = csv.reader(stream)
            headers = next(csv_reader) # Get header row
            email_index = headers.index('email')

            for row in csv_reader:
                recipient_email = row[email_index]
                # TODO: add validation
                recipient = Recipient(email=recipient_email, campaign=campaign)
                db.session.add(recipient)
        
        db.session.commit()
        flash('Your campaign has been created!')
        return redirect(url_for('main.view_campaign', campaign_id=campaign.id))

    return render_template('create_campaign.html', title='New Campaign')

@bp.route('/campaign/<int:campaign_id>/send')
@login_required
def send_campaign(campaign_id):
    # This is non-blocking. It starts the background task and returns immediately.
    send_campaign_task.delay(campaign_id)
    flash('Your campaign is being sent in the background!')
    return redirect(url_for('main.view_campaign', campaign_id=campaign_id))


# --- Authentication Routes ---

@bp.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('main.index'))
    if request.method == 'POST':
        user = User.query.filter_by(username=request.form['username']).first()
        if user is None or not user.check_password(request.form['password']):
            flash('Invalid username or password')
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
        flash('Congratulations, you are now a registered user!')
        return redirect(url_for('main.login'))
    return render_template('register.html', title='Register')