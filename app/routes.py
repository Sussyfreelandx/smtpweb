from flask import render_template, flash, redirect, url_for, request, current_app
from flask_login import login_user, logout_user, current_user, login_required
from app import db
from app.routes import bp
from app.models import User, Campaign, Recipient
from app.tasks import send_campaign_task
import csv
import io
import json

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
    campaign = db.session.get(Campaign, campaign_id)
    if not campaign or campaign.user_id != current_user.id:
        flash('Campaign not found or you do not have permission to view it.')
        return redirect(url_for('main.index'))
        
    page = request.args.get('page', 1, type=int)
    recipients = Recipient.query.filter_by(campaign_id=campaign.id).paginate(
        page=page, per_page=current_app.config['ITEMS_PER_PAGE'], error_out=False
    )
    return render_template('campaign.html', title=campaign.name, campaign=campaign, recipients=recipients)

@bp.route('/campaign/new', methods=['GET', 'POST'])
@login_required
def new_campaign():
    """Page to create a new campaign."""
    if request.method == 'POST':
        try:
            # Create a new campaign from the form data
            campaign = Campaign(
                name=request.form['campaign_name'],
                subject=request.form['subject'],
                body_html=request.form['body_html'],
                smtp_server=request.form['smtp_server'],
                smtp_port=int(request.form['smtp_port']),
                smtp_username=request.form['smtp_username'],
                smtp_password=request.form['smtp_password'], # TODO: Encrypt this
                smtp_sender_name=request.form['smtp_sender_name'],
                smtp_sender_email=request.form['smtp_sender_email'],
                author=current_user
            )
            db.session.add(campaign)
            
            # Process uploaded recipient file
            file = request.files['recipients_file']
            if file and file.filename != '':
                stream = io.StringIO(file.stream.read().decode("UTF-8"), newline=None)
                csv_reader = csv.DictReader(stream)
                
                if 'email' not in csv_reader.fieldnames:
                    flash('CSV file must have an "email" column header.', 'danger')
                    return redirect(url_for('main.new_campaign'))

                for row in csv_reader:
                    recipient_email = row.get('email').strip().lower()
                    if recipient_email:
                        recipient = Recipient(
                            email=recipient_email, 
                            campaign=campaign,
                            data=json.dumps(row) # Store the whole row as JSON
                        )
                        db.session.add(recipient)
            
            db.session.commit()
            flash(f'Campaign "{campaign.name}" has been created!', 'success')
            return redirect(url_for('main.view_campaign', campaign_id=campaign.id))
        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f"Error creating campaign: {e}")
            flash(f'An error occurred while creating the campaign: {e}', 'danger')

    return render_template('create_campaign.html', title='New Campaign')

@bp.route('/campaign/<int:campaign_id>/send')
@login_required
def send_campaign_route(campaign_id):
    campaign = db.session.get(Campaign, campaign_id)
    if not campaign or campaign.user_id != current_user.id:
        flash('Campaign not found or you do not have permission to send it.', 'danger')
        return redirect(url_for('main.index'))
        
    # This is non-blocking. It starts the background task and returns immediately.
    send_campaign_task.delay(campaign_id)
    flash(f'Your campaign "{campaign.name}" is being sent in the background!', 'info')
    return redirect(url_for('main.view_campaign', campaign_id=campaign_id))


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
    return redirect(url_for('main.index'))

@bp.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('main.index'))
    if request.method == 'POST':
        user = User.query.filter_by(username=request.form['username']).first()
        if user:
            flash('Username already taken. Please choose a different one.', 'warning')
            return redirect(url_for('main.register'))
        
        user = User(username=request.form['username'], email=request.form['email'])
        user.set_password(request.form['password'])
        db.session.add(user)
        db.session.commit()
        flash('Congratulations, you are now a registered user!', 'success')
        return redirect(url_for('main.login'))
    return render_template('register.html', title='Register')