import os
from datetime import datetime, timezone
from app import db, login
from werkzeug.security import generate_password_hash, check_password_hash
from flask_login import UserMixin
from itsdangerous import URLSafeTimedSerializer as Serializer
from flask import current_app
from cryptography.fernet import Fernet

# The key for encrypting/decrypting sensitive data.
# In a real app, this should be handled more securely (e.g., from environment variables).
ENCRYPTION_KEY = os.environ.get('ENCRYPTION_KEY', Fernet.generate_key().decode())
fernet = Fernet(ENCRYPTION_KEY.encode())

@login.user_loader
def load_user(id):
    return User.query.get(int(id))

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), index=True, unique=True)
    email = db.Column(db.String(120), index=True, unique=True)
    password_hash = db.Column(db.String(128))

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def __repr__(self):
        return f'<User {self.username}>'

class SMTPServer(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    profile_name = db.Column(db.String(100), nullable=False)
    server = db.Column(db.String(120), nullable=False)
    port = db.Column(db.Integer, nullable=False)
    username = db.Column(db.String(120), nullable=False)
    encrypted_password = db.Column(db.String(256), nullable=False)
    use_tls = db.Column(db.Boolean, default=True)
    use_ssl = db.Column(db.Boolean, default=False)
    sender_name = db.Column(db.String(100))
    sender_email = db.Column(db.String(120))
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))

    def set_password(self, password):
        self.encrypted_password = fernet.encrypt(password.encode()).decode()

    def get_password(self):
        return fernet.decrypt(self.encrypted_password.encode()).decode()

    def to_dict(self):
        return {
            'server': self.server,
            'port': self.port,
            'username': self.username,
            'password': self.get_password(),
            'use_tls': self.use_tls,
            'use_ssl': self.use_ssl,
            'sender_name': self.sender_name,
            'sender_email': self.sender_email
        }

    def __repr__(self):
        return f'<SMTPServer {self.profile_name}>'

class Campaign(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    subject = db.Column(db.String(200), nullable=False)
    body = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    smtp_profile_id = db.Column(db.Integer, db.ForeignKey('smtp_server.id'))
    
    smtp_profile = db.relationship('SMTPServer', backref='campaigns')
    recipients = db.relationship('Recipient', backref='campaign', lazy='dynamic')

    def __repr__(self):
        return f'<Campaign {self.name}>'

class Recipient(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), nullable=False)
    first_name = db.Column(db.String(100))
    last_name = db.Column(db.String(100))
    status = db.Column(db.String(20), default='Queued') # e.g., Queued, Sending, Sent, Failed, Opened, Clicked
    status_message = db.Column(db.String(200)) # For storing error messages
    sent_at = db.Column(db.DateTime)
    opened_at = db.Column(db.DateTime)
    campaign_id = db.Column(db.Integer, db.ForeignKey('campaign.id'))

    def get_tracking_token(self, action, expires_sec=1800):
        s = Serializer(current_app.config['SECRET_KEY'])
        return s.dumps({'recipient_id': self.id, 'action': action})

    def __repr__(self):
        return f'<Recipient {self.email}>'

class Suppression(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    reason = db.Column(db.String(100)) # e.g., 'unsubscribe', 'bounce', 'spam_complaint'
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f'<Suppression {self.email}>'
