from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash
from flask_login import UserMixin
from itsdangerous import URLSafeTimedSerializer as Serializer
from flask import current_app
from app import db, login
from cryptography.fernet import Fernet
import json

@login.user_loader
def load_user(id):
    return User.query.get(int(id))

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), index=True, unique=True)
    email = db.Column(db.String(120), index=True, unique=True)
    password_hash = db.Column(db.String(256))
    campaigns = db.relationship('Campaign', backref='author', lazy='dynamic')

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

class SMTPServer(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    profile_name = db.Column(db.String(100), unique=True, nullable=False)
    server = db.Column(db.String(100), nullable=False)
    port = db.Column(db.Integer, nullable=False)
    use_tls = db.Column(db.Boolean, default=True)
    use_ssl = db.Column(db.Boolean, default=False)
    username = db.Column(db.String(100), nullable=False)
    password_encrypted = db.Column(db.String(512), nullable=False)
    sender_name = db.Column(db.String(100))
    sender_email = db.Column(db.String(100))
    
    # IMAP Support
    imap_server = db.Column(db.String(100))
    imap_port = db.Column(db.Integer, default=993)
    imap_username = db.Column(db.String(100))
    imap_password_encrypted = db.Column(db.String(512))
    
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))

    def set_password(self, password):
        key = current_app.config['SECRET_KEY'].encode()
        f = Fernet(key)
        self.password_encrypted = f.encrypt(password.encode()).decode()

    def get_password(self):
        key = current_app.config['SECRET_KEY'].encode()
        f = Fernet(key)
        return f.decrypt(self.password_encrypted.encode()).decode()
    
    def set_imap_password(self, password):
        key = current_app.config['SECRET_KEY'].encode()
        f = Fernet(key)
        self.imap_password_encrypted = f.encrypt(password.encode()).decode()

    def get_imap_password(self):
        if not self.imap_password_encrypted: return None
        key = current_app.config['SECRET_KEY'].encode()
        f = Fernet(key)
        return f.decrypt(self.imap_password_encrypted.encode()).decode()
    
    def to_dict(self):
        return {
            'server': self.server,
            'port': self.port,
            'username': self.username,
            'password': self.get_password(),
            'sender_name': self.sender_name,
            'sender_email': self.sender_email,
            'use_tls': self.use_tls,
            'use_ssl': self.use_ssl
        }

class Campaign(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(140))
    status = db.Column(db.String(20), default='Draft') # Draft, Sending, Paused, Stopped, Completed
    
    # Content
    subject = db.Column(db.String(140))
    body = db.Column(db.Text)
    
    # Configuration
    parallel_workers = db.Column(db.Integer, default=10)
    smtp_rotation_enabled = db.Column(db.Boolean, default=False)
    throttle_amount = db.Column(db.Integer, default=20)
    throttle_delay = db.Column(db.Integer, default=1)
    throttle_unit = db.Column(db.String(10), default='Minutes')
    
    # A/B Testing
    ab_testing_enabled = db.Column(db.Boolean, default=False)
    subject_b = db.Column(db.String(140))
    body_b = db.Column(db.Text)
    ab_split_ratio = db.Column(db.Integer, default=50)
    
    # Secure Redirector (Updated with PDF)
    burner_domain = db.Column(db.String(100))
    lure_path = db.Column(db.String(100))
    template_pdf = db.Column(db.String(200)) # Stores file path
    
    timestamp = db.Column(db.DateTime, index=True, default=datetime.utcnow)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    smtp_profile_id = db.Column(db.Integer, db.ForeignKey('smtp_server.id'))
    smtp_profile = db.relationship('SMTPServer', backref='campaigns')
    recipients = db.relationship('Recipient', backref='campaign', lazy='dynamic', cascade="all, delete-orphan")
    logs = db.relationship('ActivityLog', backref='campaign', lazy='dynamic', cascade="all, delete-orphan")

class Recipient(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), index=True)
    data = db.Column(db.Text) # JSON data
    
    status = db.Column(db.String(20), default='Queued')
    status_message = db.Column(db.String(200))
    campaign_id = db.Column(db.Integer, db.ForeignKey('campaign.id'))
    sent_at = db.Column(db.DateTime, nullable=True)
    opened_at = db.Column(db.DateTime, nullable=True)
    clicked_at = db.Column(db.DateTime, nullable=True)

    def get_tracking_token(self, action, expires_in=None, payload=None):
        s = Serializer(current_app.config['SECRET_KEY'])
        data = {'action': action, 'recipient_id': self.id}
        if payload:
            data.update(payload)
        return s.dumps(data, salt=action)

class ActivityLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey('campaign.id'))
    message = db.Column(db.String(500))
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    log_type = db.Column(db.String(20), default='info') # info, error, success

    def to_dict(self):
        return {
            'timestamp': self.timestamp.strftime('%H:%M:%S'),
            'message': self.message,
            'type': self.log_type
        }

class Suppression(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, index=True)
    reason = db.Column(db.String(100))
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
