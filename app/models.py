from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash
from flask_login import UserMixin
from itsdangerous import URLSafeTimedSerializer as Serializer
from flask import current_app
from app import db, login
from cryptography.fernet import Fernet, InvalidToken
import base64
import hashlib
import json
import secrets
import enum

@login.user_loader
def load_user(id):
    return User.query.get(int(id))

# ==================== ENUMS ====================

class UserRole(enum.Enum):
    VIEWER = 'viewer'
    EDITOR = 'editor'
    ADMIN = 'admin'
    OWNER = 'owner'

class CampaignStatus(enum.Enum):
    DRAFT = 'Draft'
    SCHEDULED = 'Scheduled'
    SENDING = 'Sending'
    PAUSED = 'Paused'
    COMPLETED = 'Completed'
    STOPPED = 'Stopped'
    FAILED = 'Failed'

# ==================== ASSOCIATION TABLES ====================

team_members = db.Table('team_members',
    db.Column('user_id', db.Integer, db.ForeignKey('user.id', ondelete='CASCADE'), primary_key=True),
    db.Column('team_id', db.Integer, db.ForeignKey('team.id', ondelete='CASCADE'), primary_key=True),
    db.Column('role', db.String(20), default='editor'),
    db.Column('joined_at', db.DateTime, default=datetime.utcnow)
)

campaign_tags = db.Table('campaign_tags',
    db.Column('campaign_id', db.Integer, db.ForeignKey('campaign.id', ondelete='CASCADE'), primary_key=True),
    db.Column('tag_id', db.Integer, db.ForeignKey('tag.id', ondelete='CASCADE'), primary_key=True)
)

recipient_segments = db.Table('recipient_segments',
    db.Column('recipient_id', db.Integer, db.ForeignKey('recipient.id', ondelete='CASCADE'), primary_key=True),
    db.Column('segment_id', db.Integer, db.ForeignKey('segment.id', ondelete='CASCADE'), primary_key=True)
)

# ==================== USER & TEAM MODELS ====================

class User(UserMixin, db.Model):
    __tablename__ = 'user'
    
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), index=True, unique=True, nullable=False)
    email = db.Column(db.String(120), index=True, unique=True, nullable=False)
    password_hash = db.Column(db.String(256))
    role = db.Column(db.String(20), default='editor')
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_login = db.Column(db.DateTime)
    
    # Two-Factor Authentication
    two_factor_enabled = db.Column(db.Boolean, default=False)
    two_factor_secret = db.Column(db.String(32))
    
    # Profile
    first_name = db.Column(db.String(64))
    last_name = db.Column(db.String(64))
    avatar_url = db.Column(db.String(255))
    timezone = db.Column(db.String(50), default='UTC')
    
    # Preferences
    preferences = db.Column(db.Text)  # JSON
    
    # Relationships
    campaigns = db.relationship('Campaign', backref='author', lazy='dynamic', foreign_keys='Campaign.user_id')
    api_keys = db.relationship('APIKey', backref='user', lazy='dynamic')
    notifications = db.relationship('Notification', backref='user', lazy='dynamic')
    activity_logs = db.relationship('ActivityLog', backref='user', lazy='dynamic')
    
    def set_password(self, password):
        self.password_hash = generate_password_hash(password)
    
    def check_password(self, password):
        if not self.password_hash:
            return False
        return check_password_hash(self.password_hash, password)
    
    def generate_2fa_secret(self):
        import pyotp
        self.two_factor_secret = pyotp.random_base32()
        return self.two_factor_secret
    
    def verify_2fa_token(self, token):
        if not self.two_factor_secret:
            return False
        import pyotp
        totp = pyotp.TOTP(self.two_factor_secret)
        return totp.verify(token)
    
    def get_2fa_qr_uri(self):
        import pyotp
        if not self.two_factor_secret:
            return None
        totp = pyotp.TOTP(self.two_factor_secret)
        return totp.provisioning_uri(self.email, issuer_name="Paris Sender")
    
    def get_preferences(self):
        if self.preferences:
            try:
                return json.loads(self.preferences)
            except:
                pass
        return {}
    
    def get_full_name(self):
        if self.first_name and self.last_name:
            return f"{self.first_name} {self.last_name}"
        return self.username or self.email.split('@')[0]
    
    def to_dict(self):
        return {
            'id': self.id,
            'username': self.username,
            'email': self.email,
            'role': self.role,
            'is_active': self.is_active,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'full_name': self.get_full_name()
        }

class Team(db.Model):
    __tablename__ = 'team'
    
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    slug = db.Column(db.String(100), unique=True)
    description = db.Column(db.Text)
    owner_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='SET NULL'))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    # White Label Settings
    custom_logo_url = db.Column(db.String(255))
    custom_domain = db.Column(db.String(255))
    primary_color = db.Column(db.String(7), default='#007bff')
    
    # Relationships
    owner = db.relationship('User', backref='owned_teams')
    members = db.relationship('User', secondary=team_members, backref='teams')
    campaigns = db.relationship('Campaign', backref='team', lazy='dynamic')

# ==================== SMTP & EMAIL MODELS ====================

class SMTPServer(db.Model):
    __tablename__ = 'smtp_server'
    
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
    reply_to_email = db.Column(db.String(100))
    
    # IMAP Settings
    imap_server = db.Column(db.String(100))
    imap_port = db.Column(db.Integer, default=993)
    imap_username = db.Column(db.String(100))
    imap_password_encrypted = db.Column(db.String(512))
    
    # Ownership
    user_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='CASCADE'))
    team_id = db.Column(db.Integer, db.ForeignKey('team.id', ondelete='CASCADE'))
    
    # Status & Limits
    is_active = db.Column(db.Boolean, default=True)
    daily_limit = db.Column(db.Integer, default=500)
    hourly_limit = db.Column(db.Integer, default=100)
    sent_today = db.Column(db.Integer, default=0)
    last_reset_date = db.Column(db.Date, default=datetime.utcnow().date)
    priority = db.Column(db.Integer, default=1)
    
    # Warmup
    warmup_enabled = db.Column(db.Boolean, default=False)
    warmup_start_date = db.Column(db.Date)
    
    # Health
    last_test_at = db.Column(db.DateTime)
    last_test_result = db.Column(db.Boolean)
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
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
            current_app.logger.error(f"Encryption Error: {e}")
    
    def get_password(self):
        if not self.password_encrypted:
            return None
        try: 
            key = self._get_fernet_key()
            f = Fernet(key)
            return f.decrypt(self.password_encrypted.encode()).decode()
        except InvalidToken:
            return None
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
            'port': self.port,
            'username': self.username,
            'password': self.get_password(),
            'sender_name': self.sender_name,
            'sender_email': self.sender_email,
            'reply_to_email': self.reply_to_email,
            'use_tls': self.use_tls,
            'use_ssl': self.use_ssl
        }

class EmailTemplate(db.Model):
    __tablename__ = 'email_template'
    
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text)
    html_content = db.Column(db.Text, nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='SET NULL'))
    is_public = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

# ==================== CAMPAIGN MODELS ====================

class Campaign(db.Model):
    __tablename__ = 'campaign'
    
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(140), nullable=False)
    subject = db.Column(db.String(255))
    preheader = db.Column(db.String(255))
    body_html = db.Column(db.Text)
    body_plain = db.Column(db.Text)
    
    # A/B Testing
    ab_testing_enabled = db.Column(db.Boolean, default=False)
    subject_b = db.Column(db.String(255))
    body_b = db.Column(db.Text)
    ab_split_ratio = db.Column(db.Integer, default=50)
    
    # Configuration
    burner_domain = db.Column(db.String(100))
    lure_path = db.Column(db.String(100))
    throttle_amount = db.Column(db.Integer, default=20)
    throttle_delay = db.Column(db.Integer, default=60)
    smtp_rotation_enabled = db.Column(db.Boolean, default=False)
    smtp_profile_id = db.Column(db.Integer, db.ForeignKey('smtp_server.id', ondelete='SET NULL'))
    
    # Tracking & Options
    tracking_enabled = db.Column(db.Boolean, default=True)
    warmup_mode = db.Column(db.Boolean, default=False)
    smart_send_enabled = db.Column(db.Boolean, default=False)
    
    # Scheduling
    scheduled_at = db.Column(db.DateTime, nullable=True)
    
    # Status
    status = db.Column(db.String(20), default='Draft')
    total_recipients = db.Column(db.Integer, default=0)
    sent_count = db.Column(db.Integer, default=0)
    failed_count = db.Column(db.Integer, default=0)
    
    # Timestamps
    created_at = db.Column(db.DateTime, index=True, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    started_at = db.Column(db.DateTime)
    completed_at = db.Column(db.DateTime)
    
    # Ownership
    user_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='CASCADE'))
    team_id = db.Column(db.Integer, db.ForeignKey('team.id', ondelete='SET NULL'))
    template_id = db.Column(db.Integer, db.ForeignKey('email_template.id', ondelete='SET NULL'))
    
    # Relationships
    smtp_profile = db.relationship('SMTPServer', backref='campaigns')
    recipients = db.relationship('Recipient', backref='campaign', lazy='dynamic', cascade="all, delete-orphan", passive_deletes=True)
    tags = db.relationship('Tag', secondary=campaign_tags, backref='campaigns')
    template = db.relationship('EmailTemplate', backref='campaigns')
    
    def get_analytics(self):
        """Calculate campaign analytics."""
        total = self.total_recipients or 0
        sent = self.recipients.filter_by(status='Sent').count()
        opened = self.recipients.filter(Recipient.opened_at.isnot(None)).count()
        clicked = self.recipients.filter(Recipient.clicked_at.isnot(None)).count()
        bounced = self.recipients.filter_by(status='Bounced').count()
        failed = self.recipients.filter_by(status='Failed').count()
        
        # Use DB queries for efficient counts on large lists
        queued = self.recipients.filter_by(status='Queued').count()
        
        return {
            'total': total,
            'sent': sent,
            'queued': queued,
            'opened': opened,
            'clicked': clicked,
            'bounced': bounced,
            'failed': failed,
            'open_rate': round((opened / sent * 100), 2) if sent > 0 else 0,
            'click_rate': round((clicked / sent * 100), 2) if sent > 0 else 0,
        }
    
    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'subject': self.subject,
            'status': self.status,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'scheduled_at': self.scheduled_at.isoformat() if self.scheduled_at else None
        }

class Recipient(db.Model):
    __tablename__ = 'recipient'
    
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), index=True, nullable=False)
    data = db.Column(db.Text)  # JSON
    
    # Status
    status = db.Column(db.String(20), default='Queued', index=True)
    status_message = db.Column(db.String(255))
    
    # Campaign Reference
    campaign_id = db.Column(db.Integer, db.ForeignKey('campaign.id', ondelete='CASCADE'), index=True)
    
    # Details
    ab_version = db.Column(db.String(1))
    sent_at = db.Column(db.DateTime)
    smtp_profile_used_id = db.Column(db.Integer, db.ForeignKey('smtp_server.id', ondelete='SET NULL'))
    message_id = db.Column(db.String(255))
    
    # Engagement
    opened_at = db.Column(db.DateTime)
    clicked_at = db.Column(db.DateTime)
    open_count = db.Column(db.Integer, default=0)
    click_count = db.Column(db.Integer, default=0)
    
    # Retry
    attempts = db.Column(db.Integer, default=0)
    engagement_score = db.Column(db.Float, default=0.0)
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    # Relationships
    segments = db.relationship('Segment', secondary=recipient_segments, backref='recipients')
    
    def get_tracking_token(self, action, payload=None):
        s = Serializer(current_app.config['SECRET_KEY'])
        data = {'action': action, 'rid': self.id}
        if payload:
            data.update(payload)
        return s.dumps(data, salt='track')
    
    def to_dict(self):
        return {
            'id': self.id,
            'email': self.email,
            'status': self.status,
            'sent_at': self.sent_at.isoformat() if self.sent_at else None,
            'opened_at': self.opened_at.isoformat() if self.opened_at else None
        }

# ==================== OTHER MODELS ====================

class Tag(db.Model):
    __tablename__ = 'tag'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='SET NULL'))

class Segment(db.Model):
    __tablename__ = 'segment'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    rules = db.Column(db.Text)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='SET NULL'))

class Sequence(db.Model):
    __tablename__ = 'sequence'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100))
    user_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='SET NULL'))

class SequenceRecipient(db.Model):
    __tablename__ = 'sequence_recipient'
    id = db.Column(db.Integer, primary_key=True)
    sequence_id = db.Column(db.Integer, db.ForeignKey('sequence.id', ondelete='CASCADE'))
    email = db.Column(db.String(120), index=True)

class Suppression(db.Model):
    __tablename__ = 'suppression'
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, index=True, nullable=False)
    reason = db.Column(db.String(100))
    source = db.Column(db.String(50))
    user_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='SET NULL'))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class APIKey(db.Model):
    __tablename__ = 'api_key'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100))
    key_hash = db.Column(db.String(256), unique=True)
    key_prefix = db.Column(db.String(10))
    user_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='SET NULL'))
    scopes = db.Column(db.Text)
    is_active = db.Column(db.Boolean, default=True)
    last_used_at = db.Column(db.DateTime)
    request_count = db.Column(db.Integer, default=0)
    expires_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    @staticmethod
    def generate_key():
        return secrets.token_urlsafe(32)
    
    @staticmethod
    def hash_key(key):
        return hashlib.sha256(key.encode()).hexdigest()
    
    def set_key(self, key):
        self.key_prefix = key[:8]
        self.key_hash = self.hash_key(key)
    
    def verify_key(self, key):
        return self.key_hash == self.hash_key(key)
        
    def get_scopes(self):
        try:
            return json.loads(self.scopes)
        except:
            return []
            
    def is_valid(self):
        if not self.is_active: return False
        if self.expires_at and datetime.utcnow() > self.expires_at: return False
        return True

class Webhook(db.Model):
    __tablename__ = 'webhook'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100))
    url = db.Column(db.String(500))
    secret = db.Column(db.String(100))
    events = db.Column(db.Text)
    is_active = db.Column(db.Boolean, default=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='SET NULL'))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_triggered_at = db.Column(db.DateTime)
    success_count = db.Column(db.Integer, default=0)
    failure_count = db.Column(db.Integer, default=0)
    
    def generate_secret(self):
        self.secret = secrets.token_urlsafe(32)
    
    def get_events(self):
        try: return json.loads(self.events)
        except: return []
        
    def set_events(self, events):
        self.events = json.dumps(events)

class WebhookDelivery(db.Model):
    __tablename__ = 'webhook_delivery'
    id = db.Column(db.Integer, primary_key=True)
    webhook_id = db.Column(db.Integer, db.ForeignKey('webhook.id', ondelete='CASCADE'))
    event = db.Column(db.String(50))
    success = db.Column(db.Boolean)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class Notification(db.Model):
    __tablename__ = 'notification'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='CASCADE'))
    title = db.Column(db.String(200))
    message = db.Column(db.Text)
    type = db.Column(db.String(20), default='info')
    read = db.Column(db.Boolean, default=False)
    related_type = db.Column(db.String(50))
    related_id = db.Column(db.Integer)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class ActivityLog(db.Model):
    __tablename__ = 'activity_log'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='SET NULL'))
    team_id = db.Column(db.Integer, db.ForeignKey('team.id', ondelete='SET NULL'))
    action = db.Column(db.String(100))
    description = db.Column(db.Text)
    object_type = db.Column(db.String(50))
    object_id = db.Column(db.Integer)
    ip_address = db.Column(db.String(45))
    user_agent = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class GlobalSettings(db.Model):
    __tablename__ = 'global_settings'
    id = db.Column(db.Integer, primary_key=True)
    burner_domain = db.Column(db.String(150))
    lure_path = db.Column(db.String(100))
    template_pdf_path = db.Column(db.String(255))
    warmup_schedule = db.Column(db.Text)
    default_throttle_amount = db.Column(db.Integer, default=20)
    default_throttle_delay = db.Column(db.Integer, default=60)
    default_tracking_domain = db.Column(db.String(150))
    ai_provider = db.Column(db.String(20), default='openai')
    openai_api_key = db.Column(db.String(255))
    local_ai_url = db.Column(db.String(255))
    local_ai_model = db.Column(db.String(50))
    gdpr_enabled = db.Column(db.Boolean, default=True)
    data_retention_days = db.Column(db.Integer, default=365)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)

class UserSettings(db.Model):
    __tablename__ = 'user_settings'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='CASCADE'), unique=True)
    email_notifications = db.Column(db.Boolean, default=True)

class DailyStats(db.Model):
    __tablename__ = 'daily_stats'
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='SET NULL'))
    campaign_id = db.Column(db.Integer, db.ForeignKey('campaign.id', ondelete='CASCADE'))
    smtp_profile_id = db.Column(db.Integer, db.ForeignKey('smtp_server.id', ondelete='SET NULL'))
    emails_sent = db.Column(db.Integer, default=0)
    unique_opens = db.Column(db.Integer, default=0)
    unique_clicks = db.Column(db.Integer, default=0)

class HourlyStats(db.Model):
    __tablename__ = 'hourly_stats'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='SET NULL'))
    day_of_week = db.Column(db.Integer)
    hour_of_day = db.Column(db.Integer)
    total_sends = db.Column(db.Integer, default=0)
    total_opens = db.Column(db.Integer, default=0)
    total_clicks = db.Column(db.Integer, default=0)

class ConsentRecord(db.Model):
    __tablename__ = 'consent_record'
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), index=True)
    consented = db.Column(db.Boolean, default=True)
    source = db.Column(db.String(100))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
