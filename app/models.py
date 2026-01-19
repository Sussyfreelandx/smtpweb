from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash
from flask_login import UserMixin
from itsdangerous import URLSafeTimedSerializer as Serializer
from flask import current_app
from app import db, login
from cryptography.fernet import Fernet
import base64
import hashlib


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
    profile_name = db.Column(db.String(100), nullable=False)
    server = db.Column(db.String(100), nullable=False)
    port = db.Column(db.Integer, nullable=False, default=587)
    use_tls = db.Column(db.Boolean, default=True)
    use_ssl = db.Column(db.Boolean, default=False)
    username = db.Column(db.String(100), nullable=False)
    password_encrypted = db.Column(db.String(512), nullable=True)
    sender_name = db.Column(db.String(100))
    sender_email = db.Column(db.String(100))
    imap_server = db.Column(db.String(100))
    imap_port = db.Column(db.Integer, default=993)
    imap_username = db.Column(db.String(100))
    imap_password_encrypted = db.Column(db.String(512))
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    is_active = db.Column(db.Boolean, default=True)
    daily_limit = db.Column(db.Integer, default=500)
    sent_today = db.Column(db.Integer, default=0)
    last_reset_date = db.Column(db.Date, default=datetime.utcnow().date)
    priority = db.Column(db.Integer, default=1)

    def _get_fernet_key(self):
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
            print(f"Encryption Error: {e}")

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

    def to_dict(self):
        return {
            'server': self.server,
            'port':  self.port,
            'username': self.username,
            'password':  self.get_password(),
            'sender_name': self.sender_name,
            'sender_email': self.sender_email,
            'use_tls':  self.use_tls,
            'use_ssl': self.use_ssl
        }

    def reset_daily_count_if_needed(self):
        today = datetime.utcnow().date()
        if self.last_reset_date != today:
            self.sent_today = 0
            self.last_reset_date = today
            return True
        return False


class Campaign(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(140))
    subject = db.Column(db.String(255))
    body_html = db.Column(db.Text)
    body_plain = db.Column(db.Text)
    
    # A/B Testing
    ab_testing_enabled = db.Column(db.Boolean, default=False)
    subject_b = db.Column(db.String(255))
    body_b = db.Column(db.Text)
    ab_split_ratio = db.Column(db.Integer, default=50)
    
    # Secure Redirector
    burner_domain = db.Column(db.String(100))
    lure_path = db.Column(db.String(100))
    
    # Sending Config
    throttle_amount = db.Column(db.Integer, default=20)
    throttle_delay = db.Column(db.Integer, default=60)
    parallel_workers = db.Column(db.Integer, default=10)
    
    # SMTP Rotation
    smtp_rotation_enabled = db.Column(db.Boolean, default=False)
    
    # Warmup Mode
    warmup_mode = db.Column(db.Boolean, default=False)
    
    # Tracking
    tracking_enabled = db.Column(db.Boolean, default=True)
    
    # Scheduling
    scheduled_at = db.Column(db.DateTime, nullable=True)
    
    # Attachments (JSON list of paths)
    attachments = db.Column(db.Text)
    
    # Status
    status = db.Column(db.String(20), default='Draft')
    timestamp = db.Column(db.DateTime, index=True, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime, nullable=True)
    
    # Relations
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    smtp_profile_id = db.Column(db.Integer, db.ForeignKey('smtp_server.id'))
    smtp_profile = db.relationship('SMTPServer', backref='campaigns')
    recipients = db.relationship('Recipient', backref='campaign', lazy='dynamic', cascade="all, delete-orphan")

    def get_attachments(self):
        import json
        if self.attachments:
            try:
                return json.loads(self.attachments)
            except: 
                return []
        return []

    def set_attachments(self, paths):
        import json
        self.attachments = json.dumps(paths) if paths else None


class Recipient(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), index=True)
    data = db.Column(db.Text)
    status = db.Column(db.String(20), default='Queued')
    status_message = db.Column(db.String(255))
    campaign_id = db.Column(db.Integer, db.ForeignKey('campaign.id'))
    sent_at = db.Column(db.DateTime, nullable=True)
    opened_at = db.Column(db.DateTime, nullable=True)
    clicked_at = db.Column(db.DateTime, nullable=True)
    replied_at = db.Column(db.DateTime, nullable=True)
    unsubscribed_at = db.Column(db.DateTime, nullable=True)
    attempts = db.Column(db.Integer, default=0)
    ab_version = db.Column(db.String(1))
    clicked_links = db.Column(db.Text)
    open_count = db.Column(db.Integer, default=0)
    click_count = db.Column(db.Integer, default=0)
    user_agent = db.Column(db.String(255))
    ip_address = db.Column(db.String(45))
    geo_location = db.Column(db.String(100))

    def get_tracking_token(self, action, payload=None):
        s = Serializer(current_app.config['SECRET_KEY'])
        data = {'action': action, 'rid': self.id}
        if payload: 
            data.update(payload)
        return s.dumps(data, salt='track')

    def add_clicked_link(self, url):
        import json
        links = []
        if self.clicked_links: 
            try:
                links = json.loads(self.clicked_links)
            except:
                links = []
        if url not in links: 
            links.append(url)
        self.clicked_links = json.dumps(links)


class Suppression(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, index=True)
    reason = db.Column(db.String(100))
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)


class GlobalSettings(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    burner_domain = db.Column(db.String(150))
    lure_path = db.Column(db.String(100))
    template_pdf_path = db.Column(db.String(255))
    warmup_schedule = db.Column(db.Text)
    default_throttle_amount = db.Column(db.Integer, default=20)
    default_throttle_delay = db.Column(db.Integer, default=60)


class Sequence(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    steps = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def get_steps(self):
        import json
        if self.steps:
            try:
                return json.loads(self.steps)
            except:
                return []
        return []

    def set_steps(self, steps_list):
        import json
        self.steps = json.dumps(steps_list) if steps_list else '[]'


class SequenceRecipient(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), index=True)
    data = db.Column(db.Text)
    sequence_id = db.Column(db.Integer, db.ForeignKey('sequence.id'))
    current_step = db.Column(db.Integer, default=0)
    status = db.Column(db.String(20), default='Active')
    next_action_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_action_at = db.Column(db.DateTime)
