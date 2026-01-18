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
    
    # SMTP Settings are stored encrypted with the campaign
    smtp_server = db.Column(db.String(120), nullable=False)
    smtp_port = db.Column(db.Integer, nullable=False)
    smtp_username = db.Column(db.String(120), nullable=False)
    smtp_password = db.Column(db.String(256)) # In a real app, use Fernet to encrypt this
    smtp_sender_name = db.Column(db.String(120), nullable=False)
    smtp_sender_email = db.Column(db.String(120), nullable=False)
    
    recipients = db.relationship('Recipient', backref='campaign', lazy='dynamic', cascade="all, delete-orphan")

    def __repr__(self):
        return f'<Campaign {self.name}>'

class Recipient(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), index=True, nullable=False)
    campaign_id = db.Column(db.Integer, db.ForeignKey('campaign.id'), nullable=False)
    status = db.Column(db.String(50), default='Queued', index=True) # Queued, Sending, Sent, Failed, Opened, Clicked
    status_message = db.Column(db.String(250)) # For failure reasons
    sent_at = db.Column(db.DateTime, nullable=True)
    opened_at = db.Column(db.DateTime, nullable=True)
    clicked_at = db.Column(db.DateTime, nullable=True)
    
    # Store personalized data for this recipient as JSON text
    data = db.Column(db.Text) 

    __table_args__ = (db.Index('ix_recipient_campaign_id_email', 'campaign_id', 'email'),)

    def __repr__(self):
        return f'<Recipient {self.email}>'