import re
import random
from datetime import datetime
from jinja2 import Environment, exceptions as jinja_exceptions

# These sets are from the original script for company name detection
COMMON_ISP_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "aol.com", "outlook.com",
    "msn.com", "live.com", "icloud.com", "mail.com", "comcast.net",
    "verizon.net", "att.net", "sbcglobal.net", "cox.net", "yandex.com",
    "protonmail.com", "zoho.com", "gmx.com", "fastmail.com", "hey.com"
}
GENERIC_EMAIL_WORDS = {
    'info', 'contact', 'admin', 'support', 'sales', 'mail', 'email', 'hello', 
    'test', 'demo', 'user', 'customer', 'press', 'jobs', 'careers', 'service', 
    'team', 'office', 'billing', 'accounts', 'dev', 'webmaster', 'media', 
    'noreply', 'no-reply', 'marketing', 'newsletter', 'updates', 'general', 
    'enquiry', 'inquiries', 'hr', 'recruitment'
}

jinja_env = Environment()

def spin(text):
    """
    Recursively processes spintax `{a|b|c}` in a string.
    """
    pattern = re.compile(r'{([^{}]*)}')
    match = pattern.search(text)
    if not match:
        return text
    
    options = match.group(1).split('|')
    chosen_option = random.choice(options)
    
    # Replace only the first occurrence and recurse
    new_text = text[:match.start()] + chosen_option + text[match.end():]
    return spin(new_text)

def build_personalization_context(email, recipient_data):
    """
    Builds the complete context for personalizing an email, including autograbbed data.
    This function is a direct adaptation of the logic in `_personalize_content`.
    """
    context = {k.lower(): v for k, v in recipient_data.items()}

    # 1. Autograb Firstname if not present in CSV data
    if 'firstname' not in context or not context['firstname']:
        local_part = email.split('@')[0]
        potential_parts = re.split(r'[._\-+]+', local_part)
        valid_parts = [p for p in potential_parts if len(p) > 1 and p.isalpha()]
        if valid_parts:
            candidate = valid_parts[0]
            if candidate.lower() not in GENERIC_EMAIL_WORDS:
                context['firstname'] = candidate.capitalize()

    # 2. Autograb Company if not present
    if 'company' not in context or not context['company']:
        try:
            domain = email.split('@')[1].lower()
            if domain in COMMON_ISP_DOMAINS:
                context['company'] = "your company" # Fallback for common ISPs
            else:
                parts = domain.split('.')
                company_part = parts[-2] if len(parts) > 2 and len(parts[-2]) > 2 and parts[-2] not in ('co', 'com', 'org', 'net', 'ac', 'gov', 'edu') else parts[0]
                context['company'] = '-'.join([p.capitalize() for p in company_part.split('-')])
        except Exception:
            pass # Keep company blank if logic fails

    # 3. Add dynamic autograb placeholders
    now = datetime.utcnow()
    hour = now.hour
    if 5 <= hour < 12: base_greeting = "Good morning"
    elif 12 <= hour < 18: base_greeting = "Good afternoon"
    else: base_greeting = "Good evening"

    # Combine greeting with firstname if available
    if context.get('firstname'):
        context['greetings'] = f"{base_greeting} {context['firstname']}"
    else:
        context['greetings'] = base_greeting

    # Set fallbacks for templates
    context.setdefault('firstname', 'there')
    context.setdefault('company', 'your company')

    # Other dynamic fields
    context['currentdate'] = now.strftime("%B %d, %Y")
    context['time'] = now.strftime("%I:%M %p")
    
    return context


def personalize_content(subject, body, context):
    """
    Renders the email subject and body using the provided context,
    with Spintax processing and robust Jinja2 rendering.
    """
    # 1. Process Spintax first
    spun_subject = spin(subject)
    spun_body = spin(body)

    # 2. Render with Jinja2
    try:
        subject_template = jinja_env.from_string(spun_subject)
        rendered_subject = subject_template.render(context)

        body_template = jinja_env.from_string(spun_body)
        rendered_body = body_template.render(context)
        
        return rendered_subject, rendered_body
    except jinja_exceptions.TemplateError as e:
        # Fallback to simple replacement if Jinja rendering fails
        # This makes the system more robust against user errors in templates
        rendered_subject = spun_subject
        rendered_body = spun_body
        for key, value in context.items():
            rendered_subject = rendered_subject.replace(f"{{{{ {key} }}}}", str(value))
            rendered_body = rendered_body.replace(f"{{{{ {key} }}}}", str(value))
        return rendered_subject, rendered_body
```

### **2. Updated Application Files**

Here are the files from the `app/` directory that need to be updated to use the new logic and add the tracking routes.

````python name=app/models.py
from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash
from flask_login import UserMixin
from app import db, login

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), index=True, unique=True, nullable=False)
    email = db.Column(db.String(120), index=True, unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    campaigns = db.relationship('Campaign', backref='author', lazy='dynamic')

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def __repr__(self):
        return f'<User {self.username}>'

@login.user_loader
def load_user(id):
    return db.session.get(User, int(id))

class Campaign(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(140), nullable=False)
    subject = db.Column(db.String(200), nullable=False)
    body_html = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, index=True, default=datetime.utcnow)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    
    # SMTP Settings are stored with the campaign
    smtp_server = db.Column(db.String(120), nullable=False)
    smtp_port = db.Column(db.Integer, nullable=False)
    smtp_username = db.Column(db.String(120), nullable=False)
    smtp_password = db.Column(db.String(256)) # In production, use Fernet or KMS to encrypt this
    smtp_sender_name = db.Column(db.String(120), nullable=False)
    smtp_sender_email = db.Column(db.String(120), nullable=False)
    
    recipients = db.relationship('Recipient', backref='campaign', lazy='dynamic', cascade="all, delete-orphan")

    def __repr__(self):
        return f'<Campaign {self.name}>'

class Recipient(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    # Use a UUID for the public-facing ID to avoid exposing database IDs
    public_id = db.Column(db.String(36), unique=True, nullable=False, index=True)
    email = db.Column(db.String(120), index=True, nullable=False)
    campaign_id = db.Column(db.Integer, db.ForeignKey('campaign.id'), nullable=False)
    status = db.Column(db.String(50), default='Queued', index=True) # Queued, Sending, Sent, Failed, Opened, Clicked, Unsubscribed
    status_message = db.Column(db.String(250))
    sent_at = db.Column(db.DateTime, nullable=True)
    opened_at = db.Column(db.DateTime, nullable=True)
    clicked_at = db.Column(db.DateTime, nullable=True)
    unsubscribed_at = db.Column(db.DateTime, nullable=True) # New field
    
    # Store personalized data for this recipient as JSON text
    data = db.Column(db.Text) 

    __table_args__ = (db.Index('ix_recipient_campaign_id_email', 'campaign_id', 'email'),)

    def __repr__(self):
        return f'<Recipient {self.email}>'
