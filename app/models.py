from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash
from flask_login import UserMixin
from itsdangerous import URLSafeTimedSerializer as Serializer
from flask import current_app
from app import db, login
from cryptography.fernet import Fernet

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
    # NEW: Parallel workers setting
    parallel_workers = db.Column(db.Integer, default=1)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))

    def set_password(self, password):
        # Ensure we have a string for the key
        key = current_app.config['SECRET_KEY']
        if not key:
            raise ValueError("SECRET_KEY is missing in config.")
        # Fernet requires a 32-byte base64 encoded key. 
        # If SECRET_KEY isn't compliant, we hash it to make it safe.
        import base64
        import hashlib
        key_bytes = hashlib.sha256(key.encode()).digest()
        safe_key = base64.urlsafe_b64encode(key_bytes)
        
        f = Fernet(safe_key)
        self.password_encrypted = f.encrypt(password.encode()).decode()

    def get_password(self):
        key = current_app.config['SECRET_KEY']
        if not key:
            return None
        import base64
        import hashlib
        key_bytes = hashlib.sha256(key.encode()).digest()
        safe_key = base64.urlsafe_b64encode(key_bytes)
        
        f = Fernet(safe_key)
        try:
            return f.decrypt(self.password_encrypted.encode()).decode()
        except:
            return None
    
    def to_dict(self):
        return {
            'server': self.server,
            'port': self.port,
            'username': self.username,
            'password': self.get_password(),
            'sender_name': self.sender_name,
            'sender_email': self.sender_email,
            'use_tls': self.use_tls,
            'use_ssl': self.use_ssl,
            'parallel_workers': self.parallel_workers
        }

class Campaign(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(140))
    subject = db.Column(db.String(140))
    body = db.Column(db.Text)
    timestamp = db.Column(db.DateTime, index=True, default=datetime.utcnow)
    status = db.Column(db.String(20), default='Draft') # Draft, Sending, Completed, Paused
    
    # NEW: Throttling settings
    throttle_amount = db.Column(db.Integer, default=0) # 0 means disabled
    throttle_delay = db.Column(db.Integer, default=0) # in seconds
    
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    smtp_profile_id = db.Column(db.Integer, db.ForeignKey('smtp_server.id'))
    smtp_profile = db.relationship('SMTPServer', backref='campaigns')
    recipients = db.relationship('Recipient', backref='campaign', lazy='dynamic', cascade="all, delete-orphan")

class Recipient(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), index=True)
    data = db.Column(db.Text) # JSON string for personalization data (firstname, company, etc)
    status = db.Column(db.String(20), default='Queued')
    status_message = db.Column(db.String(200))
    campaign_id = db.Column(db.Integer, db.ForeignKey('campaign.id'))
    sent_at = db.Column(db.DateTime, nullable=True)
    opened_at = db.Column(db.DateTime, nullable=True)
    clicked_at = db.Column(db.DateTime, nullable=True)
    unsubscribed_at = db.Column(db.DateTime, nullable=True)

    def get_tracking_token(self, action, payload=None):
        s = Serializer(current_app.config['SECRET_KEY'])
        data = {'action': action, 'recipient_id': self.id}
        if payload:
            data.update(payload)
        return s.dumps(data)

class Suppression(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, index=True)
    reason = db.Column(db.String(100))
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
