from flask_wtf import FlaskForm
from wtforms import (
    StringField, PasswordField, BooleanField, SubmitField, IntegerField, 
    TextAreaField, SelectField, FileField, MultipleFileField
)
from wtforms.validators import DataRequired, Email, EqualTo, ValidationError, Length, NumberRange

from app.models import User

class LoginForm(FlaskForm):
    username = StringField('Username', validators=[DataRequired()])
    password = PasswordField('Password', validators=[DataRequired()])
    remember_me = BooleanField('Remember Me')
    submit = SubmitField('Sign In')


class RegistrationForm(FlaskForm):
    username = StringField('Username', validators=[DataRequired(), Length(min=3, max=64)])
    email = StringField('Email', validators=[DataRequired(), Email()])
    password = PasswordField('Password', validators=[DataRequired(), Length(min=8)])
    password2 = PasswordField(
        'Repeat Password', validators=[DataRequired(), EqualTo('password')]
    )
    submit = SubmitField('Register')

    def validate_username(self, username):
        user = User.query.filter_by(username=username.data).first()
        if user is not None:
            raise ValidationError('Please use a different username.')

    def validate_email(self, email):
        user = User.query.filter_by(email=email.data).first()
        if user is not None:
            raise ValidationError('Please use a different email address.')


class SMTPProfileForm(FlaskForm):
    name = StringField('Profile Name', validators=[DataRequired()])
    server = StringField('SMTP Server', validators=[DataRequired()])
    port = IntegerField('Port', validators=[DataRequired(), NumberRange(min=1, max=65535)], default=587)
    username = StringField('Username', validators=[DataRequired()])
    password = PasswordField('Password') # Not required for edits
    sender_name = StringField('Sender Name')
    sender_email = StringField('Sender Email', validators=[Email()])
    use_tls = BooleanField('Use TLS', default=True)
    use_ssl = BooleanField('Use SSL')
    is_active = BooleanField('Active', default=True)
    daily_limit = IntegerField('Daily Limit', default=500)
    priority = IntegerField('Priority', default=1, validators=[NumberRange(min=1, max=100)])
    
    # IMAP Fields
    imap_server = StringField('IMAP Server')
    imap_port = IntegerField('IMAP Port', default=993)
    imap_username = StringField('IMAP Username')
    imap_password = PasswordField('IMAP Password')
    
    submit = SubmitField('Save Profile')


class SuppressionForm(FlaskForm):
    email = StringField('Email Address', validators=[DataRequired(), Email()])
    reason = StringField('Reason', default='Manual Add')
    submit = SubmitField('Add to List')
