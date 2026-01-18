from flask import render_template, flash, redirect, url_for, request, jsonify, Blueprint
from flask_login import login_user, logout_user, current_user, login_required
from app import db
from app.models import User, Campaign, Recipient
from app.tasks import send_campaign_task
import csv
import io
import json

# --- DEFINE BLUEPRINT HERE ---
bp = Blueprint('main', __name__)

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
    recipients = campaign.recipients.order_by(Recipient.id.asc()).all()
    return render_template('campaign.html', title=campaign.name, campaign=campaign, recipients=recipients)

@bp.route('/campaign/new', methods=['GET', 'POST'])
@login_required
def new_campaign():
    if request.method == 'POST':
        campaign = Campaign(
            name=request.form['campaign_name'],
            subject=request.form['subject'],
            body=request.form['body_html'], # Note: Model uses 'body', form likely sends 'body_html'
            smtp_profile_id=1, # simplified for now, ensuring model compatibility
            user_id=current_user.id
        )
        # Note: In a real scenario, you'd handle the SMTP profile selection properly here
        
        db.session.add(campaign)
        db.session.flush()
        
        file = request.files.get('recipients_file')
        if file:
            try:
                stream = io.StringIO(file.stream.read().decode("UTF-8"), newline=None)
                csv_reader = csv.DictReader(stream)
                for row in csv_reader:
                    if 'email' in row:
                        recipient = Recipient(email=row['email'], campaign_id=campaign.id)
                        db.session.add(recipient)
            except Exception as e:
                flash(f'Error reading CSV: {e}', 'danger')
        
        db.session.commit()
        flash('Campaign created!', 'success')
        return redirect(url_for('main.view_campaign', campaign_id=campaign.id))
    return render_template('create_campaign.html', title='New Campaign')

@bp.route('/campaign/<int:campaign_id>/send')
@login_required
def send_campaign(campaign_id):
    send_campaign_task.delay(campaign_id)
    flash('Campaign sending started.', 'success')
    return redirect(url_for('main.view_campaign', campaign_id=campaign_id))

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
