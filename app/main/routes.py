from flask import render_template, flash, redirect, url_for, request, jsonify, current_app
from flask_login import login_user, logout_user, current_user, login_required
from app import db
from app.main import bp
from app.models import User, Campaign, Recipient
from app.tasks import send_campaign_task
from app.core_logic.ai_handler import AIHandler, LocalAIHandler
from app.core_logic.deliverability import DeliverabilityHelper
import csv
import io
import json
from datetime import datetime

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
    if campaign.author.id != current_user.id:
        flash("You do not have permission to view this campaign.", "danger")
        return redirect(url_for('main.index'))
    
    page = request.args.get('page', 1, type=int)
    recipients = campaign.recipients.order_by(Recipient.id.asc()).paginate(
        page, current_app.config['ITEMS_PER_PAGE'], False)
    
    next_url = url_for('main.view_campaign', campaign_id=campaign.id, page=recipients.next_num) if recipients.has_next else None
    prev_url = url_for('main.view_campaign', campaign_id=campaign.id, page=recipients.prev_num) if recipients.has_prev else None

    return render_template('campaign.html', title=campaign.name, campaign=campaign, recipients=recipients.items, next_url=next_url, prev_url=prev_url)

@bp.route('/campaign/new', methods=['GET', 'POST'])
@login_required
def new_campaign():
    """Page to create a new campaign."""
    if request.method == 'POST':
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
        db.session.flush() # Flush to get campaign ID for recipients
        
        file = request.files['recipients_file']
        if file:
            try:
                stream = io.StringIO(file.stream.read().decode("UTF8"), newline=None)
                csv_reader = csv.DictReader(stream)
                
                if 'email' not in csv_reader.fieldnames:
                    flash('CSV file must have an "email" column.', 'danger')
                    return redirect(request.url)

                for row in csv_reader:
                    recipient_email = row.get('email').strip()
                    if recipient_email:
                        recipient = Recipient(email=recipient_email, campaign_id=campaign.id)
                        # Store other columns as JSON data
                        recipient.set_data({k: v for k, v in row.items() if k != 'email'})
                        db.session.add(recipient)
            except Exception as e:
                db.session.rollback()
                flash(f'Error processing CSV file: {e}', 'danger')
                return redirect(request.url)

        db.session.commit()
        flash('Your campaign has been created!', 'success')
        return redirect(url_for('main.view_campaign', campaign_id=campaign.id))

    return render_template('create_campaign.html', title='New Campaign')

@bp.route('/campaign/<int:campaign_id>/send')
@login_required
def send_campaign(campaign_id):
    """Endpoint to trigger the background task for sending a campaign."""
    campaign = Campaign.query.get_or_404(campaign_id)
    if campaign.author.id != current_user.id:
        flash("You do not have permission to send this campaign.", "danger")
        return redirect(url_for('main.index'))
        
    send_campaign_task.delay(campaign_id)
    flash(f'Campaign "{campaign.name}" is being sent in the background!', 'info')
    return redirect(url_for('main.view_campaign', campaign_id=campaign_id))

# --- Deliverability & AI Tools ---

@bp.route('/tools/deliverability', methods=['GET', 'POST'])
@login_required
def deliverability_tools():
    results = {}
    if request.method == 'POST':
        helper = DeliverabilityHelper()
        if 'check_auth' in request.form:
            domain = request.form.get('domain_ip', '').strip()
            if "@" in domain:
                domain = domain.split('@')[1]
            results['auth'] = helper.check_domain_authentication(domain)
            results['type'] = 'auth'
        elif 'check_blacklist' in request.form:
            target = request.form.get('domain_ip', '').strip()
            results['blacklist'] = helper.check_blacklist(target)
            results['type'] = 'blacklist'
        elif 'ai_spam_check' in request.form:
            # This is an API-style interaction
            subject = request.form.get('subject')
            body = request.form.get('body')
            provider = request.form.get('provider') # 'openai' or 'local'

            if provider == 'openai':
                handler = AIHandler(api_key=current_app.config['OPENAI_API_KEY'])
            else:
                handler = LocalAIHandler(api_url=current_app.config['LOCAL_AI_URL'], model=current_app.config['LOCAL_AI_MODEL'])
            
            prompt = (f"Analyze the following email for spam triggers, awkward phrasing, or phishing indicators. "
                      f"Provide a spam score from 1 to 10 (1 is best), a one-sentence summary of the risk, "
                      f"and a bulleted list of concrete suggestions for improvement. Format your response clearly as plain text.\n\n"
                      f"SUBJECT: {subject}\n\nBODY:\n{body}")
            
            success, result_text = handler.generate(prompt, system_msg="You are an expert email deliverability analyst.")
            return jsonify({'success': success, 'result': result_text})

    return render_template('deliverability.html', title='Deliverability Tools', results=results)


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
        if User.query.filter_by(username=request.form['username']).first():
            flash('Username already taken. Please choose a different one.', 'warning')
            return redirect(url_for('main.register'))
        if User.query.filter_by(email=request.form['email']).first():
            flash('Email address already registered.', 'warning')
            return redirect(url_for('main.register'))
            
        user = User(username=request.form['username'], email=request.form['email'])
        user.set_password(request.form['password'])
        db.session.add(user)
        db.session.commit()
        flash('Congratulations, you are now a registered user! Please log in.', 'success')
        return redirect(url_for('main.login'))
    return render_template('register.html', title='Register')
