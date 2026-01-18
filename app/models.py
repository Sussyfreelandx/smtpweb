import json
from datetime import datetime, timedelta
from werkzeug.security import generate_password_hash, check_password_hash
from flask_login import UserMixin
from flask import current_app
from itsdangerous import URLSafeTimedSerializer
from app import db, login

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

    def __repr__(self):
        return f'<User {self.username}>'

@login.user_loader
def load_user(id):
    return User.query.get(int(id))

class Campaign(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(140))
    subject = db.Column(db.String(200))
    body_html = db.Column(db.Text)
    created_at = db.Column(db.DateTime, index=True, default=datetime.utcnow)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    
    smtp_server = db.Column(db.String(120))
    smtp_port = db.Column(db.Integer)
    smtp_username = db.Column(db.String(120))
    smtp_password = db.Column(db.String(200)) # Encrypt this in a real high-security app!
    smtp_sender_name = db.Column(db.String(120))
    smtp_sender_email = db.Column(db.String(120))
    
    recipients = db.relationship('Recipient', backref='campaign', lazy='dynamic', cascade="all, delete-orphan")

    def __repr__(self):
        return f'<Campaign {self.name}>'

class Recipient(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), index=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey('campaign.id'))
    status = db.Column(db.String(50), default='Queued')
    status_message = db.Column(db.String(250))
    sent_at = db.Column(db.DateTime, nullable=True)
    opened_at = db.Column(db.DateTime, nullable=True)
    clicked_at = db.Column(db.DateTime, nullable=True)
    
    data = db.Column(db.Text) 

    def get_data(self):
        return json.loads(self.data) if self.data else {}

    def get_tracking_token(self, action, payload=None):
        serializer = URLSafeTimedSerializer(current_app.config['SECRET_KEY'])
        data_to_sign = {'recipient_id': self.id, 'action': action}
        if payload:
            data_to_sign.update(payload)
        return serializer.dumps(data_to_sign, salt=current_app.config['SECURITY_PASSWORD_SALT'])
    
    @staticmethod
    def verify_tracking_token(token, max_age_days=30):
        serializer = URLSafeTimedSerializer(current_app.config['SECRET_KEY'])
        try:
            data = serializer.loads(
                token,
                salt=current_app.config['SECURITY_PASSWORD_SALT'],
                max_age=timedelta(days=max_age_days).total_seconds()
            )
            return data
        except:
            return None

    def __repr__(self):
        return f'<Recipient {self.email}>'

class Suppression(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), index=True, unique=True)
    reason = db.Column(db.String(100)) # 'unsubscribe', 'bounce', 'manual'
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f'<Suppression {self.email}>'
