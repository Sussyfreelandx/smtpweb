from datetime import datetime, date
from werkzeug.security import generate_password_hash, check_password_hash
from flask_login import UserMixin
from itsdangerous import URLSafeTimedSerializer as Serializer
from flask import current_app
from app import db, login
from cryptography.fernet import Fernet
import base64
import hashlib
import json


@login.user_loader
def load_user(id):
    return User.query.get(int(id))


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), index=True, unique=True)
    email = db.Column(db.String(120), index=True, unique=True)
    password_hash = db.Column(db.String(256))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    campaigns = db.relationship('Campaign', backref='author', lazy='dynamic')

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def __repr__(self):
        return f'<User {self.username}>'


class SMTPServer(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    profile_name = db.Column(db.String(100), nullable=False)
    server = db.Column(db.String(100), nullable=False)
    port = db.Column(db.Integer, nullable=False, default=587)
    use_tls = db.Column(db.Boolean, default=True)
    use_ssl = db.Column(db.Boolean, default=False)
    username = db.Column(db.String(100), nullable=False)
    password_encrypted = db.Column(db.String(512), nullable=True)
    sender_name = db.Column(db.String(100))
    sender_email = db.Column(db.String(100))
    
    # IMAP settings for reply tracking
    imap_server = db.Column(db.String(100))
    imap_port = db.Column(db.Integer, default=993)
    imap_username = db.Column(db.String(100))
    imap_password_encrypted = db.Column(db.String(512))
    
    # Rotation and limits
    is_active = db.Column(db.Boolean, default=True)
    daily_limit = db.Column(db.Integer, default=500)
    sent_today = db.Column(db.Integer, default=0)
    last_reset_date = db.Column(db.Date, default=date.today)
    priority = db.Column(db.Integer, default=1)
    
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    user = db.relationship('User', backref='smtp_profiles')

    def _get_fernet_key(self):
        """Generates a safe URL-safe base64-encoded 32-byte key from the app SECRET_KEY."""
        secret = current_app.config['SECRET_KEY']
        digest = hashlib.sha256(secret.encode()).digest()
        return base64.urlsafe_b64encode(digest)

    def set_password(self, password):
        if not password:
            return
        try:
            key = self._get_fernet_key()
            f = Fernet(key)
            self.password_encrypted = f.encrypt(password.encode()).decode()
        except Exception as e: 
            current_app.logger.error(f"Encryption Error: {e}")

    def get_password(self):
        if not self.password_encrypted:
            return None
        try:
            key = self._get_fernet_key()
            f = Fernet(key)
            return f.decrypt(self.password_encrypted.encode()).decode()
        except Exception: 
            return None

    def set_imap_password(self, password):
        if not password:
            return
        try:
            key = self._get_fernet_key()
            f = Fernet(key)
            self.imap_password_encrypted = f.encrypt(password.encode()).decode()
        except Exception: 
            pass

    def get_imap_password(self):
        if not self.imap_password_encrypted: 
            return None
        try:
            key = self._get_fernet_key()
            f = Fernet(key)
            return f.decrypt(self.imap_password_encrypted.encode()).decode()
        except Exception:
            return None

    def reset_daily_count_if_needed(self):
        """Reset daily count if it's a new day."""
        today = date.today()
        if self.last_reset_date != today:
            self.sent_today = 0
            self.last_reset_date = today
            db.session.commit()

    def to_dict(self):
        return {
            'server': self.server,
            'port':  self.port,
            'username': self.username,
            'password':  self.get_password(),
            'sender_name': self.sender_name or '',
            'sender_email': self.sender_email or self.username,
            'use_tls': self.use_tls,
            'use_ssl':  self.use_ssl
        }

    def __repr__(self):
        return f'<SMTPServer {self.profile_name}>'


class Campaign(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(140))
    subject = db.Column(db.String(500))
    body_html = db.Column(db.Text)
    body_plain = db.Column(db.Text)
    
    # A/B Testing
    ab_testing_enabled = db.Column(db.Boolean, default=False)
    subject_b = db.Column(db.String(500))
    body_b = db.Column(db.Text)
    ab_split_ratio = db.Column(db.Integer, default=50)
    
    # Secure Redirector
    burner_domain = db.Column(db.String(200))
    lure_path = db.Column(db.String(200))
    
    # Throttling Config
    throttle_amount = db.Column(db.Integer, default=20)
    throttle_delay = db.Column(db.Integer, default=60)
    parallel_workers = db.Column(db.Integer, default=10)
    
    # Options
    tracking_enabled = db.Column(db.Boolean, default=True)
    warmup_mode = db.Column(db.Boolean, default=False)
    smtp_rotation_enabled = db.Column(db.Boolean, default=False)
    
    # Status tracking
    status = db.Column(db.String(20), default='Draft')
    
    # Timestamps
    timestamp = db.Column(db.DateTime, index=True, default=datetime.utcnow)
    scheduled_at = db.Column(db.DateTime, nullable=True)
    started_at = db.Column(db.DateTime, nullable=True)
    completed_at = db.Column(db.DateTime, nullable=True)
    
    # Attachments (JSON array of file paths)
    attachments_json = db.Column(db.Text)
    
    # Foreign Keys
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    smtp_profile_id = db.Column(db.Integer, db.ForeignKey('smtp_server.id'))
    smtp_profile = db.relationship('SMTPServer', backref='campaigns')
    
    recipients = db.relationship('Recipient', backref='campaign', lazy='dynamic', cascade="all, delete-orphan")

    def get_attachments(self):
        if not self.attachments_json:
            return []
        try:
            return json.loads(self.attachments_json)
        except: 
            return []

    def set_attachments(self, attachments):
        self.attachments_json = json.dumps(attachments) if attachments else None

    def __repr__(self):
        return f'<Campaign {self.name}>'


class Recipient(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), index=True)
    data = db.Column(db.Text)
    status = db.Column(db.String(20), default='Queued')
    status_message = db.Column(db.String(500))
    
    # A/B Testing
    ab_version = db.Column(db.String(1))
    
    # Tracking
    sent_at = db.Column(db.DateTime, nullable=True)
    opened_at = db.Column(db.DateTime, nullable=True)
    clicked_at = db.Column(db.DateTime, nullable=True)
    unsubscribed_at = db.Column(db.DateTime, nullable=True)
    
    # Counts
    open_count = db.Column(db.Integer, default=0)
    click_count = db.Column(db.Integer, default=0)
    attempts = db.Column(db.Integer, default=0)
    
    # Additional tracking info
    user_agent = db.Column(db.String(500))
    ip_address = db.Column(db.String(50))
    clicked_links = db.Column(db.Text)
    
    campaign_id = db.Column(db.Integer, db.ForeignKey('campaign.id'))

    def get_data(self):
        if not self.data:
            return {}
        try: 
            return json.loads(self.data)
        except: 
            return {}

    def add_clicked_link(self, url):
        try:
            links = json.loads(self.clicked_links) if self.clicked_links else []
            if url not in links:
                links.append(url)
            self.clicked_links = json.dumps(links)
        except:
            self.clicked_links = json.dumps([url])

    def get_tracking_token(self, action, payload=None):
        s = Serializer(current_app.config['SECRET_KEY'])
        data = {'action': action, 'rid': self.id}
        if payload: 
            data.update(payload)
        return s.dumps(data, salt='track')

    def __repr__(self):
        return f'<Recipient {self.email}>'


class Suppression(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, index=True)
    reason = db.Column(db.String(100))
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f'<Suppression {self.email}>'


class GlobalSettings(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    burner_domain = db.Column(db.String(200))
    lure_path = db.Column(db.String(200))
    template_pdf_path = db.Column(db.String(500))
    default_throttle_amount = db.Column(db.Integer, default=20)
    default_throttle_delay = db.Column(db.Integer, default=60)

    def __repr__(self):
        return f'<GlobalSettings {self.id}>'
