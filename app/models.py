from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash
from flask_login import UserMixin
from app import db, login
import json

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
    
    # SMTP Settings stored with the campaign
    smtp_server = db.Column(db.String(120))
    smtp_port = db.Column(db.Integer)
    smtp_username = db.Column(db.String(120))
    smtp_password = db.Column(db.String(256)) # Encrypt this in a real app!
    smtp_sender_name = db.Column(db.String(120))
    smtp_sender_email = db.Column(db.String(120))
    
    recipients = db.relationship('Recipient', backref='campaign', lazy='dynamic', cascade="all, delete-orphan")

    def __repr__(self):
        return f'<Campaign {self.name}>'

class Recipient(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), index=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey('campaign.id'))
    status = db.Column(db.String(50), default='Queued') # e.g., Queued, Sending, Sent, Failed, Opened, Clicked, Unsubscribed
    status_message = db.Column(db.String(200)) # For failure reasons
    
    # Store personalized data as JSON
    data = db.Column(db.Text)

    # Tracking timestamps
    added_at = db.Column(db.DateTime, default=datetime.utcnow)
    sent_at = db.Column(db.DateTime, nullable=True)
    opened_at = db.Column(db.DateTime, nullable=True)
    clicked_at = db.Column(db.DateTime, nullable=True)
    unsubscribed_at = db.Column(db.DateTime, nullable=True)

    def set_data(self, data_dict):
        self.data = json.dumps(data_dict)

    def get_data(self):
        if self.data:
            return json.loads(self.data)
        return {}

    def __repr__(self):
        return f'<Recipient {self.email}>'
