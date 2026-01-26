from datetime import datetime, timedelta
from werkzeug.security import generate_password_hash, check_password_hash
from flask_login import UserMixin
from itsdangerous import URLSafeTimedSerializer as Serializer
from flask import current_app
# Correctly import db and the login_manager instance
from app import db, login_manager
from cryptography.fernet import Fernet
import base64
import hashlib
import json
import secrets
import enum


# Use the imported login_manager instance for the user_loader decorator
@login_manager.user_loader
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


class RecipientStatus(enum.Enum):
    QUEUED = 'Queued'
    SENDING = 'Sending'
    SENT = 'Sent'
    OPENED = 'Opened'
    CLICKED = 'Clicked'
    REPLIED = 'Replied'
    BOUNCED = 'Bounced'
    FAILED = 'Failed'
    UNSUBSCRIBED = 'Unsubscribed'
    SUPPRESSED = 'Suppressed'
    INVALID = 'Invalid'


class WebhookEvent(enum.Enum):
    EMAIL_SENT = 'email.sent'
    EMAIL_OPENED = 'email.opened'
    EMAIL_CLICKED = 'email.clicked'
    EMAIL_BOUNCED = 'email.bounced'
    EMAIL_UNSUBSCRIBED = 'email.unsubscribed'
    CAMPAIGN_STARTED = 'campaign.started'
    CAMPAIGN_COMPLETED = 'campaign.completed'
    CAMPAIGN_FAILED = 'campaign.failed'


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
    # Added explicit cascading to prevent FK errors when deleting users
    campaigns = db.relationship('Campaign', backref='author', lazy='dynamic', foreign_keys='Campaign.user_id')
    
    api_keys = db.relationship('APIKey', backref='user', lazy='dynamic', cascade="all, delete-orphan", passive_deletes=True)
    notifications = db.relationship('Notification', backref='user', lazy='dynamic', cascade="all, delete-orphan", passive_deletes=True)
    activity_logs = db.relationship('ActivityLog', backref='user', lazy='dynamic')
    
    def set_password(self, password):
        self.password_hash = generate_password_hash(password)
    
    def check_password(self, password):
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
    
    def set_preferences(self, prefs):
        self.preferences = json.dumps(prefs)
    
    def get_full_name(self):
        if self.first_name and self.last_name:
            return f"{self.first_name} {self.last_name}"
        return self.username
    
    def to_dict(self):
        return {
            'id': self.id,
            'username': self.username,
            'email': self.email,
            'role': self.role if isinstance(self.role, str) else self.role.value,
            'is_active': self.is_active,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'two_factor_enabled': self.two_factor_enabled,
            'full_name': self.get_full_name()
        }


class Team(db.Model):
    __tablename__ = 'team'
    
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    slug = db.Column(db.String(100), unique=True, nullable=False)
    description = db.Column(db.Text)
    owner_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='SET NULL'))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    # White Label Settings
    custom_logo_url = db.Column(db.String(255))
    custom_domain = db.Column(db.String(255))
    primary_color = db.Column(db.String(7), default='#007bff')
    secondary_color = db.Column(db.String(7), default='#6c757d')
    
    # Settings
    settings = db.Column(db.Text)  # JSON
    
    # Relationships
    owner = db.relationship('User', backref='owned_teams')
    members = db.relationship('User', secondary=team_members, backref='teams')
    campaigns = db.relationship('Campaign', backref='team', lazy='dynamic')
    
    def get_settings(self):
        if self.settings:
            try:
                return json.loads(self.settings)
            except:
                pass
        return {}
    
    def set_settings(self, settings_dict):
        self.settings = json.dumps(settings_dict)
    
    def add_member(self, user, role='editor'):
        if user not in self.members:
            self.members.append(user)
    
    def remove_member(self, user):
        if user in self.members:
            self.members.remove(user)


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
    team_id = db.Column(db.Integer, db.ForeignKey('team.id', ondelete='SET NULL'))
    
    # Status & Limits
    is_active = db.Column(db.Boolean, default=True)
    daily_limit = db.Column(db.Integer, default=500)
    hourly_limit = db.Column(db.Integer, default=100)
    sent_today = db.Column(db.Integer, default=0)
    sent_this_hour = db.Column(db.Integer, default=0)
    last_reset_date = db.Column(db.Date, default=datetime.utcnow().date)
    last_hour_reset = db.Column(db.DateTime, default=datetime.utcnow)
    priority = db.Column(db.Integer, default=1)
    
    # Warmup
    warmup_enabled = db.Column(db.Boolean, default=False)
    warmup_start_date = db.Column(db.Date)
    warmup_current_day = db.Column(db.Integer, default=0)
    
    # Health & Reputation
    last_test_at = db.Column(db.DateTime)
    last_test_result = db.Column(db.Boolean)
    reputation_score = db.Column(db.Float, default=100.0)
    bounce_rate = db.Column(db.Float, default=0.0)
    complaint_rate = db.Column(db.Float, default=0.0)
    
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
    
    def reset_daily_count_if_needed(self):
        today = datetime.utcnow().date()
        if self.last_reset_date != today:
            self.sent_today = 0
            self.last_reset_date = today
            return True
        return False
    
    def reset_hourly_count_if_needed(self):
        now = datetime.utcnow()
        if self.last_hour_reset and (now - self.last_hour_reset).total_seconds() >= 3600:
            self.sent_this_hour = 0
            self.last_hour_reset = now
            return True
        return False
    
    def can_send(self):
        self.reset_daily_count_if_needed()
        self.reset_hourly_count_if_needed()
        
        if not self.is_active:
            return False
        
        if self.warmup_enabled:
            warmup_limit = self.get_warmup_limit()
            if self.sent_today >= warmup_limit:
                return False
        else:
            if self.sent_today >= self.daily_limit:
                return False
            if self.hourly_limit and self.sent_this_hour >= self.hourly_limit:
                return False
        
        return True
    
    def get_warmup_limit(self):
        if not self.warmup_enabled or not self.warmup_start_date:
            return self.daily_limit
        
        days_since_start = (datetime.utcnow().date() - self.warmup_start_date).days
        warmup_schedule = current_app.config.get('WARMUP_SCHEDULE', [10, 25, 50, 100, 200, 400, 800, 1500])
        
        if days_since_start >= len(warmup_schedule):
            return self.daily_limit
        
        return warmup_schedule[days_since_start]
    
    def increment_sent_count(self):
        self.sent_today += 1
        self.sent_this_hour += 1


class EmailTemplate(db.Model):
    __tablename__ = 'email_template'
    
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text)
    category = db.Column(db.String(50))
    thumbnail_url = db.Column(db.String(255))
    html_content = db.Column(db.Text, nullable=False)
    json_content = db.Column(db.Text)  # For drag-drop builder
    
    # Ownership
    user_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='SET NULL'))
    team_id = db.Column(db.Integer, db.ForeignKey('team.id', ondelete='SET NULL'))
    is_public = db.Column(db.Boolean, default=False)
    
    # Usage stats
    usage_count = db.Column(db.Integer, default=0)
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    def get_json_content(self):
        if self.json_content:
            try:
                return json.loads(self.json_content)
            except:
                pass
        return {}
    
    def set_json_content(self, content):
        self.json_content = json.dumps(content)


# ==================== CAMPAIGN MODELS ====================

class Campaign(db.Model):
    __tablename__ = 'campaign'
    
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(140), nullable=False)
    subject = db.Column(db.String(255))
    preheader = db.Column(db.String(255))
    body_html = db.Column(db.Text)
    body_plain = db.Column(db.Text)
    body_json = db.Column(db.Text)  # For drag-drop builder
    
    # A/B Testing
    ab_testing_enabled = db.Column(db.Boolean, default=False)
    subject_b = db.Column(db.String(255))
    body_b = db.Column(db.Text)
    ab_split_ratio = db.Column(db.Integer, default=50)
    ab_winner_criteria = db.Column(db.String(20), default='opens')  # opens, clicks, revenue
    ab_auto_select_winner = db.Column(db.Boolean, default=False)
    ab_winner_wait_hours = db.Column(db.Integer, default=24)
    ab_winner = db.Column(db.String(1))  # 'A' or 'B'
    
    # Secure Redirector
    burner_domain = db.Column(db.String(100))
    lure_path = db.Column(db.String(100))
    
    # Sending Config
    throttle_amount = db.Column(db.Integer, default=20)
    throttle_delay = db.Column(db.Integer, default=60)
    parallel_workers = db.Column(db.Integer, default=10)
    
    # SMTP Configuration
    smtp_rotation_enabled = db.Column(db.Boolean, default=False)
    smtp_profile_id = db.Column(db.Integer, db.ForeignKey('smtp_server.id', ondelete='SET NULL'))
    
    # Warmup & Safety
    warmup_mode = db.Column(db.Boolean, default=False)
    
    # Tracking
    tracking_enabled = db.Column(db.Boolean, default=True)
    track_opens = db.Column(db.Boolean, default=True)
    track_clicks = db.Column(db.Boolean, default=True)
    google_analytics_enabled = db.Column(db.Boolean, default=False)
    utm_source = db.Column(db.String(50))
    utm_medium = db.Column(db.String(50))
    utm_campaign = db.Column(db.String(50))
    
    # Scheduling
    scheduled_at = db.Column(db.DateTime, nullable=True)
    send_timezone = db.Column(db.String(50), default='UTC')
    
    # Smart Send Time
    smart_send_enabled = db.Column(db.Boolean, default=False)
    
    # Attachments
    attachments = db.Column(db.Text)  # JSON array
    
    # Status
    status = db.Column(db.String(20), default='Draft')
    
    # Progress Tracking
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
    
    # Template
    template_id = db.Column(db.Integer, db.ForeignKey('email_template.id', ondelete='SET NULL'))
    
    # Approval Workflow
    requires_approval = db.Column(db.Boolean, default=False)
    approved_by_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='SET NULL'))
    approved_at = db.Column(db.DateTime)
    
    # Relationships
    smtp_profile = db.relationship('SMTPServer', backref='campaigns')
    recipients = db.relationship(
        'Recipient',
        backref='campaign',
        lazy='dynamic',
        cascade="all, delete-orphan",
        passive_deletes=True
    )
    tags = db.relationship('Tag', secondary=campaign_tags, backref='campaigns')
    template = db.relationship('EmailTemplate', backref='campaigns')
    approved_by = db.relationship('User', foreign_keys=[approved_by_id])
    
    def get_attachments(self):
        if self.attachments:
            try:
                return json.loads(self.attachments)
            except:
                return []
        return []
    
    def set_attachments(self, paths):
        self.attachments = json.dumps(paths) if paths else None
    
    def get_body_json(self):
        if self.body_json:
            try: 
                return json.loads(self.body_json)
            except:
                pass
        return {}
    
    def set_body_json(self, content):
        self.body_json = json.dumps(content)
    
    def get_analytics(self):
        """Calculate campaign analytics."""
        total = self.recipients.count()
        sent = self.recipients.filter_by(status='Sent').count()
        opened = self.recipients.filter(Recipient.opened_at.isnot(None)).count()
        clicked = self.recipients.filter(Recipient.clicked_at.isnot(None)).count()
        bounced = self.recipients.filter_by(status='Bounced').count()
        failed = self.recipients.filter_by(status='Failed').count()
        unsubscribed = self.recipients.filter_by(status='Unsubscribed').count()
        
        return {
            'total': total,
            'sent': sent,
            'opened': opened,
            'clicked': clicked,
            'bounced': bounced,
            'failed': failed,
            'unsubscribed': unsubscribed,
            'open_rate': round((opened / sent * 100), 2) if sent > 0 else 0,
            'click_rate': round((clicked / sent * 100), 2) if sent > 0 else 0,
            'bounce_rate': round((bounced / sent * 100), 2) if sent > 0 else 0,
            'unsubscribe_rate': round((unsubscribed / sent * 100), 2) if sent > 0 else 0
        }
    
    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'subject': self.subject,
            'status': self.status,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'scheduled_at': self.scheduled_at.isoformat() if self.scheduled_at else None,
            'analytics': self.get_analytics()
        }


class Recipient(db.Model):
    __tablename__ = 'recipient'
    
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), index=True, nullable=False)
    data = db.Column(db.Text)  # JSON with personalization data
    
    # Status
    status = db.Column(db.String(20), default='Queued', index=True)
    status_message = db.Column(db.String(255))
    
    # Campaign Reference
    campaign_id = db.Column(db.Integer, db.ForeignKey('campaign.id', ondelete='CASCADE'), index=True)
    
    # A/B Testing
    ab_version = db.Column(db.String(1))
    
    # Sending Details
    sent_at = db.Column(db.DateTime)
    smtp_profile_used_id = db.Column(db.Integer, db.ForeignKey('smtp_server.id', ondelete='SET NULL'))
    message_id = db.Column(db.String(255))
    
    # Engagement Tracking
    opened_at = db.Column(db.DateTime)
    clicked_at = db.Column(db.DateTime)
    replied_at = db.Column(db.DateTime)
    unsubscribed_at = db.Column(db.DateTime)
    bounced_at = db.Column(db.DateTime)
    
    # Engagement Counts
    open_count = db.Column(db.Integer, default=0)
    click_count = db.Column(db.Integer, default=0)
    
    # Clicked Links
    clicked_links = db.Column(db.Text)  # JSON array
    
    # Retry Info
    attempts = db.Column(db.Integer, default=0)
    last_attempt_at = db.Column(db.DateTime)
    next_retry_at = db.Column(db.DateTime)
    
    # Tracking Info
    user_agent = db.Column(db.String(255))
    ip_address = db.Column(db.String(45))
    geo_location = db.Column(db.String(100))
    device_type = db.Column(db.String(20))
    email_client = db.Column(db.String(50))
    
    # Lead Scoring
    engagement_score = db.Column(db.Float, default=0.0)
    
    # Smart Send Time
    optimal_send_time = db.Column(db.Time)
    timezone = db.Column(db.String(50))
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    # Relationships
    smtp_profile_used = db.relationship('SMTPServer', backref='sent_emails')
    segments = db.relationship('Segment', secondary=recipient_segments, backref='recipients')
    
    def get_data(self):
        if self.data:
            try:
                return json.loads(self.data)
            except:
                pass
        return {}
    
    def set_data(self, data_dict):
        self.data = json.dumps(data_dict)
    
    def get_clicked_links(self):
        if self.clicked_links:
            try:
                return json.loads(self.clicked_links)
            except:
                pass
        return []
    
    def add_clicked_link(self, url):
        links = self.get_clicked_links()
        if url not in links: 
            links.append(url)
        self.clicked_links = json.dumps(links)
    
    def get_tracking_token(self, action, payload=None):
        s = Serializer(current_app.config['SECRET_KEY'])
        data = {'action': action, 'rid': self.id}
        if payload:
            data.update(payload)
        return s.dumps(data, salt='track')
    
    def calculate_engagement_score(self):
        """Calculate engagement score based on actions."""
        score = 0.0
        
        if self.status == 'Sent':
            score += 10
        if self.opened_at:
            score += 20 + (min(self.open_count, 5) * 5)
        if self.clicked_at:
            score += 30 + (min(self.click_count, 10) * 3)
        if self.replied_at:
            score += 50
        if self.status == 'Bounced': 
            score -= 50
        if self.status == 'Unsubscribed':
            score -= 100
        
        # Recency bonus
        if self.opened_at: 
            days_since_open = (datetime.utcnow() - self.opened_at).days
            if days_since_open < 7:
                score += 10
            elif days_since_open < 30:
                score += 5
        
        self.engagement_score = max(0, min(100, score))
        return self.engagement_score
    
    def to_dict(self):
        return {
            'id': self.id,
            'email': self.email,
            'status': self.status,
            'sent_at': self.sent_at.isoformat() if self.sent_at else None,
            'opened_at': self.opened_at.isoformat() if self.opened_at else None,
            'clicked_at': self.clicked_at.isoformat() if self.clicked_at else None,
            'open_count': self.open_count,
            'click_count': self.click_count,
            'engagement_score': self.engagement_score,
            'data': self.get_data()
        }


# ==================== SEGMENTATION & TAGGING ====================

class Tag(db.Model):
    __tablename__ = 'tag'
    
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False)
    color = db.Column(db.String(7), default='#007bff')
    user_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='SET NULL'))
    team_id = db.Column(db.Integer, db.ForeignKey('team.id', ondelete='SET NULL'))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Segment(db.Model):
    __tablename__ = 'segment'
    
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text)
    
    # Segment Type
    is_dynamic = db.Column(db.Boolean, default=False)  # Auto-updated based on rules
    is_ai_generated = db.Column(db.Boolean, default=False)
    
    # Rules for dynamic segments (JSON)
    rules = db.Column(db.Text)
    
    # Ownership
    user_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='SET NULL'))
    team_id = db.Column(db.Integer, db.ForeignKey('team.id', ondelete='SET NULL'))
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    def get_rules(self):
        if self.rules:
            try:
                return json.loads(self.rules)
            except:
                pass
        return {}
    
    def set_rules(self, rules_dict):
        self.rules = json.dumps(rules_dict)


# ==================== AUTOMATION & SEQUENCES ====================

class Sequence(db.Model):
    __tablename__ = 'sequence'
    
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text)
    
    # Status
    is_active = db.Column(db.Boolean, default=False)
    
    # Steps (JSON workflow definition)
    steps = db.Column(db.Text)
    
    # Trigger settings
    trigger_type = db.Column(db.String(50))  # manual, tag_added, form_submitted, etc.
    trigger_config = db.Column(db.Text)  # JSON
    
    # Ownership
    user_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='CASCADE'))
    team_id = db.Column(db.Integer, db.ForeignKey('team.id', ondelete='SET NULL'))
    
    # Stats
    total_enrolled = db.Column(db.Integer, default=0)
    total_completed = db.Column(db.Integer, default=0)
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    # Added cascade delete to recipients so they are removed if sequence is deleted
    recipients = db.relationship(
        'SequenceRecipient', 
        backref='sequence', 
        lazy='dynamic', 
        cascade="all, delete-orphan", 
        passive_deletes=True
    )
    
    def get_steps(self):
        if self.steps:
            try:
                return json.loads(self.steps)
            except:
                pass
        return []
    
    def set_steps(self, steps_list):
        self.steps = json.dumps(steps_list) if steps_list else '[]'
    
    def get_trigger_config(self):
        if self.trigger_config:
            try:
                return json.loads(self.trigger_config)
            except:
                pass
        return {}


class SequenceRecipient(db.Model):
    __tablename__ = 'sequence_recipient'
    
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), index=True, nullable=False)
    data = db.Column(db.Text)  # JSON
    
    # This was the cause of the "cannot drop table" error
    # Changed to CASCADE to allow smooth migrations
    sequence_id = db.Column(db.Integer, db.ForeignKey('sequence.id', ondelete='CASCADE'))
    
    # Progress
    current_step = db.Column(db.Integer, default=0)
    status = db.Column(db.String(20), default='Active')  # Active, Paused, Completed, Exited
    
    # Timing
    enrolled_at = db.Column(db.DateTime, default=datetime.utcnow)
    next_step_at = db.Column(db.DateTime)
    completed_at = db.Column(db.DateTime)
    last_action_at = db.Column(db.DateTime)
    
    # History (JSON array of completed actions)
    history = db.Column(db.Text)
    
    def get_history(self):
        if self.history:
            try:
                return json.loads(self.history)
            except:
                pass
        return []
    
    def add_history_entry(self, entry):
        history = self.get_history()
        entry['timestamp'] = datetime.utcnow().isoformat()
        history.append(entry)
        self.history = json.dumps(history)


# ==================== SUPPRESSION & COMPLIANCE ====================

class Suppression(db.Model):
    __tablename__ = 'suppression'
    
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, index=True, nullable=False)
    reason = db.Column(db.String(100))
    source = db.Column(db.String(50))  # manual, unsubscribe, bounce, complaint
    
    user_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='SET NULL'))
    team_id = db.Column(db.Integer, db.ForeignKey('team.id', ondelete='SET NULL'))
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    
    # GDPR/Compliance
    gdpr_request_id = db.Column(db.String(100))
    data_deleted = db.Column(db.Boolean, default=False)


class ConsentRecord(db.Model):
    """GDPR consent tracking."""
    __tablename__ = 'consent_record'
    
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), index=True, nullable=False)
    consent_type = db.Column(db.String(50))  # marketing, transactional, etc.
    consented = db.Column(db.Boolean, default=True)
    source = db.Column(db.String(100))  # Form name, import file, etc.
    ip_address = db.Column(db.String(45))
    user_agent = db.Column(db.String(255))
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# ==================== API & WEBHOOKS ====================

class APIKey(db.Model):
    __tablename__ = 'api_key'
    
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    key_hash = db.Column(db.String(256), unique=True, nullable=False)
    key_prefix = db.Column(db.String(10))  # First 8 chars for identification
    
    user_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='CASCADE'))
    team_id = db.Column(db.Integer, db.ForeignKey('team.id', ondelete='SET NULL'))
    
    # Permissions
    scopes = db.Column(db.Text)  # JSON array of allowed scopes
    
    # Rate Limiting
    rate_limit = db.Column(db.Integer, default=1000)  # Requests per hour
    
    # Status
    is_active = db.Column(db.Boolean, default=True)
    
    # Usage
    last_used_at = db.Column(db.DateTime)
    request_count = db.Column(db.Integer, default=0)
    
    # Expiry
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
        if self.scopes:
            try:
                return json.loads(self.scopes)
            except:
                pass
        return ['read', 'write']
    
    def set_scopes(self, scopes_list):
        self.scopes = json.dumps(scopes_list)
    
    def is_valid(self):
        if not self.is_active:
            return False
        if self.expires_at and datetime.utcnow() > self.expires_at:
            return False
        return True


class Webhook(db.Model):
    __tablename__ = 'webhook'
    
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    url = db.Column(db.String(500), nullable=False)
    secret = db.Column(db.String(100))
    
    user_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='SET NULL'))
    team_id = db.Column(db.Integer, db.ForeignKey('team.id', ondelete='SET NULL'))
    
    # Events to trigger
    events = db.Column(db.Text)  # JSON array of WebhookEvent values
    
    # Status
    is_active = db.Column(db.Boolean, default=True)
    
    # Stats
    last_triggered_at = db.Column(db.DateTime)
    success_count = db.Column(db.Integer, default=0)
    failure_count = db.Column(db.Integer, default=0)
    last_error = db.Column(db.Text)
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    def get_events(self):
        if self.events:
            try:
                return json.loads(self.events)
            except:
                pass
        return []
    
    def set_events(self, events_list):
        self.events = json.dumps(events_list)
    
    def generate_secret(self):
        self.secret = secrets.token_urlsafe(32)
        return self.secret


class WebhookDelivery(db.Model):
    """Log of webhook delivery attempts."""
    __tablename__ = 'webhook_delivery'
    
    id = db.Column(db.Integer, primary_key=True)
    webhook_id = db.Column(db.Integer, db.ForeignKey('webhook.id', ondelete='CASCADE'))
    
    event = db.Column(db.String(50))
    payload = db.Column(db.Text)  # JSON
    
    # Response
    status_code = db.Column(db.Integer)
    response_body = db.Column(db.Text)
    response_time_ms = db.Column(db.Integer)
    
    # Status
    success = db.Column(db.Boolean, default=False)
    error_message = db.Column(db.Text)
    
    # Retry
    attempt_number = db.Column(db.Integer, default=1)
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    webhook = db.relationship('Webhook', backref='deliveries')


# ==================== NOTIFICATIONS & ACTIVITY ====================

class Notification(db.Model):
    __tablename__ = 'notification'
    
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='CASCADE'))
    
    title = db.Column(db.String(200), nullable=False)
    message = db.Column(db.Text)
    type = db.Column(db.String(20), default='info')  # info, success, warning, error
    
    # Related object
    related_type = db.Column(db.String(50))  # campaign, recipient, etc.
    related_id = db.Column(db.Integer)
    
    # Status
    read = db.Column(db.Boolean, default=False)
    read_at = db.Column(db.DateTime)
    
    # Delivery
    email_sent = db.Column(db.Boolean, default=False)
    push_sent = db.Column(db.Boolean, default=False)
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    def mark_as_read(self):
        self.read = True
        self.read_at = datetime.utcnow()


class ActivityLog(db.Model):
    __tablename__ = 'activity_log'
    
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='SET NULL'))
    team_id = db.Column(db.Integer, db.ForeignKey('team.id', ondelete='SET NULL'))
    
    action = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text)
    
    # Related object
    object_type = db.Column(db.String(50))
    object_id = db.Column(db.Integer)
    
    # Context
    ip_address = db.Column(db.String(45))
    user_agent = db.Column(db.String(255))
    
    # Data changes (JSON)
    old_values = db.Column(db.Text)
    new_values = db.Column(db.Text)
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)


# ==================== SETTINGS ====================

class GlobalSettings(db.Model):
    __tablename__ = 'global_settings'
    
    id = db.Column(db.Integer, primary_key=True)
    
    # Secure Redirector
    burner_domain = db.Column(db.String(150))
    lure_path = db.Column(db.String(100))
    template_pdf_path = db.Column(db.String(255))
    
    # Warmup
    warmup_schedule = db.Column(db.Text)  # JSON
    
    # Throttling Defaults
    default_throttle_amount = db.Column(db.Integer, default=20)
    default_throttle_delay = db.Column(db.Integer, default=60)
    
    # Tracking
    default_tracking_domain = db.Column(db.String(150))
    
    # AI Settings
    ai_provider = db.Column(db.String(20), default='openai')
    openai_api_key_encrypted = db.Column(db.String(512))
    local_ai_url = db.Column(db.String(255))
    
    # Compliance
    gdpr_enabled = db.Column(db.Boolean, default=True)
    data_retention_days = db.Column(db.Integer, default=365)
    
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class UserSettings(db.Model):
    __tablename__ = 'user_settings'
    
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='CASCADE'), unique=True)
    
    # Notification Preferences
    email_notifications = db.Column(db.Boolean, default=True)
    push_notifications = db.Column(db.Boolean, default=True)
    notify_campaign_complete = db.Column(db.Boolean, default=True)
    notify_high_bounce_rate = db.Column(db.Boolean, default=True)
    notify_daily_summary = db.Column(db.Boolean, default=False)
    
    # UI Preferences
    theme = db.Column(db.String(20), default='light')  # light, dark, auto
    language = db.Column(db.String(10), default='en')
    date_format = db.Column(db.String(20), default='YYYY-MM-DD')
    time_format = db.Column(db.String(10), default='24h')
    
    # Editor Preferences
    default_editor = db.Column(db.String(20), default='visual')  # visual, code
    
    user = db.relationship('User', backref=db.backref('settings', uselist=False, cascade="all, delete-orphan", passive_deletes=True))


# ==================== ANALYTICS & REPORTING ====================

class DailyStats(db.Model):
    """Aggregated daily statistics for faster analytics."""
    __tablename__ = 'daily_stats'
    
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, index=True, nullable=False)
    
    user_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='SET NULL'))
    team_id = db.Column(db.Integer, db.ForeignKey('team.id', ondelete='SET NULL'))
    campaign_id = db.Column(db.Integer, db.ForeignKey('campaign.id', ondelete='CASCADE'))
    smtp_profile_id = db.Column(db.Integer, db.ForeignKey('smtp_server.id', ondelete='SET NULL'))
    
    # Counts
    emails_sent = db.Column(db.Integer, default=0)
    emails_delivered = db.Column(db.Integer, default=0)
    emails_opened = db.Column(db.Integer, default=0)
    unique_opens = db.Column(db.Integer, default=0)
    emails_clicked = db.Column(db.Integer, default=0)
    unique_clicks = db.Column(db.Integer, default=0)
    emails_bounced = db.Column(db.Integer, default=0)
    emails_complained = db.Column(db.Integer, default=0)
    emails_unsubscribed = db.Column(db.Integer, default=0)
    
    __table_args__ = (
        db.UniqueConstraint('date', 'user_id', 'campaign_id', name='unique_daily_stats'),
    )


class HourlyStats(db.Model):
    """Hourly engagement data for send time optimization."""
    __tablename__ = 'hourly_stats'
    
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='SET NULL'))
    
    day_of_week = db.Column(db.Integer)  # 0=Mon, 6=Sun
    hour_of_day = db.Column(db.Integer)  # 0-23
    
    total_sends = db.Column(db.Integer, default=0)
    total_opens = db.Column(db.Integer, default=0)
    total_clicks = db.Column(db.Integer, default=0)
