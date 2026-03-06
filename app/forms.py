from flask_wtf import FlaskForm
from flask_wtf.file import FileField, FileAllowed
from wtforms import (
    StringField, PasswordField, BooleanField, SubmitField,
    TextAreaField, IntegerField, SelectField, DateField, TimeField,
    MultipleFileField
)
from wtforms.validators import (
    DataRequired, ValidationError, Email, EqualTo, Length,
    Optional, NumberRange
)

# Try to import User model, but prevent build error if running in isolation/testing
try:
    from app.models import User
except ImportError:
    # Placeholder for User if app.models doesn't exist in the current environment
    User = None

# ==================== AUTH FORMS ====================

class LoginForm(FlaskForm):
    """Form for user login."""
    username = StringField('Username', validators=[DataRequired()])
    password = PasswordField('Password', validators=[DataRequired()])
    remember_me = BooleanField('Remember Me')
    submit = SubmitField('Sign In')


class RegistrationForm(FlaskForm):
    """Form for user registration."""
    username = StringField('Username', validators=[DataRequired(), Length(min=3, max=64)])
    email = StringField('Email', validators=[DataRequired(), Email()])
    password = PasswordField('Password', validators=[DataRequired(), Length(min=8)])
    password2 = PasswordField(
        'Repeat Password', validators=[DataRequired(), EqualTo('password')]
    )
    submit = SubmitField('Register')

    def validate_username(self, username):
        if User:
            user = User.query.filter_by(username=username.data).first()
            if user is not None:
                raise ValidationError('Please use a different username.')

    def validate_email(self, email):
        if User:
            user = User.query.filter_by(email=email.data).first()
            if user is not None:
                raise ValidationError('Please use a different email address.')


class EditProfileForm(FlaskForm):
    """Form for editing user profile."""
    username = StringField('Username', validators=[DataRequired()])
    email = StringField('Email', validators=[DataRequired(), Email()])
    submit = SubmitField('Submit')

    def __init__(self, original_username, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.original_username = original_username

    def validate_username(self, username):
        if username.data != self.original_username and User:
            user = User.query.filter_by(username=self.username.data).first()
            if user is not None:
                raise ValidationError('Please use a different username.')


# ==================== CAMPAIGN & SETTINGS FORMS ====================

class NewCampaignForm(FlaskForm):
    """Form for creating a new campaign."""
    campaign_name = StringField('Campaign Name', validators=[DataRequired()])
    subject = StringField('Subject Line', validators=[DataRequired()])
    body_html = TextAreaField('HTML Body', validators=[DataRequired()])
    recipients_file = FileField('Recipients File (CSV or TXT)', validators=[
        DataRequired(),
        FileAllowed(['csv', 'txt'], 'CSV or TXT files only!')
    ])
    smtp_profile_id = SelectField('Mailer Profile', coerce=int, validators=[DataRequired()])

    # A/B Testing
    ab_testing_enabled = BooleanField('Enable A/B Testing', default=False)
    subject_b = StringField('Subject Line B', validators=[Optional()])
    body_b = TextAreaField('HTML Body B', validators=[Optional()])
    ab_split_ratio = IntegerField('Split Ratio (A %)', default=50, validators=[NumberRange(min=1, max=99)])

    # Secure Redirector
    burner_domain = StringField('Burner Domain', validators=[Optional()])
    lure_path = StringField('Lure Path', validators=[Optional()])

    # Throttling
    throttle_amount = IntegerField('Batch Size', default=20, validators=[DataRequired(), NumberRange(min=1)])
    throttle_delay = IntegerField('Delay (seconds)', default=60, validators=[DataRequired(), NumberRange(min=1)])

    # Attachments
    attachments = MultipleFileField('Attachments', validators=[Optional()])

    # Options
    tracking_enabled = BooleanField('Enable Tracking', default=True)
    smtp_rotation_enabled = BooleanField('SMTP Rotation', default=False)
    warmup_mode = BooleanField('Warmup Mode', default=False)

    # Scheduling
    scheduled_date = DateField('Scheduled Date', validators=[Optional()])
    scheduled_time = TimeField('Scheduled Time', validators=[Optional()])

    submit = SubmitField('Create Campaign')


class SMTPServerForm(FlaskForm):
    """Form for adding/editing SMTP servers."""
    profile_id = IntegerField('Profile ID', validators=[Optional()])
    name = StringField('Profile Name', validators=[DataRequired()])
    server = StringField('SMTP Server', validators=[DataRequired()])
    port = IntegerField('Port', validators=[DataRequired(), NumberRange(min=1, max=65535)])
    username = StringField('Username', validators=[DataRequired()])
    password = PasswordField('Password', validators=[Optional()])
    sender_name = StringField('Sender Name', validators=[Optional()])
    sender_email = StringField('Sender Email', validators=[Optional(), Email()])
    cc_emails = StringField('CC Emails', validators=[Optional()])
    bcc_emails = StringField('BCC Emails', validators=[Optional()])
    use_tls = BooleanField('Use TLS', default=True)
    use_ssl = BooleanField('Use SSL', default=False)
    is_active = BooleanField('Active', default=True)
    daily_limit = IntegerField('Daily Limit', default=500, validators=[DataRequired()])
    priority = IntegerField('Priority', default=1, validators=[DataRequired(), NumberRange(min=1)])

    # IMAP Settings
    imap_server = StringField('IMAP Server', validators=[Optional()])
    imap_port = IntegerField('IMAP Port', default=993, validators=[Optional()])
    imap_username = StringField('IMAP Username', validators=[Optional()])
    imap_password = PasswordField('IMAP Password', validators=[Optional()])

    submit = SubmitField('Save Profile')


class SuppressionForm(FlaskForm):
    """Form for adding an email to the suppression list."""
    email = StringField('Email Address', validators=[DataRequired(), Email()])
    reason = StringField('Reason', validators=[Optional(), Length(max=100)])
    submit = SubmitField('Add to List')


class GlobalSettingsForm(FlaskForm):
    """Form for managing global application settings."""
    burner_domain = StringField('Burner Domain', validators=[Optional()])
    lure_path = StringField('Lure Path', validators=[Optional()])
    template_pdf = FileField('Template PDF', validators=[
        Optional(),
        FileAllowed(['pdf'], 'PDF files only!')
    ])
    # Explicitly set label to None if not provided, though WTForms handles validators-only kwarg
    remove_pdf = StringField('Remove PDF', validators=[Optional()])

    default_throttle_amount = IntegerField('Default Batch Size', validators=[DataRequired()])
    default_throttle_delay = IntegerField('Default Delay (seconds)', validators=[DataRequired()])

    warmup_schedule = TextAreaField('Warmup Schedule (JSON)', validators=[DataRequired()])

    openai_api_key = PasswordField('OpenAI API Key', validators=[Optional()])
    local_ai_url = StringField('Local AI URL', validators=[Optional()])
    local_ai_model = StringField('Local AI Model', default='llama3', validators=[Optional()])

    submit = SubmitField('Save Settings')


# ==================== FORMS MOVED FROM ROUTES ====================

class DeliverabilityForm(FlaskForm):
    domain_ip = StringField('Domain or IP', validators=[DataRequired()])
    check_auth = SubmitField('Check Authentication')
    check_blacklist = SubmitField('Check Blacklist')


class SMTPProfileForm(FlaskForm):
    name = StringField('Profile Name', validators=[DataRequired()])
    server = StringField('SMTP Server', validators=[DataRequired()])
    port = StringField('Port', default='587')
    username = StringField('Username', validators=[DataRequired()])
    password = StringField('Password')
    sender_name = StringField('Sender Name')
    sender_email = StringField('Sender Email', validators=[Optional(), Email()])
    use_tls = BooleanField('Use TLS', default=True)
    use_ssl = BooleanField('Use SSL', default=False)
    is_active = BooleanField('Active', default=True)
    daily_limit = StringField('Daily Limit', default='500')
    priority = StringField('Priority', default='1')
    submit = SubmitField('Save Profile')


class TeamForm(FlaskForm):
    name = StringField('Team Name', validators=[DataRequired()])
    description = TextAreaField('Description')
    submit = SubmitField('Create Team')


class WebhookForm(FlaskForm):
    name = StringField('Webhook Name', validators=[DataRequired()])
    url = StringField('Webhook URL', validators=[DataRequired()])
    submit = SubmitField('Save Webhook')
