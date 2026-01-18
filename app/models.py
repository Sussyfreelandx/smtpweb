from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash
from flask_login import UserMixin
from app import db, login
import json

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), index=True, unique=True)
    email = db.Column(db.String(120), index=True, unique=True)
    password_hash = db.Column(db.String(256)) # Increased length for stronger hashing
    campaigns = db.relationship('Campaign', backref='author', lazy='dynamic')
    smtp_profiles = db.relationship('SmtpProfile', backref='owner', lazy='dynamic')

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def __repr__(self):
        return f'<User {self.username}>'

@login.user_loader
def load_user(id):
    return User.query.get(int(id))

class Campaign(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(140))
    created_at = db.Column(db.DateTime, index=True, default=datetime.utcnow)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    
    # --- Content Fields ---
    subject_a = db.Column(db.String(200))
    body_html_a = db.Column(db.Text)
    
    # --- A/B Testing Fields (NEW) ---
    is_ab_test = db.Column(db.Boolean, default=False)
    subject_b = db.Column(db.String(200), nullable=True)
    body_html_b = db.Column(db.Text, nullable=True)
    ab_split_ratio = db.Column(db.Integer, default=50) # Percentage for version A
    
    # --- Association to SMTP profiles (NEW) ---
    # Can be a single profile or a group of profiles for rotation
    smtp_profile_ids = db.Column(db.String(200), nullable=True) # Storing comma-separated IDs
    
    recipients = db.relationship('Recipient', backref='campaign', lazy='dynamic', cascade="all, delete-orphan")

    def __repr__(self):
        return f'<Campaign {self.name}>'

class Recipient(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), index=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey('campaign.id'))
    
    # Status & Tracking
    status = db.Column(db.String(50), default='Queued') # e.g., Queued, Sending, Sent, Failed, Opened, Clicked, Unsubscribed
    status_message = db.Column(db.String(250)) # For failure reasons
    sent_at = db.Column(db.DateTime, nullable=True)
    opened_at = db.Column(db.DateTime, nullable=True)
    clicked_at = db.Column(db.DateTime, nullable=True)
    
    # Which version of the A/B test they received
    version_sent = db.Column(db.String(1), nullable=True) # 'A' or 'B'
    
    # Store personalized data from CSV as JSON
    data = db.Column(db.Text) 

    def __repr__(self):
        return f'<Recipient {self.email}>'

class SmtpProfile(db.Model):
    """(NEW) Model to store user's SMTP configurations."""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    server = db.Column(db.String(120), nullable=False)
    port = db.Column(db.Integer, default=587)
    username = db.Column(db.String(120), nullable=False)
    password = db.Column(db.String(256)) # IMPORTANT: Encrypt this in a real production environment
    sender_name = db.Column(db.String(120))
    sender_email = db.Column(db.String(120))
    use_tls = db.Column(db.Boolean, default=True)
    use_ssl = db.Column(db.Boolean, default=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    
    def __repr__(self):
        return f'<SmtpProfile {self.name}>'

class SuppressedEmail(db.Model):
    """(NEW) Global suppression list."""
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), index=True, unique=True, nullable=False)
    reason = db.Column(db.String(100), nullable=True) # e.g., 'Unsubscribed', 'Hard Bounce'
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f'<SuppressedEmail {self.email}>'
