from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash
from flask_login import UserMixin
from itsdangerous import URLSafeTimedSerializer as Serializer
from flask import current_app
from . import db, login
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
    port = db.Column(db.Integer, nullable=False)
    use_tls = db.Column(db.Boolean, default=True)
    use_ssl = db.Column(db.Boolean, default=False)
    username = db.Column(db.String(100), nullable=False)
    password_encrypted = db.Column(db.String(512))
    sender_name = db.Column(db.String(100))
    sender_email = db.Column(db.String(100))
    
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))

    def _get_fernet_key(self):
        """
        Generates a URL-safe base64-encoded 32-byte key from the app SECRET_KEY.
        This prevents Internal Server Errors if SECRET_KEY is not in Fernet format.
        """
        secret = current_app.config['SECRET_KEY']
        digest = hashlib.sha256(secret.encode()).digest()
        return base64.urlsafe_b64encode(digest)

    def set_password(self, password):
        if not password: 
            self.password_encrypted = None
            return
        try:
            key = self._get_fernet_key()
            f = Fernet(key)
            self.password_encrypted = f.encrypt(password.encode()).decode()
        except Exception as e:
            current_app.logger.error(f"Encryption Error: {e}")
            self.password_encrypted = None

    def get_password(self):
        if not self.password_encrypted: return None
        try:
            key = self._get_fernet_key()
            f = Fernet(key)
            return f.decrypt(self.password_encrypted.encode()).decode()
        except Exception as e:
            current_app.logger.error(f"Decryption Error: {e}. Key may have changed or data is corrupt.")
            return None
    
    def to_dict(self):
        return {
            'id': self.id,
            'name': self.profile_name,
            'server': self.server,
            'port': self.port,
            'username': self.username,
            'sender_name': self.sender_name,
            'sender_email': self.sender_email,
            'use_tls': self.use_tls,
            'use_ssl': self.use_ssl,
        }
        
    def to_config_dict(self):
        """ Returns a dictionary formatted for the SMTPHandler. """
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
    name = db.Column(db.String(140), nullable=False)
    subject = db.Column(db.String(255), nullable=False)
    body = db.Column(db.Text, nullable=False)
    
    ab_testing_enabled = db.Column(db.Boolean, default=False)
    subject_b = db.Column(db.String(255))
    body_b = db.Column(db.Text)
    ab_split_ratio = db.Column(db.Integer, default=50)
    
    burner_domain = db.Column(db.String(100))
    lure_path = db.Column(db.String(100))
    
    throttle_amount = db.Column(db.Integer, default=20)
    throttle_delay = db.Column(db.Integer, default=60)
    parallel_workers = db.Column(db.Integer, default=10)
    
    status = db.Column(db.String(20), default='Draft', index=True)
    
    timestamp = db.Column(db.DateTime, index=True, default=datetime.utcnow)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    
    smtp_profile_id = db.Column(db.Integer, db.ForeignKey('smtp_server.id'))
    smtp_profile = db.relationship('SMTPServer', backref='campaigns')
    
    recipients = db.relationship('Recipient', backref='campaign', lazy='dynamic', cascade="all, delete-orphan")

class Recipient(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), index=True, nullable=False)
    data = db.Column(db.Text) 
    status = db.Column(db.String(20), default='Queued', index=True)
    status_message = db.Column(db.String(255))
    campaign_id = db.Column(db.Integer, db.ForeignKey('campaign.id'), nullable=False)
    sent_at = db.Column(db.DateTime, nullable=True)
    opened_at = db.Column(db.DateTime, nullable=True)
    clicked_at = db.Column(db.DateTime, nullable=True)
    
    def get_tracking_token(self, action, payload=None):
        s = Serializer(current_app.config['SECRET_KEY'])
        data = {'action': action, 'rid': self.id, 'cid': self.campaign_id}
        if payload: data.update(payload)
        return s.dumps(data, salt='tracking-salt')

class Suppression(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, index=True, nullable=False)
    reason = db.Column(db.String(100))
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

class GlobalSettings(db.Model):
    id = db.Column(db.Integer, primary_key=True, default=1)
    burner_domain = db.Column(db.String(150))
    lure_path = db.Column(db.String(100))
    template_pdf_path = db.Column(db.String(255))
